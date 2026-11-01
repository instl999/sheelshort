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
import re
import threading
import time
import tomllib
import zlib
from pathlib import Path
from urllib.parse import urlparse

from . import comfy, eta, flows

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


def episode_key(ep: str) -> tuple:
    m = re.match(r"([A-Za-z]*)(\d+)(.*)", ep)
    return (m.group(1), int(m.group(2)), m.group(3)) if m else (ep, 0, "")


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
        self.io_lock = threading.Lock()   # state.json and progress.json writes, never held around network calls
        self._down_until: dict[str, float] = {}   # a server that just failed is skipped until then (circuit breaker)
        self._clients: dict[str, comfy.ComfyClient] = {}
        self._assigned: dict[str, int] = {}
        self._uploaded: dict[tuple[str, str], str] = {}
        self._wf: dict[str, dict] = {}
        self._touched: set[str] = set()
        self._warned: dict[str, float] = {}
        self.limit_s: float | None = None   # test mode: only the opening clips of each episode, about this long
        self.only_eps: list[str] | None = None   # the episodes this run works on (casting covers only their characters)
        self.timing = eta.Timing()
        self.run_info = {"pid": os.getpid(), "started": time.time(), "command": "", "phases": {}}

    # ---------- files ----------

    @property
    def chars(self) -> dict:
        """The bible's characters and each of their outfits as a character of its own (wardrobe.py)."""
        from . import wardrobe
        return {**self.bible["characters"], **wardrobe.variants(self.bible)}

    @property
    def locs(self) -> dict:
        """The bible's locations and each of their other lights as a location of its own (wardrobe.py)."""
        from . import wardrobe
        return {**self.bible["locations"], **wardrobe.location_variants(self.bible)}

    def plates(self, loc: str) -> list[Path]:
        """A location's plates in order: the user's own (inputs/loc_<id>.png, loc_<id>_2.png...) or the made ones.
        An exact match: loc_club must not pick up loc_club_hall, nor a location its night light's plates."""
        pat = re.compile(rf"loc_{re.escape(loc)}(_\d+)?\.(png|jpe?g|webp)$", re.I)
        for d in (self.root / "inputs", self.root / "assets"):
            found = sorted((f for f in d.glob(f"loc_{loc}*") if pat.fullmatch(f.name)), key=lambda f: (len(f.stem), f.name)) \
                if d.exists() else []
            if found:
                return found
        return [self.root / "assets" / f"loc_{loc}.png"]

    def base(self, c: str) -> str:
        """The character an outfit id belongs to (the id itself for a character in its own costume)."""
        return self.chars.get(c, {}).get("base", c)

    def episode(self, ep: str) -> dict:
        """The episode file as written (outfits not applied; clips() and scene() apply them)."""
        return load_json(self.root / "episodes" / f"{ep}.json")

    def clips(self, ep: str) -> list[dict]:
        """The episode's clips; in test mode (`limit_s`) only the opening ones: clips are taken in order until the
        running length reaches the limit, and one that would overshoot it by more than 5 s is left out."""
        from . import wardrobe
        from .h3prompt import clip_len
        e = self.episode(ep)
        clips = [wardrobe.dress(e, c) for c in e["clips"]]
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
            chars, locs, props = set(self.chars), set(self.bible["locations"]), set(self.bible.get("props", {}))
            for ep in self.episodes():   # the other lights in use
                locs |= {c["location"] for c in self.clips(ep)}
            return chars, locs, props
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
        """Episode ids in story order: E2 before E10, E99 before E100 (a string sort put E100 before E11)."""
        return sorted((p.stem for p in (self.root / "episodes").glob("*.json")), key=episode_key)

    def scene(self, ep: str) -> dict:
        """Bible + episode in the shape h3prompt expects."""
        from . import wardrobe
        e = self.episode(ep)
        return {**self.bible, **{k: v for k, v in e.items() if k != "id"}, "characters": self.chars, "locations": self.locs,
                "clips": [wardrobe.dress(e, c) for c in e["clips"]], "episode": ep}

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
        """The cast voice (an outfit speaks with its character's voice); `<id>_h3.wav` beside it is H3's cut."""
        return self.root / "voices" / f"{self.base(c)}.wav"

    def wf(self, name: str) -> dict:
        if name not in self._wf:
            self._wf[name] = flows.load(PKG / "workflows" / self.cfg["workflows"][name])
        return self._wf[name]

    def save(self) -> None:
        """Write the jobs this process touched into state.json, keeping what other processes wrote meanwhile."""
        with self.io_lock:
            disk = load_json(self.state_path) if self.state_path.exists() else {"jobs": {}}
            for k in self._touched:
                if k in self.state["jobs"]:
                    disk["jobs"][k] = self.state["jobs"][k]
                else:
                    disk["jobs"].pop(k, None)
            self.state["jobs"] = {**disk["jobs"], **{k: v for k, v in self.state["jobs"].items() if k in self._touched}}
            save_json(self.state_path, disk)

    def progress(self, phase: str, info: dict) -> None:
        """progress.json: what this run is doing, for `sheelshort status`."""
        with self.io_lock:
            self.run_info["phases"][phase] = info
            save_json(self.root / "progress.json", self.run_info)

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
        mid-run gets work at once instead of finding everything already queued elsewhere.

        Servers are probed in parallel, once each with short timeouts, and never while the run's lock is held: one
        slow server once froze every thread of a run for 11 min. A server that fails a probe is skipped for a minute.
        Only when no server has answered for `[comfy] unreachable_minutes` (10) does the job fail."""
        from concurrent.futures import ThreadPoolExecutor
        cap = int(self.cfg["comfy"].get("in_flight", 2))
        give_up = float(self.cfg["comfy"].get("unreachable_minutes", 10)) * 60
        stock_wait = float(self.cfg.get("fleet", {}).get("stock_wait_minutes", 360)) * 60
        alone_since, said = None, 0.0
        while True:
            self.wake()   # on-demand servers: also retries while AutoDL has no GPU in stock
            now = time.time()
            with self.lock:
                urls = [u for u in self.cfg["comfy"]["servers"] if u not in exclude]
                busy = [u for u in urls if self._assigned.get(u, 0) >= cap]
                free = [u for u in urls if u not in busy and self._down_until.get(u, 0) <= now]
            loads = {}
            if free:
                with ThreadPoolExecutor(len(free)) as ex:
                    for u, res in zip(free, ex.map(self._probe, free)):
                        if res is not None:
                            loads[u] = res
            with self.lock:
                ranked = sorted(loads, key=lambda u: loads[u] + self._assigned.get(u, 0))
                for u in ranked:
                    if self._assigned.get(u, 0) < cap:
                        self._assigned[u] = self._assigned.get(u, 0) + 1
                        return self.client(u)
            if loads or busy:
                alone_since = None
            else:
                alone_since = alone_since or now
                none_yet = self.waker is not None and not urls   # no server started yet: waiting for GPU stock
                limit = stock_wait if none_yet else give_up
                if none_yet and now - said > 300:
                    log(f"waiting for a GPU server (none in stock yet); up to {limit / 60:.0f} min")
                    said = now
                if now - alone_since > limit:
                    raise comfy.ComfyError(f"no ComfyUI server reachable for {limit / 60:.0f} min")
            time.sleep(3)   # every server is full or down: wait for a slot (or for the fleet to add a server)

    def _probe(self, url: str) -> int | None:
        """A server's queue length, or None (and a minute's rest for it) when it does not answer at once."""
        try:
            running, pending = self.client(url).queue_info(quick=True)
            return running + pending
        except Exception as e:  # noqa: BLE001 - an unreachable server is skipped
            self._down_until[url] = time.time() + 60
            if time.time() - self._warned.get(url, 0) > 60:   # once a minute, not on every retry
                log(f"server {urlparse(url).hostname} unavailable, skipped for 1 min: {str(e)[:120]}")
                self._warned[url] = time.time()
            return None

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
                timeout_s: float = 3600, exclude: set[str] | frozenset = frozenset(), units: float = 1.0) -> list[Path] | None:
        """Queue one workflow, or resume it after a crash; a finished job is never queued twice.

        `build(client)` returns the API-format workflow; it runs only when the job is (re)queued, so uploads happen on
        the server that will run it. Every request is validated against that server's live node schemas first."""
        job = self.state["jobs"].setdefault(key, {})
        self._touched.add(key)
        if job.get("status") == "succeeded" and all(Path(f).exists() for f in job.get("files", [])):
            return [Path(f) for f in job["files"]]
        tries = 1 + int(self.cfg["comfy"].get("job_retries", 2))
        avoid = set(exclude)
        for attempt in range(tries):
            files, failed_on = self._run_once(key, build, out_dir, stem, kinds, timeout_s, frozenset(avoid), units)
            if files is not None or failed_on is None:
                return files
            if attempt < tries - 1:   # a dropped server, a lost prompt, a network error: try again on another server
                others = len(self.cfg["comfy"]["servers"]) > len(avoid | {failed_on})
                err = self.state["jobs"][key].get("error", "")
                if not others and " failed: " in err and err.startswith("ComfyError: prompt "):
                    break   # the workflow itself failed on the only server there is: the same again would fail too
                if others:
                    avoid.add(failed_on)
                log(f"{key}: trying again ({attempt + 2}/{tries})")
                job = self.state["jobs"][key]
                job.pop("prompt_id", None)
                job["status"] = "retrying"
        return None

    def _run_once(self, key, build, out_dir, stem, kinds, timeout_s, exclude, units) -> tuple[list[Path] | None, str | None]:
        """One attempt of run_job: (files, None) on success, (None, server) when it failed there and may be retried
        elsewhere, (None, None) when it must not be retried (a dry run, an invalid workflow)."""
        job = self.state["jobs"][key]
        c, picked = None, False
        try:
            if job.get("prompt_id") and job.get("status") == "queued" and not self.dry:
                self.wake()
                c = self.client(job["server"])
                if job["server"] not in self.cfg["comfy"]["servers"]:
                    # the run that queued it is gone and so is its server (powered off): queue it again
                    log(f"{key}: its server is no longer in use; queuing it again")
                    job.pop("prompt_id")
                    return None, job["server"]
                log(f"{key}: resuming {job['prompt_id']} on {urlparse(c.base_url).hostname}")
            else:
                if self.dry:
                    self.wake()   # a dry run only uses a server that is already running, for its node schemas
                    if not self.cfg["comfy"]["servers"]:
                        raise comfy.ComfyError("dry run: no server is running to validate against")
                c = self.pick(exclude) if not self.dry else self.client(self.cfg["comfy"]["servers"][0])
                picked = not self.dry
                api = build(c)
                problems = comfy.validate(api, self.object_info(c))
                req = self.d("requests") / f"{key.replace('/', '_')}.json"
                req.write_text(json.dumps(api, ensure_ascii=False, indent=1), encoding="utf-8")
                if problems:
                    job.update(status="failed", error="workflow failed validation:\n  " + "\n  ".join(problems))
                    log(f"{key}: FAILED {job['error'][:600]}")
                    return None, None
                if self.dry:
                    log(f"{key}: dry run, request written to {req}")
                    return None, None
                job.clear()
                job.update(server=c.base_url, prompt_id=c.queue(api), status="queued", queued_at=time.time(), units=units)
                job["eta"] = self._eta(key, c.base_url, units)
                self.save()   # persist before waiting, so a crash resumes instead of queuing again
                log(f"{key}: queued on {urlparse(c.base_url).hostname}, done ~{eta.clock(job['eta'])} "
                    f"(in ~{eta.span(job['eta'] - time.time())})")
            hist = c.wait(job["prompt_id"], timeout_s=timeout_s)
            t_dl = time.time()
            files = c.download_outputs(hist, out_dir, stem, kinds)
            dl = time.time() - t_dl
            job.update(status="succeeded", files=[str(f) for f in files], elapsed_s=round(time.time() - job["queued_at"], 1))
            ran = eta.exec_seconds(hist)
            if ran is not None:   # what this kind of job really costs: the server's run plus the download
                self.timing.record(key, ran + dl, job.get("units", units))
            size = sum(f.stat().st_size for f in files if f.exists())
            speed = f", {size / 1e6:.1f} MB down in {dl:.0f}s ({size / 1e6 / max(dl, 0.01):.1f} MB/s)" if size > 1e6 else ""
            log(f"{key}: done in {job['elapsed_s']:.0f}s" + (f" (ran {ran:.0f}s on the server{speed})" if ran is not None else ""))
            return files, None
        except Exception as e:  # noqa: BLE001 - any failure of one job is that job's, never the whole run's
            if isinstance(e, TimeoutError):
                e = comfy.ComfyError(f"{e}; given up")
            job.update(status="failed", error=f"{type(e).__name__}: {str(e)[:2000]}")
            log(f"{key}: FAILED {type(e).__name__}: {str(e)[:600]}")
            if c is not None:
                self._down_until[c.base_url] = time.time() + 60
            return None, c.base_url if c is not None else ""
        finally:
            if picked:
                self.release(c)
            if c is not None and not self.dry:
                self.save()

    def _eta(self, key: str, server: str, units: float) -> float:
        """When a job just queued on `server` should be back: after the jobs of this run still ahead of it there."""
        now = time.time()
        ahead = [j.get("eta", now) for k, j in list(self.state["jobs"].items())
                 if k != key and j.get("status") == "queued" and j.get("server") == server and k in self._touched]
        return max([now] + ahead) + self.timing.expected(key, units)

    def forget(self, key: str) -> None:
        """Drop a job so the next run generates it again (a re-take)."""
        self.state["jobs"].pop(key, None)
        self._touched.add(key)
        self.save()
