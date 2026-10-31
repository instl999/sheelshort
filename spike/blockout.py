"""White-mesh (blockout) shot references: scene.json cuts -> Blender job -> clay/depth stills and clip animatics.

The Blender side is blender/render_shots.py; this module builds its job, runs Blender headless and turns the
rendered frames into one animatic per clip (same length and cut timing as the H3 clip).
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import h3prompt
import library
import media

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "blender" / "render_shots.py"
SET_EXTS = (".blend", ".glb", ".gltf", ".fbx", ".obj")

# camera text from the storyboard -> move; the first match wins
MOVES = [
    (r"push(es|ing)? in|dolly in|move[sd]? in|creep(s|ing)? in", "push_in"),
    (r"pull(s|ing)? (back|out)|dolly (back|out)", "pull_out"),
    (r"(track|truck|slide|dolly)\w* (\w+ )?(to the )?right|lateral track \w* ?to the right", "truck_right"),
    (r"(track|truck|slide|dolly)\w* (\w+ )?(to the )?left|lateral track \w* ?to the left", "truck_left"),
    (r"pan(s|ning)? (to the )?left", "pan_left"),
    (r"pan(s|ning)? (to the )?right", "pan_right"),
    (r"(orbit|arc)\w* (to the )?left", "orbit_left"),
    (r"(orbit|arc)\w* (to the )?right", "orbit_right"),
    (r"tilt(s|ing)? up", "tilt_up"),
    (r"tilt(s|ing)? down", "tilt_down"),
    (r"handheld", "handheld"),
]


def parse_move(cut: dict) -> dict:
    """A cut's camera move: its explicit `move`, else read from the camera text."""
    if isinstance(cut.get("move"), dict):
        return cut["move"]
    if isinstance(cut.get("move"), str):
        return {"kind": cut["move"], "amount": "small"}
    text = (cut.get("camera") or "").lower()
    amount = "small" if re.search(r"small amplitude|very slow|barely|subtle|slight", text) else "normal"
    for pattern, kind in MOVES:
        if re.search(pattern, text):
            return {"kind": kind, "amount": amount}
    return {"kind": "static", "amount": amount}


def find_blender(configured: str | None = None) -> str:
    candidates = [configured, os.environ.get("BLENDER"), shutil.which("blender")]
    candidates += sorted(glob.glob(r"C:\Program Files\Blender Foundation\Blender *\blender.exe"), reverse=True)
    candidates += ["/Applications/Blender.app/Contents/MacOS/Blender"]
    for c in candidates:
        if c and Path(c).exists():
            return c
    raise FileNotFoundError("Blender not found; install it or set [blender] exe in config.toml / the BLENDER env var")


def job(scene: dict, clip_ids: list[str], set_path: Path, out: Path, fps: int, still_res, animatic_res,
        engine: str = "cycles", animatic_samples: int = 6, animatic_fps: int = 8, measure_only: bool = False) -> dict:
    """Blender job for the given clips. The animatic renders every (fps // animatic_fps)-th frame; ffmpeg
    retimes it to `fps`, so camera moves keep their real speed while CPU rendering stays affordable."""
    clips = {c["id"]: c for c in scene["clips"]}
    return {
        "out": str(out.resolve()),
        "import": str(set_path.resolve()) if set_path.suffix.lower() != ".blend" else None,
        "fps": fps,
        "animatic_step": max(1, fps // animatic_fps),
        "measure_only": measure_only,
        "engine": engine,
        "animatic_samples": animatic_samples,
        "still_res": list(still_res),
        "animatic_res": list(animatic_res),
        "clips": [{
            "id": cid,
            "cast": h3prompt.clip_characters(clips[cid]),
            "cuts": [{"type": cut["type"], "subjects": cut.get("subjects", []), "over": cut.get("over"),
                      "dur": h3prompt.cut_len(cut), "move": parse_move(cut)} for cut in clips[cid]["cuts"]],
        } for cid in clip_ids],
    }


def render(blender: str, set_path: Path, spec: dict, log=print, timeout_s: float = 1800) -> dict:
    """Run Blender headless for every clip in the job; return the manifest with animatic paths added."""
    out = Path(spec["out"])
    out.mkdir(parents=True, exist_ok=True)
    job_file = out / "job.json"
    job_file.write_text(json.dumps(spec, ensure_ascii=False, indent=1), encoding="utf-8")
    args = [blender, "-b"] + ([str(set_path.resolve())] if set_path.suffix.lower() == ".blend" else []) + [
        "--factory-startup", "-noaudio", "-P", str(SCRIPT), "--", str(job_file)]
    log(f"blockout: rendering {len(spec['clips'])} clip(s) from {set_path.name}")
    p = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout_s)
    (out / "blender.log").write_text(p.stdout + p.stderr, encoding="utf-8")
    if p.returncode != 0 or "render_shots: done" not in p.stdout:
        tail = "\n".join((p.stdout + p.stderr).strip().splitlines()[-25:])
        raise RuntimeError(f"Blender failed (exit {p.returncode}); see {out / 'blender.log'}\n{tail}")
    if spec.get("measure_only"):
        return json.loads((out / "views.json").read_text(encoding="utf-8"))
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    step = spec["animatic_step"]
    for cid, meta in manifest["clips"].items():
        seq = out / cid / "seq"
        shutil.rmtree(seq, ignore_errors=True)
        seq.mkdir()
        for i, f in enumerate(sorted((out / cid / "frames").glob("*.png")), 1):
            shutil.copy(f, seq / f"{i:04d}.png")
        meta["animatic"] = str(media.image_sequence(seq / "%04d.png", out / f"{cid}_animatic.mp4", spec["fps"] / step, spec["fps"]))
        meta["stills"] = {f"cut{c['cut']}": {"clay": str(out / cid / f"cut{c['cut']}_clay.png"),
                                             "depth": str(out / cid / f"cut{c['cut']}_depth.png")} for c in meta["cuts"]}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


# ---------- camera text: what a blockout camera shows, written for the H3 prompt ----------

def _shot_size(frame_h: float) -> str:
    for limit, name in ((0.32, "an extreme close-up"), (0.7, "a close-up"), (1.1, "a medium close-up"),
                        (1.6, "a medium shot"), (3.2, "a full shot")):
        if frame_h < limit:
            return name
    return "a wide shot"


def _angle(pitch: float, height: float) -> str:
    where = f" from {height:.1f} m up, near the ceiling," if height > 2.5 else " from below chest height," if height < 1.0 else ""
    if pitch <= -20:
        return f"a high angle{where} looking steeply down (about {abs(pitch):.0f} degrees)"
    if pitch <= -8:
        return f"a slightly high angle{where} looking down"
    if pitch >= 20:
        return f"a low angle{where} looking steeply up (about {pitch:.0f} degrees)"
    if pitch >= 8:
        return f"a slightly low angle{where} looking up"
    return "eye level"


def _lens(lens: float) -> str:
    mm = f"{lens:.0f} mm"
    article = "an" if mm.startswith(("8", "11 ", "18 ")) else "a"
    if lens < 24:
        return f"{article} {mm} wide-angle lens with strong perspective"
    if lens < 40:
        return f"{article} {mm} lens"
    if lens <= 60:
        return f"{article} {mm} normal lens"
    return f"{article} {mm} telephoto portrait lens with compressed background"


def _where(x: float) -> str:
    if x < 0.12:
        return "at the left edge of the frame"
    if x > 0.88:
        return "at the right edge of the frame"
    return "in the left third" if x < 0.38 else "in the right third" if x > 0.62 else "in the centre"


def _scale(frame_h: float) -> str:
    if frame_h >= 2.6:
        return "small in the frame, full figure"
    if frame_h >= 1.6:
        return "full figure"
    if frame_h >= 0.9:
        return "seen from the waist up"
    if frame_h >= 0.45:
        return "head and shoulders"
    return "face filling the frame"


def _facing(deg: float, looks: str) -> str:
    if deg < 30:
        return "facing the camera" + (f", eyes toward screen {looks}" if looks != "camera" else "")
    if deg < 70:
        return f"in three-quarter view looking toward screen {looks}"
    if deg < 115:
        return f"in profile looking toward screen {looks}"
    if deg < 150:
        return "turned mostly away from the camera"
    return "with the back to the camera"


def _move(m: dict) -> str:
    d = max(m["subject_distance_m"], 0.5)
    parts = []
    if abs(m["forward_m"]) > 0.04 * d:
        parts.append("slowly pushes in" if m["forward_m"] > 0 else "slowly pulls back")
    if abs(m["right_m"]) > 0.1:
        parts.append(f"slowly tracks {'right' if m['right_m'] > 0 else 'left'}")
    if abs(m["up_m"]) > 0.1:
        parts.append(f"slowly cranes {'up' if m['up_m'] > 0 else 'down'}")
    if abs(m["pan_deg"]) > 3:
        parts.append(f"slowly pans {'right' if m['pan_deg'] > 0 else 'left'}")
    if abs(m["tilt_deg"]) > 3:
        parts.append(f"slowly tilts {'up' if m['tilt_deg'] > 0 else 'down'}")
    if abs(m["roll_deg"]) > 3:
        parts.append("slowly rolls")
    return ("The camera " + " and ".join(parts) + ", with small amplitude, at slow speed.") if parts else "Static camera."


def camera_texts(manifest_clip: dict, clip: dict, location_look: str, all_set_pieces: set[str]) -> dict[int, dict]:
    """Per cut (1-based): prompt text for the blockout camera.

    A camera placed by hand in the set file is the director's intent, so its text *replaces* the cut's written
    composition and camera move ("replace": True; shot size included). An automatic camera already follows the
    written composition, so only angle, lens and eyelines are added. Names use {char_id} placeholders."""
    look = location_look.lower()
    out = {}
    for meta, cut in zip(manifest_clip["cuts"], clip["cuts"]):
        v = meta.get("view")
        if not v:
            continue
        subs = {c: s for c, s in v["subjects"].items() if s["in_frame"] and s["depth_m"] > 0}
        nearest = min((s["frame_height_m"] for s in subs.values()), default=None)
        size = _shot_size(nearest) if nearest else "a shot"
        angle, lens = _angle(v["pitch_deg"], v["height_m"]), _lens(v["lens"])
        dutch = f" with a strong {abs(v['roll_deg']):.0f}-degree Dutch tilt, the whole frame rotated so verticals lean" \
            if abs(v["roll_deg"]) > 8 else ""
        if not meta.get("set_camera"):
            eyes = "; ".join(f"{{{c}}} looks toward screen {s['looks_screen']}" for c, s in subs.items()
                             if s["looks_screen"] != "camera")
            out[meta["cut"]] = {"replace": False,
                                "text": f"Camera at {angle}{dutch} with {lens}." + (f" {eyes[0].upper()}{eyes[1:]}." if eyes else "")}
            continue
        people = []
        for c, s in sorted(subs.items(), key=lambda kv: kv[1]["x"]):
            people.append(f"{{{c}}} is {_where(s['x'])}, {_scale(s['frame_height_m'])}, {_facing(s['facing_deg'], s['looks_screen'])}")
        shown = v["set_pieces"]
        missing = [p for p in sorted(all_set_pieces - set(shown)) if _mentioned(p, look)]
        text = f"The camera is at {angle}{dutch}, through {lens}."
        if people:
            text += " " + "; ".join(people) + "."
        if shown:
            text += f" In frame: the {', the '.join(shown)}."
        if missing:
            text += f" Not in frame: the {', the '.join(missing)}."
        out[meta["cut"]] = {"replace": True, "size": size, "text": text, "move": _move(v["move"])}
    return out


def _mentioned(piece: str, look: str) -> bool:
    return any(w in look for w in piece.split() if len(w) > 3)


def view_wants(manifest_clip: dict, clip: dict, location_look: str, all_set_pieces: set[str]) -> list[tuple[str, set[str], set[str]]]:
    """Per cut, what its background should and should not show, from the measured camera view:
    (shot type, words of set pieces in frame, words of pieces the location is known for that are out of frame)."""
    look = location_look.lower()
    out = []
    for meta, cut in zip(manifest_clip["cuts"], clip["cuts"]):
        v = meta.get("view") or {}
        shown = set(v.get("set_pieces", []))
        missing = {p for p in all_set_pieces - shown if _mentioned(p, look)}
        out.append((cut["type"], library.words(" ".join(shown)), library.words(" ".join(missing))))
    return out
