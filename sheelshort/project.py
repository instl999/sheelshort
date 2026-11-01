"""A sheelshort project on disk, its state, and the resumable job runner that drives the ComfyUI servers.

Layout (projects/<name>/):
  bible.json                 characters, locations, props, visual style (agent-written)
  episodes/<EP>.json         clips and 3 s cuts with dialogue and acting directions (agent-written)
  inputs/                    user-supplied faces, voices, location photos (optional)
  assets/                    locked reference pack: char_<id>.png, sheet_<id>.png, prop_<id>.png, loc_<id>.png
  voices/                    cast voices: <id>.wav (H3/Drama 3 reference) and <id>.json (its transcript)
  perform/<EP>/              acted takes per line, the chosen take, and one dialogue track per clip
  clips/<EP>/                rendered H3 takes
  edit/<EP>/                 master.mp4, subtitles, review page
  state.json                 every queued job: server, prompt id, status, files (resume-safe)
"""
from __future__ import annotations

import json
import os
import threading
import time
import tomllib
import zlib
from pathlib import Path
from urllib.parse import urlparse

from . import comfy, flows

PKG = Path(__file__).resolve().parent
REPO = PKG.parent
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
AUDIO_EXTS = (".wav", ".mp3", ".flac", ".m4a", ".ogg")


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def config(path: Path | None = None) -> dict:
    """sheelshort.toml: the repo-level one, overridden key by key by the project's own if it has one."""
    cfg = tomllib.loads((REPO / "sheelshort.example.toml").read_text(encoding="utf-8"))
    for p in [REPO / "sheelshort.toml", path]:
        if p and p.exists():
            for section, values in tomllib.loads(p.read_text(encoding="utf-8")).items():
                cfg.setdefault(section, {}).update(values) if isinstance(values, dict) else cfg.update({section: values})
    if os.environ.get("SHEELSHORT_SERVERS"):
        cfg["comfy"]["servers"] = os.environ["SHEELSHORT_SERVERS"].split(",")
    return cfg


class Project:
    def __init__(self, root: Path, dry_run: bool = False):
        self.root = root.resolve()
        if not (self.root / "bible.json").exists():
            raise FileNotFoundError(f"{self.root} has no bible.json (create the project with `sheelshort new`)")
        self.cfg = config(self.root / "sheelshort.toml")
        self.bible = load_json(self.root / "bible.json")
        self.dry = dry_run
        self.waker = None   # fleet.Session when the servers are powered on on demand (`[fleet] core`)
        self.state_path = self.root / "state.json"
        self.state = load_json(self.state_path) if self.state_path.exists() else {"jobs": {}}
        self.lock = threading.Lock()
        self._clients: dict[str, comfy.ComfyClient] = {}
        self._assigned: dict[str, int] = {}
        self._uploaded: dict[tuple[str, str], str] = {}
        self._wf: dict[str, dict] = {}
        self._touched: set[str] = set()
        self._warned: dict[str, float] = {}
        self.limit_s: float | None = None   # test mode: only the opening clips of each episode, about this long
        self.only_eps: list[str] | None = None   # the episodes this run works on (casting covers only their characters)

    # ---------- files ----------

    @property
    def chars(self) -> dict:
        return self.bible["characters"]

    def episode(self, ep: str) -> dict:
        return load_json(self.root / "episodes" / f"{ep}.json")

    def clips(self, ep: str) -> list[dict]:
        """The episode's clips; in test mode (`limit_s`) only the opening ones: clips are taken in order until the
        running length reaches the limit, and one that would overshoot it by more than 5 s is left out."""
        from .h3prompt import clip_len
        clips = self.episode(ep)["clips"]
        if not self.limit_s:
            return clips
        out, t = [], 0.0
        for c in clips:
            n = clip_len(c)
            if out and (t >= self.limit_s or t + n > self.limit_s + 5):
                break
            out.append(c)
            t += n
        return out

    def in_use(self) -> tuple[set[str], set[str], set[str]]:
        """Characters, locations and props that appear in the clips in scope (all of the bible's if no limit)."""
        if not self.limit_s:
            return set(self.chars), set(self.bible["locations"]), set(self.bible.get("props", {}))
        chars, locs, props = set(), set(), set()
        for ep in self.only_eps or self.episodes():
            for clip in self.clips(ep):
                locs.add(clip["location"])
                for cut in clip["cuts"]:
                    chars |= {x for x in cut.get("subjects", []) + [cut.get("speaker"), cut.get("over")] if x}
                    props |= set(cut.get("props", []))
        return chars, locs, props

    def master_name(self) -> str:
        rc = self.cfg["render"]
        name = "master_acted" if rc["dialogue"] == "acted" else "master_actedref" if rc.get("voice_ref") == "acted" else "master"
        return f"{name}_{int(self.limit_s)}s" if self.limit_s else name

    def episodes(self) -> list[str]:
        return sorted(p.stem for p in (self.root / "episodes").glob("*.json"))

    def scene(self, ep: str) -> dict:
        """Bible + episode in the shape h3prompt expects."""
        e = self.episode(ep)
        return {**self.bible, **{k: v for k, v in e.items() if k != "id"}, "episode": ep}

    def d(self, *parts: str) -> Path:
        p = self.root.joinpath(*parts)
        p.mkdir(parents=True, exist_ok=True)
        return p

    def supplied(self, stem: str, exts: tuple[str, ...]) -> Path | None:
        inp = self.root / "inputs"
        if inp.exists():
            for p in sorted(inp.iterdir()):
                if p.suffix.lower() in exts and p.stem == stem:
                    return p
        return None

    def asset(self, stem: str) -> Path:
        """A locked reference image: the user's own in inputs/, else the generated one in assets/."""
        return self.supplied(stem, IMAGE_EXTS) or self.root / "assets" / f"{stem}.png"

    def voice(self, c: str) -> Path:
        return self.root / "voices" / f"{c}.wav"

    def wf(self, name: str) -> dict:
        if name not in self._wf:
            self._wf[name] = flows.load(PKG / "workflows" / self.cfg["workflows"][name])
        return self._wf[name]

    def save(self) -> None:
        """Write the jobs this process touched into state.json, keeping what other processes wrote meanwhile."""
        with self.lock:
            disk = load_json(self.state_path) if self.state_path.exists() else {"jobs": {}}
            for k in self._touched:
                if k in self.state["jobs"]:
                    disk["jobs"][k] = self.state["jobs"][k]
                else:
                    disk["jobs"].pop(k, None)
            self.state["jobs"] = {**disk["jobs"], **{k: v for k, v in self.state["jobs"].items() if k in self._touched}}
            save_json(self.state_path, disk)

    def seed(self, key: str) -> int:
        return zlib.crc32(key.encode()) + int(self.cfg["project"].get("seed_offset", 0))

    # ---------- servers ----------

    def client(self, url: str) -> comfy.ComfyClient:
        if url not in self._clients:
            headers = {}
            env = self.cfg["comfy"].get("auth_header_env")
            if env:
                headers["Authorization"] = f"Bearer {os.environ[env]}"
            self._clients[url] = comfy.ComfyClient(url, headers)
        return self._clients[url]

    def object_info(self, c: comfy.ComfyClient) -> dict:
        return c.object_info(REPO / ".cache" / f"object_info_{urlparse(c.base_url).hostname}.json")

    def wake(self) -> None:
        """Make sure servers are up before work is handed out (on-demand servers start on the first GPU job)."""
        if self.waker:
            self.waker.ensure()

    def pick(self, exclude: set[str] | frozenset = frozenset()) -> comfy.ComfyClient:
        """The least busy server with a free slot. A server holds at most `[comfy] in_flight` of this run's jobs (one
        running, one queued behind it); the others wait here, not in a server's queue, so a server the fleet adds
        mid-run gets work at once instead of finding everything already queued elsewhere."""
        cap = int(self.cfg["comfy"].get("in_flight", 2))
        self.wake()
        while True:
            with self.lock:
                best, best_load, reachable = None, None, 0
                for url in list(self.cfg["comfy"]["servers"]):
                    if url in exclude:
                        continue
                    if self._assigned.get(url, 0) >= cap:
                        reachable += 1
                        continue
                    try:
                        running, pending = self.client(url).queue_info()
                    except Exception as e:  # noqa: BLE001 - an unreachable server is skipped
                        if time.time() - self._warned.get(url, 0) > 60:   # once a minute, not on every retry
                            log(f"server {urlparse(url).hostname} unavailable: {str(e)[:120]}")
                            self._warned[url] = time.time()
                        continue
                    reachable += 1
                    load = running + pending + self._assigned.get(url, 0)
                    if best_load is None or load < best_load:
                        best, best_load = url, load
                if best is not None:
                    self._assigned[best] = self._assigned.get(best, 0) + 1
                    return self.client(best)
                if not reachable:
                    raise comfy.ComfyError("no ComfyUI server reachable")
            time.sleep(3)   # every server is full: wait for a slot (or for the fleet to add a server)

    def release(self, c: comfy.ComfyClient) -> None:
        with self.lock:
            self._assigned[c.base_url] = max(0, self._assigned.get(c.base_url, 0) - 1)

    def put(self, c: comfy.ComfyClient, path: Path) -> str:
        key = (c.base_url, f"{path}:{path.stat().st_mtime_ns}" if path.exists() else str(path))
        if key not in self._uploaded:
            self._uploaded[key] = f"sheelshort/{path.name}" if self.dry else c.upload(path)
        return self._uploaded[key]

    # ---------- jobs ----------

    def run_job(self, key: str, build, out_dir: Path, stem: str, kinds: tuple[str, ...] = comfy.OUTPUT_KEYS,
                timeout_s: float = 3600, exclude: set[str] | frozenset = frozenset()) -> list[Path] | None:
        """Queue one workflow, or resume it after a crash; a finished job is never queued twice.

        `build(client)` returns the API-format workflow; it runs only when the job is (re)queued, so uploads happen on
        the server that will run it. Every request is validated against that server's live node schemas first."""
        job = self.state["jobs"].setdefault(key, {})
        self._touched.add(key)
        if job.get("status") == "succeeded" and all(Path(f).exists() for f in job.get("files", [])):
            return [Path(f) for f in job["files"]]
        c = None
        try:
            if job.get("prompt_id") and job.get("status") == "queued" and not self.dry:
                c = self.client(job["server"])
                log(f"{key}: resuming {job['prompt_id']} on {urlparse(c.base_url).hostname}")
            else:
                if self.dry:
                    self.wake()   # a dry run only uses a server that is already running, for its node schemas
                    if not self.cfg["comfy"]["servers"]:
                        raise comfy.ComfyError("dry run: no server is running to validate against")
                c = self.pick(exclude) if not self.dry else self.client(self.cfg["comfy"]["servers"][0])
                api = build(c)
                problems = comfy.validate(api, self.object_info(c))
                req = self.d("requests") / f"{key.replace('/', '_')}.json"
                req.write_text(json.dumps(api, ensure_ascii=False, indent=1), encoding="utf-8")
                if problems:
                    raise comfy.ComfyError("workflow failed validation:\n  " + "\n  ".join(problems))
                if self.dry:
                    log(f"{key}: dry run, request written to {req}")
                    return None
                job.clear()
                job.update(server=c.base_url, prompt_id=c.queue(api), status="queued", queued_at=time.time())
                self.save()   # persist before waiting, so a crash resumes instead of queuing again
                log(f"{key}: queued on {urlparse(c.base_url).hostname}")
            hist = c.wait(job["prompt_id"], timeout_s=timeout_s)
            files = c.download_outputs(hist, out_dir, stem, kinds)
            job.update(status="succeeded", files=[str(f) for f in files], elapsed_s=round(time.time() - job["queued_at"], 1))
            log(f"{key}: done in {job['elapsed_s']:.0f}s")
            return files
        except comfy.ComfyError as e:
            job.update(status="failed", error=str(e)[:2000])
            log(f"{key}: FAILED {str(e)[:600]}")
        except TimeoutError as e:
            log(f"{key}: {e}; run the same command again to resume")
        finally:
            if c is not None and not self.dry:
                self.release(c)
                self.save()
        return None

    def forget(self, key: str) -> None:
        """Drop a job so the next run generates it again (a re-take)."""
        self.state["jobs"].pop(key, None)
        self._touched.add(key)
        self.save()
