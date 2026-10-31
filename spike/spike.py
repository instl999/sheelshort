"""M0 spike: does MiniMax H3 (U22 workflow on our ComfyUI servers) keep faces and voices
consistent across clips of one scene, under the house shot rules?

Variants, each run for every clip in scene.json:
  A  keyframe-anchored: a Qwen-edit keyframe (location + cast composed) is the first frame of [Shot 1]
  B  cast portraits + location pictures as references
  C  as B, plus one reference voice per speaking character
  D  as C, plus the clip's white-mesh blockout animatic as a camera/blocking reference video
  E  as D, plus a [Shot 1] keyframe painted from the blockout's clay render
  F  as C, plus camera text measured from the blockout (angle, lens, shot size, positions, set pieces)
  G  as F, plus the [Shot 1] blockout keyframe

Usage:
  python spike/spike.py doctor
  python spike/spike.py locations         # index + tag location photo libraries (inputs/locations/<loc>/)
  python spike/spike.py blockout          # Blender: clay/depth stills + animatic per clip (locations with a set file)
  python spike/spike.py images            # portraits, location plates, keyframes (skips files in inputs/)
  python spike/spike.py voices            # recordings from inputs/ or Qwen3-TTS designed voices
  python spike/spike.py clips --variant B --dry-run
  python spike/spike.py clips --variant B [--clips K1]
  python spike/spike.py eval
  python spike/spike.py all
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import threading
import time
import tomllib
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import blockout
import comfy
import evaluate
import flows
import h3prompt
import library
import media
import qc

HERE = Path(__file__).resolve().parent
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")
AUDIO_EXTS = (".wav", ".mp3", ".flac", ".m4a", ".ogg")
VOICE_MAX_S = 7.0   # two voices must fit H3's 15 s reference-audio budget


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


BLOCKOUT_VARIANTS = ("D", "E", "F", "G")   # need `blockout` renders
ANIMATIC_VARIANTS = ("D", "E")             # animatic as H3 reference video
KEYFRAME_VARIANTS = ("E", "G")             # [Shot 1] keyframe painted from the blockout
CAMERA_TEXT_VARIANTS = ("F", "G")          # prompt camera text measured from the blockout
VOICE_VARIANTS = ("C", "D", "E", "F", "G")


class Spike:
    def __init__(self, config_path: Path, out: Path | None = None, dry_run: bool = False):
        self.cfg = tomllib.loads(config_path.read_text(encoding="utf-8"))
        self.root = config_path.resolve().parent
        self.scene = json.loads((self.root / self.cfg["spike"].get("scene", "scene.json")).read_text(encoding="utf-8"))
        self.chars = self.scene["characters"]
        self.clips = {c["id"]: c for c in self.scene["clips"]}
        self.out = (out or self.root / "out").resolve()
        self.inputs = self.root / "inputs"
        self.dry = dry_run
        self.state_path = self.out / "state.json"
        self.state = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else {"jobs": {}}
        self.lock = threading.Lock()
        self._clients: dict[str, comfy.ComfyClient] = {}
        self._assigned: dict[str, int] = {}
        self._uploaded: dict[tuple[str, str], str] = {}

    # ---------- state, paths, assets ----------

    def save(self) -> None:
        with self.lock:
            self.out.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.state_path)

    def supplied(self, stem: str, exts: tuple[str, ...]) -> list[Path]:
        """User files in inputs/: `<stem>.<ext>` or several as `<stem>_<anything>.<ext>`."""
        if not self.inputs.exists():
            return []
        return sorted(p for p in self.inputs.iterdir()
                      if p.suffix.lower() in exts and (p.stem == stem or p.stem.startswith(stem + "_")))

    def portrait(self, c: str) -> Path:
        return (self.supplied(f"char_{c}", IMAGE_EXTS) or [self.out / "images" / f"char_{c}.png"])[0]

    def sheet(self, c: str) -> Path:
        """Front/side/back reference sheet: locks hair, costume and proportions."""
        return (self.supplied(f"sheet_{c}", IMAGE_EXTS) or [self.out / "images" / f"sheet_{c}.png"])[0]

    def prop_picture(self, pr: str) -> Path:
        return (self.supplied(f"prop_{pr}", IMAGE_EXTS) or [self.out / "images" / f"prop_{pr}.png"])[0]

    def location_pictures(self, loc: str) -> list[Path]:
        """Every picture of a location: its photo library, else the generated plate."""
        return library.files_for(self.inputs, loc) or [self.out / "images" / f"loc_{loc}.png"]

    def pick_locations(self, clip: dict, k: int) -> list[Path]:
        """The k location pictures that best fit this clip's cuts (see library.select); the plate if no library."""
        loc = clip["location"]
        lib = library.load(self.inputs, loc)
        if not lib["entries"]:
            return [self.require(p, "run `images` first") for p in self.location_pictures(loc)][:k]
        untagged = [key for key, e in lib["entries"].items() if not e.get("tags")]
        if untagged:
            log(f"{clip['id']}: {len(untagged)} untagged picture(s) in the {loc} library; run `locations` for better picks")
        when = clip.get("time") or self.scene["locations"][loc].get("time")
        picks = library.select(lib, clip, when, min(k, self.cfg.get("library", {}).get("max_per_clip", 3)))
        if not picks:
            raise ValueError(f"{clip['id']}: no usable picture in the {loc} library (all vetoed or filtered)")
        for key, why in picks:
            log(f"{clip['id']}: location picture {key}: {why}")
        return [self.inputs / key for key, _ in picks]

    def pick_locations_by_view(self, clip: dict, k: int) -> tuple[list[Path], dict[int, str]]:
        """Like pick_locations, but each cut's needs come from its measured blockout camera view (set pieces in and
        out of frame), view plates made for uncovered cuts join the candidates, and each cut is bound to the chosen
        picture that matches it. Returns the pictures and {cut: picture file name}."""
        ctx = self.view_context(clip)
        loc = clip["location"]
        lib = library.load(self.inputs, loc)
        if not ctx or not lib["entries"]:
            return self.pick_locations(clip, k), {}
        mclip, look, pieces = ctx
        wants = blockout.view_wants(mclip, clip, look, pieces)
        when = clip.get("time") or self.scene["locations"][loc].get("time")
        plates = 0
        for meta in mclip["cuts"]:
            vp = self.view_plate(clip["id"], meta["cut"])
            if vp.exists() and self.state["jobs"].get(f"qc/{vp.stem}", {}).get("passed"):   # checked: shows what the camera sees
                plates += 1
                lib["entries"][str(vp)] = {"dhash": f"{media.dhash(vp):016x}", "pin": True, "synthetic": True, "tags": {
                    "framing": "medium", "facing": "", "features": meta["view"]["set_pieces"], "landmarks": [],
                    "time_of_day": when or "unknown", "people": False, "visible_text": False}}
        picks = library.select(lib, clip, when, min(k, self.cfg.get("library", {}).get("max_per_clip", 3) + plates), wants)
        if not picks:
            raise ValueError(f"{clip['id']}: no usable picture in the {loc} library (all vetoed or filtered)")
        for key, why in picks:
            log(f"{clip['id']}: location picture {Path(key).name}: {why}")
        assigned = library.assign(lib, [key for key, _ in picks], wants)
        for n, key in sorted(assigned.items()):
            log(f"{clip['id']}: shot {n} background bound to {Path(key).name}")
        return [self.inputs / key for key, _ in picks], {n: Path(key).name for n, key in assigned.items()}

    def keyframe(self, clip_id: str) -> Path:
        return (self.supplied(f"key_{clip_id}", IMAGE_EXTS) or [self.out / "images" / f"key_{clip_id}.png"])[0]

    def blockout_clip(self, clip_id: str) -> dict | None:
        """Blockout renders of a clip (animatic, per-cut clay/depth stills), if `blockout` has run for it."""
        m = self.out / "blockout" / "manifest.json"
        if not m.exists():
            return None
        return json.loads(m.read_text(encoding="utf-8"))["clips"].get(clip_id)

    def set_file(self, loc: str) -> Path | None:
        rel = self.scene["locations"][loc].get("blockout")
        return (self.inputs / rel) if rel else None

    def voice(self, c: str) -> Path:
        return self.out / "voices" / f"{c}.wav"

    @staticmethod
    def require(path: Path, hint: str) -> Path:
        if not path.exists():
            raise FileNotFoundError(f"missing {path} ({hint})")
        return path

    def wf(self, name: str) -> dict:
        return flows.load(self.root / self.cfg["workflows"][name])

    # ---------- servers ----------

    def client(self, base_url: str) -> comfy.ComfyClient:
        if base_url not in self._clients:
            headers = {}
            if self.cfg["comfy"].get("auth_header_env"):
                headers["Authorization"] = f"Bearer {os.environ[self.cfg['comfy']['auth_header_env']]}"
            self._clients[base_url] = comfy.ComfyClient(base_url, headers)
        return self._clients[base_url]

    def object_info(self, c: comfy.ComfyClient) -> dict:
        return c.object_info(self.out / "cache" / f"object_info_{urlparse(c.base_url).hostname}.json")

    def pick(self) -> comfy.ComfyClient:
        """Least busy server, counting jobs we've already handed it in this run."""
        with self.lock:
            best, best_load = None, None
            for url in self.cfg["comfy"]["servers"]:
                try:
                    running, pending = self.client(url).queue_info()
                except Exception as e:  # noqa: BLE001 - an unreachable server is skipped
                    log(f"server {url} unavailable: {e}")
                    continue
                load = running + pending + self._assigned.get(url, 0)
                if best_load is None or load < best_load:
                    best, best_load = url, load
            if best is None:
                raise comfy.ComfyError("no ComfyUI server reachable")
            self._assigned[best] = self._assigned.get(best, 0) + 1
            return self.client(best)

    def put(self, c: comfy.ComfyClient, path: Path) -> str:
        key = (c.base_url, f"{path}:{path.stat().st_mtime_ns}" if path.exists() else str(path))
        if key not in self._uploaded:
            self._uploaded[key] = f"sheelshort/{path.name}" if self.dry else c.upload(path)
        return self._uploaded[key]

    # ---------- generic job ----------

    def run_job(self, key: str, build, out_dir: Path, stem: str, kinds: tuple[str, ...], timeout_s: float) -> list[Path] | None:
        """Queue one workflow, or resume it after a crash; never queue the same job twice."""
        job = self.state["jobs"].setdefault(key, {})
        if job.get("status") == "succeeded" and all(Path(f).exists() for f in job.get("files", [])):
            return [Path(f) for f in job["files"]]
        try:
            if job.get("prompt_id") and job.get("status") == "queued" and not self.dry:
                c = self.client(job["server"])
                log(f"{key}: resuming {job['prompt_id']} on {urlparse(c.base_url).hostname}")
            else:
                c = self.pick() if not self.dry else self.client(self.cfg["comfy"]["servers"][0])
                api = build(c)
                problems = comfy.validate(api, self.object_info(c))
                req = self.out / "requests" / f"{key.replace('/', '_')}.json"
                req.parent.mkdir(parents=True, exist_ok=True)
                req.write_text(json.dumps(api, ensure_ascii=False, indent=1), encoding="utf-8")
                if problems:
                    raise comfy.ComfyError("workflow failed validation:\n  " + "\n  ".join(problems))
                if self.dry:
                    log(f"{key}: dry run, request written to {req}")
                    return None
                job.clear()
                job.update(server=c.base_url, prompt_id=c.queue(api), status="queued", queued_at=time.time())
                self.save()   # persist before waiting so a crash resumes instead of re-queuing
                log(f"{key}: queued {job['prompt_id']} on {urlparse(c.base_url).hostname}")
            hist = c.wait(job["prompt_id"], timeout_s=timeout_s)
            files = c.download_outputs(hist, out_dir, stem, kinds)
            job.update(status="succeeded", files=[str(f) for f in files], elapsed_s=round(time.time() - job["queued_at"], 1))
            log(f"{key}: done in {job['elapsed_s']:.0f}s -> {files[0]}")
            return files
        except comfy.ComfyError as e:
            job.update(status="failed", error=str(e))
            log(f"{key}: FAILED {e}")
        except TimeoutError as e:
            log(f"{key}: {e}; re-run the same command to resume")
        finally:
            if not self.dry:
                self.save()
        return None

    def seed(self, key: str) -> int:
        return zlib.crc32(key.encode()) + self.cfg["spike"].get("seed_offset", 0)

    # ---------- images ----------

    def portrait_prompt(self, c: str) -> str:
        return ("Photorealistic character reference portrait of an original character who does not resemble any real person or celebrity. "
                f"{self.chars[c]['look']} Front view, head and shoulders, looking straight at the camera with a neutral expression, "
                "plain light-grey seamless studio backdrop, soft even key light, sharp focus on the eyes, long eyelashes and fine skin "
                "texture visible, 85mm lens, high detail, no text, no watermark.")

    def sheet_prompt(self, c: str) -> str:
        ch = self.chars[c]
        return ("Turn image 1 into a character reference sheet of exactly the same person on a plain light-grey studio background: "
                "three full-body views side by side at the same scale - front view, side view facing left, and back view - standing in a "
                f"relaxed neutral pose. Hair: {ch['hair']}. Costume: {ch['costume']}. Keep the face identical to image 1. "
                "Even soft studio lighting, photorealistic, no text, no labels, no watermark.")

    def prop_prompt(self, pr: str) -> str:
        return (f"Photorealistic product reference photo of {self.scene['props'][pr]['look']} Centered on a plain light-grey "
                "background, soft studio lighting, sharp focus, no hands, no text, no watermark.")

    def plate_prompt(self, loc: str) -> str:
        return (f"Photorealistic cinematic establishing still of {self.scene['locations'][loc]['look']} The room is empty with no people. "
                "Vertical 9:16 composition, eye-level camera, realistic night lighting, high detail, no text, no watermark.")

    def keyframe_prompt(self, clip: dict, cast: list[str]) -> str:
        names = {"loc": "the room from image 1"}
        names |= {c: f"the {self.chars[c]['role']} from image {i}" for i, c in enumerate(cast, 2)}
        names |= {pr: f"a {v['name']}" for pr, v in self.scene.get("props", {}).items()}
        first = clip["cuts"][0]
        return (f"Use image 1 as the room. Place {' and '.join(names[c] for c in cast)} into it. "
                f"{first['composition'].format_map(names)} {first['action'].format_map(names)} "
                f"Keep every face, hairstyle, makeup detail and outfit exactly as in the reference images. "
                "Photorealistic cinematic film still from a live-action vertical drama, shallow depth of field, fine facial detail, no text.")

    def set_plate_prompt(self, clip: dict) -> str:
        loc = self.scene["locations"][clip["location"]]
        return ("Image 1 is a grey clay render of the film set, seen from the exact camera position of the shot; image 2 shows the "
                "real location. Repaint image 1 as a photorealistic cinematic film still of the location in image 2: keep image 1's "
                "camera position, lens perspective, framing, room layout and the position of every wall, window, doorway and piece of "
                "furniture exactly, and take the materials, colours, lighting, decor and the view through the windows from image 2. "
                f"The location is {loc['look']} Keep the grey mannequin figures as plain smooth light-grey mannequins in exactly the "
                "same places, sizes and poses. No text, no watermark.")

    def blockout_key_prompt(self, clip: dict, cast: list[str]) -> str:
        names = {"loc": "the room"} | {c: f"the {self.chars[c]['role']} from image {i}" for i, c in enumerate(cast, 2)}
        names |= {pr: f"a {v['name']}" for pr, v in self.scene.get("props", {}).items()}
        first = clip["cuts"][0]
        cam = self.camera_texts(clip).get(1)
        framing = cam["text"] if cam and cam.get("replace") else first["composition"]
        dress = " ".join(f"The {self.chars[c]['role']} from image {i} wears {self.chars[c]['costume']}, hair {self.chars[c]['hair']}."
                         for i, c in enumerate(cast, 2))
        return ("Image 1 is a film still in which grey mannequins stand in for the actors. Replace each mannequin with a real person, "
                "keeping the mannequin's exact position, size, pose and perspective, and keep the room, camera and lighting of image 1 unchanged. "
                f"{framing.format_map(names)} {dress} Keep every face and makeup detail exactly as in the reference images. "
                "Photorealistic cinematic film still from a live-action vertical drama, shallow depth of field, fine facial detail, "
                "no mannequins left, no text.")

    def blockout_keyframe(self, clip_id: str) -> Path:
        return (self.supplied(f"bkey_{clip_id}", IMAGE_EXTS) or [self.out / "images" / f"bkey_{clip_id}.png"])[0]

    def view_plate(self, clip_id: str, cut: int) -> Path:
        return self.out / "images" / f"view_{clip_id}_{cut}.png"

    def view_plate_prompt(self, clip: dict, shown: list[str], missing: list[str]) -> str:
        """Clay render only, no photo: given a photo as well, Qwen repaints the photo's viewpoint instead of the clay's."""
        loc = self.scene["locations"][clip["location"]]
        return ("Repaint this grey clay render of a film set as a photorealistic cinematic film still. Keep the camera position, "
                "camera angle, tilt, lens perspective and framing exactly, and keep every wall, opening and object exactly where it "
                "is: wherever the render shows a plain wall, ceiling or floor, show a wall, ceiling or floor. "
                f"Materials, colours and lighting come from this location: {loc['look']} "
                + (f"In this view only these are visible: the {', the '.join(shown)}. " if shown else "")
                + (f"The {', the '.join(missing)} {'is' if len(missing) == 1 else 'are'} behind the camera or out of frame and must not appear. " if missing else "")
                + "Remove the grey mannequins completely; the room is empty. No people, no text, no watermark.")

    def vlm(self) -> tuple[str, str, str]:
        lc = self.cfg.get("library", {})
        return (lc.get("vlm_model", "Qwen3.8-27B-Q4_K_M.gguf"), lc.get("vlm_mmproj", "Qwen3.8-mmproj-BF16.gguf"),
                lc.get("vlm_chat_handler", "Qwen3.8"))

    def check_image(self, path: Path, must: list[tuple[str, float]], at_most: int, label: str) -> list[str]:
        """Vision check of a generated still (qc.py); [] means it passes. Cached per image content."""
        sha = hashlib.sha1(path.read_bytes()).hexdigest()[:12]
        key = f"vlm/qc/{label}_{sha}"
        files = self.run_job(key, lambda cl: flows.vlm_describe(self.put(cl, path), qc.PEOPLE_PROMPT, *self.vlm(), self.seed(key)),
                             self.out / "qc", f"{label}_{sha}", ("text",), 900)
        if not files:
            return ["vision check did not run"]
        try:
            result = qc.parse(files[0].read_text(encoding="utf-8"))
        except ValueError as e:
            return [f"vision check unreadable: {e}"]
        problems = qc.check(result, must, at_most)
        seen = ", ".join(f"{q['sex']} at x={q['x']:.2f}" for q in result["people"]) or "nobody"
        log(f"{label}: vision check {'passed' if not problems else 'FAILED'} (sees {seen})")
        return problems

    def check_content(self, path: Path, want: set[str], avoid: set[str], label: str) -> list[str]:
        """Tag a generated location view like a library photo and compare with what its camera should show."""
        sha = hashlib.sha1(path.read_bytes()).hexdigest()[:12]
        key = f"vlm/tags/{label}_{sha}"
        files = self.run_job(key, lambda cl: flows.vlm_describe(self.put(cl, path), library.TAG_PROMPT, *self.vlm(), self.seed(key)),
                             self.out / "qc", f"{label}_{sha}_tags", ("text",), 900)
        if not files:
            return ["content check did not run"]
        try:
            kw = library.keywords(library.parse_tags(files[0].read_text(encoding="utf-8")))
        except ValueError as e:
            return [f"content check unreadable: {e}"]
        problems = [f"shows {', '.join(sorted(kw & avoid))}, which this camera must not see"] if kw & avoid else []
        if want and not kw & want:
            problems.append(f"shows none of {', '.join(sorted(want))}")
        log(f"{label}: content check {'passed' if not problems else 'FAILED'} (sees {', '.join(sorted(kw)) or 'nothing tagged'})")
        return problems

    def checked_generation(self, label: str, dest: Path, make, must: list[tuple[str, float]], at_most: int, extra=None) -> None:
        """Generate an image, check it (people, and `extra(path)` if given), and retry with new seeds until it passes
        (images.keyframe_attempts); the best attempt is kept at `dest` and the verdict recorded in the job state."""
        record = f"qc/{label}"
        if self.dry or (dest.exists() and self.state["jobs"].get(record, {}).get("passed")):
            return
        tries = []
        for a in range(int(self.cfg["images"].get("keyframe_attempts", 3))):
            if a == 0 and dest.exists():
                path = dest   # an earlier, unchecked image is checked before anything new is generated
            else:
                key = f"image/{label}" + (f"_r{a}" if a else "")
                files = self.run_job(key, lambda cl, key=key: make(cl, self.seed(key)), self.out / "images" / "tries",
                                     f"{label}_{a}", ("images",), 900)
                if not files:
                    continue
                path = files[0]
            problems = self.check_image(path, must, at_most, label)
            if extra:
                problems += extra(path)
            tries.append((len(problems), a, path, problems))
            if not problems:
                break
            log(f"{label}: attempt {a + 1} rejected: {'; '.join(problems)}")
        if not tries:
            return
        _, _, path, problems = min(tries)
        if path != dest:
            shutil.copy(path, dest)
        self.state["jobs"][record] = {"passed": not problems, "problems": problems, "file": str(path), "attempts": len(tries)}
        self.save()
        if problems:
            log(f"{label}: no attempt passed; keeping the best ({'; '.join(problems)}). Review {dest} or replace it in inputs/")

    def view_plates(self, edit: dict) -> None:
        """For each cut shot by a camera placed in the set file whose view no library photo shows (something it
        needs is missing, or something out of frame would come along), paint an empty-room plate of exactly that
        view from its clay render, and check that it shows what the camera sees and nothing it must not."""
        for clip in self.scene["clips"]:
            ctx = self.view_context(clip)
            lib = library.load(self.inputs, clip["location"])
            if not ctx or not lib["entries"]:
                continue
            mclip, look, pieces = ctx
            wants = blockout.view_wants(mclip, clip, look, pieces)
            for meta, (ct, want, avoid) in zip(mclip["cuts"], wants):
                if not meta.get("set_camera") or library.covers(lib, want, avoid):
                    continue
                n = meta["cut"]
                clay = Path(self.blockout_clip(clip["id"])["stills"][f"cut{n}"]["clay"])
                shown = meta["view"]["set_pieces"]
                missing = [q for q in sorted(pieces - set(shown)) if blockout._mentioned(q, look.lower())]
                log(f"{clip['id']}: shot {n} needs a view plate (no library photo shows {', '.join(shown) or 'its view'} "
                    f"without {', '.join(missing) or 'extras'})")
                prompt = self.view_plate_prompt(clip, shown, missing)
                label = f"view_{clip['id']}_{n}"
                self.checked_generation(label, self.view_plate(clip["id"], n), lambda cl, seed, clay=clay, prompt=prompt, label=label: flows.edit(
                    edit, prompt, [self.put(cl, clay)], self.cfg["images"]["keyframe_long_side"], seed, f"sheelshort/{label}"),
                    [], 0, lambda path, want=want, avoid=avoid, label=label: self.check_content(path, want, avoid, label))

    def cmd_images(self, variants: list[str]) -> None:
        ic = self.cfg["images"]
        t2i = self.wf("t2i")
        for c in self.chars:
            if not self.portrait(c).exists():
                key = f"image/char_{c}"
                self.run_job(key, lambda cl, c=c, key=key: flows.t2i(t2i, self.portrait_prompt(c), *ic["portrait_size"], self.seed(key), f"sheelshort/char_{c}"),
                             self.out / "images", f"char_{c}", ("images",), 900)
        edit = self.wf("edit")
        for c in self.chars:
            if not self.sheet(c).exists():
                key = f"image/sheet_{c}"
                src = self.require(self.portrait(c), "portrait generation failed")
                self.run_job(key, lambda cl, c=c, key=key, src=src: flows.edit(
                    edit, self.sheet_prompt(c), [self.put(cl, src)], ic["sheet_long_side"], self.seed(key), f"sheelshort/sheet_{c}",
                    aspect="16:9", background="#D9D9D9"), self.out / "images", f"sheet_{c}", ("images",), 900)
        for pr in self.scene.get("props", {}):
            if not self.prop_picture(pr).exists():
                key = f"image/prop_{pr}"
                self.run_job(key, lambda cl, pr=pr, key=key: flows.t2i(t2i, self.prop_prompt(pr), *ic["prop_size"], self.seed(key), f"sheelshort/prop_{pr}"),
                             self.out / "images", f"prop_{pr}", ("images",), 900)
        for loc in self.scene["locations"]:
            if not self.location_pictures(loc)[0].exists():
                key = f"image/loc_{loc}"
                self.run_job(key, lambda cl, loc=loc, key=key: flows.t2i(t2i, self.plate_prompt(loc), *ic["plate_size"], self.seed(key), f"sheelshort/loc_{loc}"),
                             self.out / "images", f"loc_{loc}", ("images",), 900)
        if set(CAMERA_TEXT_VARIANTS) & set(variants):
            self.view_plates(edit)
        if set(KEYFRAME_VARIANTS) & set(variants):
            self.blockout_keyframes(edit)
        if "A" not in variants:
            return
        for clip in self.scene["clips"]:
            if self.keyframe(clip["id"]).exists():
                continue
            cast = clip["cuts"][0]["subjects"][:2]
            srcs = self.pick_locations({**clip, "cuts": clip["cuts"][:1]}, 1)
            srcs += [self.require(self.portrait(c), "run `images` first") for c in cast]
            key = f"image/key_{clip['id']}"
            self.run_job(key, lambda cl, clip=clip, cast=cast, srcs=srcs, key=key: flows.edit(
                edit, self.keyframe_prompt(clip, cast), [self.put(cl, p) for p in srcs], ic["keyframe_long_side"], self.seed(key), f"sheelshort/key_{clip['id']}"),
                self.out / "images", f"key_{clip['id']}", ("images",), 900)

    def blockout_keyframes(self, edit: dict) -> None:
        """[Shot 1] keyframes from the blockout: clay render -> set plate (Qwen, with the best location picture)
        -> mannequins replaced by the cast (Qwen, with their close-ups)."""
        ic = self.cfg["images"]
        roles = {c: v["role"] for c, v in self.chars.items()}
        for clip in self.scene["clips"]:
            if self.supplied(f"bkey_{clip['id']}", IMAGE_EXTS):
                continue
            bo = self.blockout_clip(clip["id"])
            if not bo:
                log(f"{clip['id']}: no blockout renders; run `blockout` first (skipping its keyframe)")
                continue
            clay = Path(bo["stills"]["cut1"]["clay"])
            loc_pic = self.pick_locations({**clip, "cuts": clip["cuts"][:1]}, 1)[0]
            if self.blockout_keyframe(clip["id"]).exists() and self.state["jobs"].get(f"qc/bkey_{clip['id']}", {}).get("passed"):
                continue
            key = f"image/setplate_{clip['id']}"
            files = self.run_job(key, lambda cl, clip=clip, key=key: flows.edit(
                edit, self.set_plate_prompt(clip), [self.put(cl, clay), self.put(cl, loc_pic)], ic["keyframe_long_side"],
                self.seed(key), f"sheelshort/setplate_{clip['id']}"), self.out / "images", f"setplate_{clip['id']}", ("images",), 900)
            if not files:
                continue
            cast = [c for c in clip["cuts"][0].get("subjects", []) if c in self.chars][:2]
            srcs = [files[0]] + [self.require(self.portrait(c), "run `images` first") for c in cast]
            view = bo["cuts"][0].get("view")
            must, at_most = qc.expected_people(view, roles) if view else ([], 99)
            self.checked_generation(f"bkey_{clip['id']}", self.blockout_keyframe(clip["id"]), lambda cl, seed, clip=clip, cast=cast, srcs=srcs: flows.edit(
                edit, self.blockout_key_prompt(clip, cast), [self.put(cl, p) for p in srcs], ic["keyframe_long_side"],
                seed, f"sheelshort/bkey_{clip['id']}"), must, at_most)

    # ---------- reference inputs: location libraries and blockouts ----------

    def cmd_locations(self, tag: bool = True) -> None:
        """Index every location's photo library and tag new photos with the servers' vision model."""
        lc = self.cfg.get("library", {})
        model = lc.get("vlm_model", "Qwen3.8-27B-Q4_K_M.gguf")
        jobs = []
        for loc in self.scene["locations"]:
            lib = library.refresh(self.inputs, loc)
            if not lib["entries"]:
                log(f"location {loc}: no library (inputs/locations/{loc}/ is empty); the generated plate is used")
                continue
            library.save(self.inputs, loc, lib)
            todo = [k for k, e in lib["entries"].items() if not e.get("tags")]
            log(f"location {loc}: {len(lib['entries'])} picture(s), {len(todo)} to tag")
            jobs += [(loc, k) for k in todo] if tag else []
        if not jobs:
            return

        def tag_one(item):
            loc, key = item
            e = library.load(self.inputs, loc)["entries"][key]
            job_key = f"vlm/{loc}/{e['sha1'][:12]}"
            files = self.run_job(job_key, lambda cl: flows.vlm_describe(
                self.put(cl, self.inputs / key), library.TAG_PROMPT, model, lc.get("vlm_mmproj", "Qwen3.8-mmproj-BF16.gguf"),
                lc.get("vlm_chat_handler", "Qwen3.8"), self.seed(job_key)),
                self.out / "library" / loc, e["sha1"][:12], ("text",), 900)
            if not files:
                return
            try:
                tags = library.parse_tags(files[0].read_text(encoding="utf-8"))
            except ValueError as err:
                log(f"{key}: could not read tags ({err}); tag it by hand in library.json")
                return
            with self.lock:
                lib = library.load(self.inputs, loc)
                lib["entries"][key].update(tags=tags, tagged_by=f"vlm:{model}")
                library.save(self.inputs, loc, lib)
            log(f"{key}: {tags['framing']}, {tags['time_of_day']}, people={tags['people']}, text={tags['visible_text']}: {tags['caption']}")

        with ThreadPoolExecutor(max_workers=1 if self.dry else len(self.cfg["comfy"]["servers"])) as pool:
            list(pool.map(tag_one, jobs))

    def cmd_blockout(self, clip_ids: list[str] | None, measure_only: bool = False) -> None:
        """Render clay/depth stills and an animatic per clip from its location's white-mesh set (Blender, headless).
        Every cut's camera view is also measured (angle, lens, shot size, who is where, set pieces in frame) for the
        prompt's camera text; `measure_only` re-measures without rendering (e.g. after relabelling set pieces)."""
        bc = self.cfg.get("blender", {})
        ids = clip_ids or list(self.clips)
        by_set: dict[Path, list[str]] = {}
        for cid in ids:
            sf = self.set_file(self.clips[cid]["location"])
            if sf is None:
                log(f"{cid}: location {self.clips[cid]['location']!r} has no blockout set file; skipped")
                continue
            by_set.setdefault(self.require(sf, "set file named in scene.json"), []).append(cid)
        if not by_set:
            return
        exe = blockout.find_blender(bc.get("exe") or None)
        mpath = self.out / "blockout" / "manifest.json"
        manifest = json.loads(mpath.read_text(encoding="utf-8")) if mpath.exists() else {"clips": {}}
        for sf, cids in by_set.items():
            spec = blockout.job(self.scene, cids, sf, self.out / "blockout", self.cfg["h3"].get("fps", 24),
                                bc.get("still_res", [720, 1280]), bc.get("animatic_res", [288, 512]),
                                bc.get("engine", "cycles"), bc.get("animatic_samples", 4), bc.get("animatic_fps", 8),
                                measure_only)
            t0 = time.time()
            got = blockout.render(exe, sf, spec, log, bc.get("timeout_s", 3600))
            if measure_only:
                for cid in cids:
                    old = manifest["clips"].get(cid)
                    if not old:
                        raise FileNotFoundError(f"{cid}: nothing rendered yet; run `blockout` without --measure-only")
                    for cut, new in zip(old["cuts"], got["clips"][cid]["cuts"]):
                        cut.update(view=new["view"], set_camera=new["set_camera"])
                log(f"blockout: re-measured {len(cids)} clip(s) in {time.time() - t0:.0f}s")
                continue
            manifest["clips"].update(got["clips"])
            manifest["fps"] = got["fps"]
            for cid in cids:
                cuts = got["clips"][cid]["cuts"]
                log(f"{cid}: animatic {got['clips'][cid]['animatic']} "
                    f"({'; '.join(c['how'] + ', ' + c['move'].get('kind', 'static') for c in cuts)})")
            log(f"blockout: {len(cids)} clip(s) in {time.time() - t0:.0f}s")
        mpath.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
        for cid in ids:   # clay stills over depth maps on one sheet, for review
            bo = manifest["clips"].get(cid)
            if bo:
                stills = [Path(v[kind]) for kind in ("clay", "depth") for v in bo["stills"].values()]
                media.tile(stills, self.out / "blockout" / f"{cid}_stills.jpg", cols=len(bo["stills"]))

    def cmd_fix_image(self, name: str, instruction: str) -> None:
        """Repair one generated image with a Qwen edit, keeping the original as <name>.orig.png."""
        src = self.out / "images" / f"{name}.png"
        self.require(src, "generate it first")
        backup = src.with_suffix(".orig.png")
        if not backup.exists():
            shutil.copy(src, backup)
        key = f"image/fix_{name}_{zlib.crc32(instruction.encode()):08x}"
        files = self.run_job(key, lambda cl: flows.edit(self.wf("edit"), instruction, [self.put(cl, backup)],
                                                         self.cfg["images"]["keyframe_long_side"], self.seed(key), f"sheelshort/fix_{name}"),
                             self.out / "images" / "fixes", key.split("/")[1], ("images",), 900)
        if files:
            shutil.copy(files[0], src)
            log(f"{name}: replaced with edited version (original kept at {backup.name})")

    # ---------- voices ----------

    def speakers(self) -> list[str]:
        return list(dict.fromkeys(cut["speaker"] for clip in self.scene["clips"] for cut in clip["cuts"] if cut.get("line")))

    def cmd_voices(self) -> None:
        design = None
        for c in self.speakers():
            dest = self.voice(c)
            rec = self.supplied(f"voice_{c}", AUDIO_EXTS)
            if rec:
                media.voice_reference(rec[0], dest, VOICE_MAX_S)
                log(f"voice {c}: from recording {rec[0].name} -> {dest}")
                continue
            if dest.exists():
                continue
            design = design or self.wf("voice_design")
            files = self.run_job(f"voice/{c}", lambda cl, c=c: flows.voice_design(
                design, self.chars[c]["voice_sample"], self.chars[c]["voice_design"], "Chinese", f"sheelshort/voice_{c}"),
                self.out / "voices" / "raw", c, ("audio",), 900)
            if files:
                media.voice_reference(files[0], dest, VOICE_MAX_S)
                log(f"voice {c}: designed -> {dest}")

    # ---------- clips ----------

    def refs(self, clip: dict, variant: str) -> tuple[h3prompt.Refs, dict[str, Path]]:
        """Reference labels for the prompt, and the local file behind each label."""
        cast = h3prompt.clip_characters(clip)
        files: dict[str, Path] = {}
        bo = None
        if variant in BLOCKOUT_VARIANTS:
            bo = self.blockout_clip(clip["id"])
            if not bo:
                raise FileNotFoundError(f"{clip['id']}: no blockout renders; run `blockout` first")
        if variant == "A":
            kf = self.require(self.keyframe(clip["id"]), "run `images` first")
            files[kf.name] = kf
            return h3prompt.Refs(keyframe=kf.name), files
        props = h3prompt.clip_props(clip)
        budget = 9 - 2 * len(cast) - len(props) - (1 if variant in KEYFRAME_VARIANTS else 0)
        if budget < 1:
            raise ValueError(f"{clip['id']}: too many characters/props for H3's 9 reference images")
        backgrounds: dict[int, str] = {}
        if variant in CAMERA_TEXT_VARIANTS:
            locs, backgrounds = self.pick_locations_by_view(clip, budget)
        else:
            locs = self.pick_locations(clip, budget)
        pack = {c: [self.require(self.portrait(c), "run `images` first"), self.require(self.sheet(c), "run `images` first")] for c in cast}
        prop_pics = {pr: [self.require(self.prop_picture(pr), "run `images` first")] for pr in props}
        refs = h3prompt.Refs(char_pictures={c: [p.name for p in ps] for c, ps in pack.items()},
                             prop_pictures={pr: [p.name for p in ps] for pr, ps in prop_pics.items()},
                             location_pictures=[p.name for p in locs], cut_backgrounds=backgrounds)
        files |= {p.name: p for ps in [*pack.values(), *prop_pics.values(), locs] for p in ps}
        verdict = self.state["jobs"].get(f"qc/bkey_{clip['id']}", {})
        if variant in KEYFRAME_VARIANTS and verdict and not verdict.get("passed"):
            log(f"{clip['id']}: blockout keyframe failed its vision check ({'; '.join(verdict['problems'])}); "
                "generating without it. Fix or replace inputs/bkey_<clip>.png to use one")
        elif variant in KEYFRAME_VARIANTS:
            kf = self.require(self.blockout_keyframe(clip["id"]), "run `images` with variant E or G enabled")
            refs.keyframe = kf.name
            files[kf.name] = kf
        if variant in ANIMATIC_VARIANTS:
            refs.animatic = f"animatic_{clip['id']}.mp4"
            files[refs.animatic] = Path(bo["animatic"])
        if variant in VOICE_VARIANTS:
            for c in h3prompt.speaker_order(clip):
                v = self.require(self.voice(c), "run `voices` first")
                refs.voices[c] = v.name
                files[v.name] = v
        return refs, files

    def view_context(self, clip: dict) -> tuple[dict, str, set[str]] | None:
        """The clip's measured blockout cameras, its location's look text, and every set piece seen at that location."""
        m = self.out / "blockout" / "manifest.json"
        if not m.exists():
            return None
        manifest = json.loads(m.read_text(encoding="utf-8"))
        if clip["id"] not in manifest["clips"] or not all("view" in c for c in manifest["clips"][clip["id"]]["cuts"]):
            return None
        loc = clip["location"]
        pieces = {p for cid, mc in manifest["clips"].items() if self.clips.get(cid, {}).get("location") == loc
                  for cut in mc["cuts"] for p in cut.get("view", {}).get("set_pieces", [])}
        return manifest["clips"][clip["id"]], self.scene["locations"][loc]["look"], pieces

    def camera_texts(self, clip: dict) -> dict[int, dict]:
        """Camera text per cut, measured from the clip's blockout (empty if it has none)."""
        ctx = self.view_context(clip)
        return blockout.camera_texts(ctx[0], clip, ctx[1], ctx[2]) if ctx else {}

    def prompt(self, clip: dict, variant: str, refs: h3prompt.Refs) -> str:
        override = self.root / "prompts" / f"{clip['id']}_{variant}.txt"
        cameras = self.camera_texts(clip) if variant in CAMERA_TEXT_VARIANTS else None
        if variant in CAMERA_TEXT_VARIANTS and not cameras:
            raise FileNotFoundError(f"{clip['id']}: no measured blockout cameras; run `blockout` first")
        text = override.read_text(encoding="utf-8") if override.exists() else h3prompt.build(self.scene, clip, refs, cameras)
        dest = self.out / "prompts" / f"{clip['id']}_{variant}.txt"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(text, encoding="utf-8")
        return text

    def clip_path(self, variant: str, clip_id: str) -> Path:
        return self.out / "clips" / variant / f"{clip_id}.mp4"

    def cmd_clips(self, variant: str, clip_ids: list[str] | None) -> None:
        errs = h3prompt.check_rules(self.scene)
        if errs:
            sys.exit("scene breaks the shot rules:\n  " + "\n  ".join(errs))
        h3 = self.wf("h3")
        hc = self.cfg["h3"]

        def one(clip_id: str) -> None:
            clip = self.clips[clip_id]
            refs, files = self.refs(clip, variant)
            text = self.prompt(clip, variant, refs)
            key = f"clip/{variant}/{clip_id}"

            def build(c):
                names = {name: self.put(c, path) for name, path in files.items()}
                return flows.h3_reference(h3, text, h3prompt.clip_len(clip), [names[n] for n in refs.images()],
                                          [names[n] for n in refs.audios()], self.seed(key), f"sheelshort/{variant}_{clip_id}",
                                          hc["aspect_ratio"], hc["megapixels"], [names[n] for n in refs.videos()])
            self.run_job(key, build, self.clip_path(variant, clip_id).parent, clip_id, ("gifs", "videos"), hc.get("timeout_s", 3600))

        ids = clip_ids or list(self.clips)
        with ThreadPoolExecutor(max_workers=1 if self.dry else len(self.cfg["comfy"]["servers"])) as pool:
            list(pool.map(one, ids))

    # ---------- eval ----------

    def eval_scene(self) -> dict:
        names = {c: v["name"] for c, v in self.chars.items()} | {"loc": "the location"}
        names |= {pr: f"the {v['name']}" for pr, v in self.scene.get("props", {}).items()}
        shots = [{"id": cid, "duration": h3prompt.clip_len(clip), "characters": h3prompt.clip_characters(clip),
                  "action": clip["summary"].format_map(names),
                  "dialogue": [{"char": cut["speaker"], "line": cut["line"]} for cut in clip["cuts"] if cut.get("line")]}
                 for cid, clip in self.clips.items()]
        return {"title": self.scene["title"], "language": self.scene.get("language", "zh-CN"), "shots": shots}

    def cmd_eval(self) -> None:
        clips_state = {}
        for key, job in self.state["jobs"].items():
            if key.startswith("clip/") and job.get("status") == "succeeded":
                _, variant, cid = key.split("/")
                clips_state[f"{variant}/{cid}"] = {"status": "succeeded", "file": job["files"][0], "elapsed_s": job.get("elapsed_s")}
        canon = {c: self.portrait(c) for c in self.chars}
        res = evaluate.evaluate(self.eval_scene(), self.out, {"clips": clips_state}, canon, self.cfg["spike"].get("whisper_model", "small"))
        for v in res["variants"]:
            paths = [self.clip_path(v, cid) for cid in self.clips]
            if all(p.exists() for p in paths):
                log(f"sequence {v}: {media.concat(paths, self.out / f'sequence_{v}.mp4')}")
            if v in BLOCKOUT_VARIANTS:   # animatic | generated clip, to judge whether H3 followed the blocking
                for cid in self.clips:
                    bo = self.blockout_clip(cid)
                    if bo and self.clip_path(v, cid).exists():
                        media.side_by_side([Path(bo["animatic"]), self.clip_path(v, cid)], self.out / f"compare_{v}_{cid}.mp4")
        for note in res["notes"]:
            log(f"note: {note}")
        log(f"report: {self.out / 'report.html'}")

    # ---------- doctor ----------

    def cmd_doctor(self) -> None:
        ok = True

        def check(label, fn):
            nonlocal ok
            try:
                log(f"OK   {label}: {fn()}")
            except Exception as e:  # noqa: BLE001
                ok = False
                log(f"FAIL {label}: {type(e).__name__}: {e}")

        check("ffmpeg/ffprobe", lambda: ", ".join(shutil.which(t) or f"{t} missing" for t in ("ffmpeg", "ffprobe")))

        def rules():
            errs = h3prompt.check_rules(self.scene)
            assert not errs, "; ".join(errs)
            return f"{len(self.clips)} clips, {sum(h3prompt.clip_len(c) for c in self.clips.values())}s, shot rules pass"
        check("scene", rules)

        samples = {
            "t2i": lambda: flows.t2i(self.wf("t2i"), "x", 720, 1280, 1, "sheelshort/doctor"),
            "edit": lambda: flows.edit(self.wf("edit"), "x", ["a.png", "b.png", "c.png"], 1280, 1, "sheelshort/doctor"),
            "voice_design": lambda: flows.voice_design(self.wf("voice_design"), "x", "x", "Chinese", "sheelshort/doctor"),
            "h3": lambda: flows.h3_reference(self.wf("h3"), "x", 12, ["a.png"], ["a.wav"], 1, "sheelshort/doctor", videos=["a.mp4"]),
            "vlm": lambda: flows.vlm_describe("a.png", "x", self.cfg.get("library", {}).get("vlm_model", "Qwen3.8-27B-Q4_K_M.gguf"),
                                              self.cfg.get("library", {}).get("vlm_mmproj", "Qwen3.8-mmproj-BF16.gguf"),
                                              self.cfg.get("library", {}).get("vlm_chat_handler", "Qwen3.8"), 1),
        }
        if any(loc.get("blockout") for loc in self.scene["locations"].values()):
            check("blender", lambda: blockout.find_blender(self.cfg.get("blender", {}).get("exe") or None))
        for url in self.cfg["comfy"]["servers"]:
            host = urlparse(url).hostname

            def server(url=url):
                c = self.client(url)
                stats = c.system_stats()
                gpus = ", ".join(f"{d['name'].split(':')[1].strip() if ':' in d['name'] else d['name']} "
                                 f"{d['vram_free'] / 2**30:.0f}/{d['vram_total'] / 2**30:.0f} GB free" for d in stats.get("devices", []))
                running, pending = c.queue_info()
                return f"ComfyUI {stats['system'].get('comfyui_version')}, {gpus}, queue {running} running / {pending} pending"
            check(f"server {host}", server)
            for name, make in samples.items():
                def wf_check(make=make, url=url):
                    problems = comfy.validate(make(), self.object_info(self.client(url)))
                    assert not problems, "; ".join(problems)
                    return "nodes and inputs valid"
                check(f"  workflow {name} on {host}", wf_check)

        log(f"INFO inputs: portraits {[c for c in self.chars if self.supplied(f'char_{c}', IMAGE_EXTS)] or 'generated'}, "
            f"location pictures {sum(len(library.files_for(self.inputs, l)) for l in self.scene['locations'])} supplied, "
            f"set files {[l for l, v in self.scene['locations'].items() if v.get('blockout')] or 'none'}, "
            f"voices {[c for c in self.speakers() if self.supplied(f'voice_{c}', AUDIO_EXTS)] or 'designed'}")
        for mod in ("faster_whisper", "resemblyzer", "insightface"):
            log(f"INFO optional {mod}: {'installed' if importlib.util.find_spec(mod) else 'not installed'}")
        log("doctor: all checks passed" if ok else "doctor: some checks failed")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, default=HERE / "config.toml")
    ap.add_argument("--out", type=Path)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor")
    p = sub.add_parser("locations", help="index location photo libraries and tag new photos with the vision model")
    p.add_argument("--no-tag", action="store_true", help="only index; leave tagging to the agent or a person")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("blockout", help="render clay/depth stills and animatics from white-mesh sets (Blender)")
    p.add_argument("--clips", help="comma-separated clip ids (default: all)")
    p.add_argument("--measure-only", action="store_true", help="re-measure camera views without rendering")
    p = sub.add_parser("images")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("voices")
    p.add_argument("--dry-run", action="store_true")
    p = sub.add_parser("clips")
    p.add_argument("--variant", required=True, choices=["A", "B", "C", "D", "E", "F", "G"])
    p.add_argument("--clips", help="comma-separated clip ids (default: all)")
    p.add_argument("--dry-run", action="store_true", help="write prompts and workflow requests without queueing")
    p = sub.add_parser("fix-image", help="repair a generated image (e.g. char_luchen) with a Qwen edit instruction")
    p.add_argument("name")
    p.add_argument("instruction")
    sub.add_parser("eval")
    sub.add_parser("all")
    args = ap.parse_args()

    if not args.config.exists():
        sys.exit(f"{args.config} not found; copy config.example.toml to config.toml and fill it in.")
    s = Spike(args.config, args.out, dry_run=getattr(args, "dry_run", False))
    variants = s.cfg["spike"]["variants"]
    match args.cmd:
        case "doctor":
            s.cmd_doctor()
        case "locations":
            s.cmd_locations(tag=not args.no_tag)
        case "blockout":
            s.cmd_blockout(args.clips.split(",") if args.clips else None, args.measure_only)
        case "images":
            s.cmd_images(variants)
        case "voices":
            s.cmd_voices()
        case "clips":
            s.cmd_clips(args.variant, args.clips.split(",") if args.clips else None)
        case "fix-image":
            s.cmd_fix_image(args.name, args.instruction)
        case "eval":
            s.cmd_eval()
        case "all":
            s.cmd_locations()
            if set(BLOCKOUT_VARIANTS) & set(variants):
                s.cmd_blockout(None)
            s.cmd_images(variants)
            if set(VOICE_VARIANTS) & set(variants):
                s.cmd_voices()
            for v in variants:
                s.cmd_clips(v, None)
            s.cmd_eval()


if __name__ == "__main__":
    main()
