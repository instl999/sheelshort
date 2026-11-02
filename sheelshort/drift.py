"""Drift across a series: is it still the same actor, the same colour, the same voice, episode after episode?

Per episode, from the takes in the edit (`drift/<EP>.json`):
- **identity**: each single-person shot's face (YuNet + SFace, OpenCV, run locally) against the character's close-up
  reference; the median cosine similarity per character. SFace's own threshold for "same person" is 0.363.
- **colour**: mean luma, black level (10th percentile luma) and white balance (mean U and V off neutral) of the
  master, 2 frames a second (ffmpeg signalstats).
- **voice**: each aligned line's median pitch against the speaker's cast voice, in semitones.

`drift.md` lays the episodes side by side and raises an alarm when a character's identity falls under the
threshold or well below its own series median, the colour leaves the series' running median, or a voice moves more
than two semitones from its cast voice: at EP5, not at EP50. The models live in `.cache/models/` (not committed;
`tools/install_face_models.py` puts them there).
"""
from __future__ import annotations

import math
import statistics as st
import subprocess
from pathlib import Path

from . import h3prompt, media
from .project import REPO, Project, load_json, log, save_json

MODELS = REPO / ".cache" / "models"
DETECTOR = "face_detection_yunet_2023mar.onnx"
RECOGNIZER = "face_recognition_sface_2021dec.onnx"
SAME_PERSON = 0.363      # SFace cosine similarity: OpenCV's published threshold
IDENTITY_DROP = 0.10     # a character this far under its own series median is drifting
LUMA_DRIFT = 8.0         # 8-bit luma levels from the series median
WB_DRIFT = 4.0           # U/V levels off the series median
VOICE_DRIFT_ST = 2.0     # semitones from the cast voice
FACE_SHOTS = ("ECU", "CU", "MCU", "MS", "RX", "OTS")


class Faces:
    def __init__(self):
        import cv2
        missing = [m for m in (DETECTOR, RECOGNIZER) if not (MODELS / m).exists()]
        if missing:
            raise FileNotFoundError(f"face models missing in {MODELS}: {missing} (python tools/install_face_models.py)")
        self.cv2 = cv2
        self.det = cv2.FaceDetectorYN.create(str(MODELS / DETECTOR), "", (320, 320), 0.7, 0.3, 5000)
        self.rec = cv2.FaceRecognizerSF.create(str(MODELS / RECOGNIZER), "")

    def embed(self, img):
        """The largest face's embedding, or None."""
        h, w = img.shape[:2]
        self.det.setInputSize((w, h))
        _, faces = self.det.detect(img)
        if faces is None or not len(faces):
            return None
        f = max(faces, key=lambda r: r[2] * r[3])
        return self.rec.feature(self.rec.alignCrop(img, f))

    def similarity(self, a, b) -> float:
        return float(self.rec.match(a, b, self.cv2.FaceRecognizerSF_FR_COSINE))


def _frame(video: Path, t: float):
    import cv2
    import numpy as np
    out = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{max(0, t):.3f}", "-i", str(video), "-frames:v", "1",
                          "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True)
    return cv2.imdecode(np.frombuffer(out.stdout, np.uint8), cv2.IMREAD_COLOR) if out.stdout else None


def identity(p: Project, ep: str, faces: Faces | None = None) -> dict:
    """Character -> {median, n, low: [[clip, cut, similarity]]} over the episode's single-person face shots."""
    import cv2
    from .assemble import chosen_take
    faces = faces or Faces()
    refs, scores = {}, {}
    for clip in p.clips(ep):
        try:
            video = chosen_take(p, ep, clip["id"])
        except FileNotFoundError:
            continue
        at = 0.0
        for n, cut in enumerate(clip["cuts"], 1):
            d = h3prompt.cut_len(cut)
            subj = [c for c in cut.get("subjects", []) if c in p.chars]
            if len(subj) == 1 and cut.get("type") in FACE_SHOTS:
                c = p.base(subj[0])
                if c not in refs:
                    ref = p.asset(f"char_{c}")
                    img = cv2.imread(str(ref)) if ref.exists() else None
                    refs[c] = faces.embed(img) if img is not None else None
                frame = _frame(video, at + d / 2)
                e = faces.embed(frame) if frame is not None and refs[c] is not None else None
                if e is not None:
                    scores.setdefault(c, []).append((clip["id"], n, round(faces.similarity(refs[c], e), 3)))
            at += d
    return {c: {"median": round(st.median(s for *_, s in v), 3), "n": len(v),
                "low": [list(x) for x in v if x[2] < SAME_PERSON]} for c, v in scores.items()}


def colour(video: Path) -> dict:
    """Mean luma, black level and white balance (U, V off 128) of a video, 2 frames a second."""
    out = subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-vf", "fps=2,signalstats,metadata=print:file=-",
                          "-f", "null", "-"], capture_output=True, text=True).stdout
    vals: dict[str, list[float]] = {}
    for line in out.splitlines():
        if "lavfi.signalstats." in line:
            k, _, v = line.partition("lavfi.signalstats.")[2].partition("=")
            if k in ("YAVG", "YLOW", "UAVG", "VAVG"):
                vals.setdefault(k, []).append(float(v))
    if not vals.get("YAVG"):
        return {}
    return {"luma": round(st.mean(vals["YAVG"]), 1), "black": round(st.mean(vals["YLOW"]), 1),
            "u": round(st.mean(vals["UAVG"]) - 128, 2), "v": round(st.mean(vals["VAVG"]) - 128, 2)}


def voice(p: Project, ep: str) -> dict:
    """Speaker -> {median_st (from the cast voice), n}: each line H3 voiced, found in the take by its words."""
    from . import speech
    from .assemble import chosen_take, heard_lines
    shifts = {}
    for clip in p.clips(ep):
        try:
            video = chosen_take(p, ep, clip["id"])
        except FileNotFoundError:
            continue
        speakers = [cut.get("speaker") for cut in clip["cuts"] if cut.get("line")]
        for who, ln in zip(speakers, heard_lines(p, ep, clip, video)):
            if not who or not ln.get("aligned"):
                continue
            meta = p.voice(who).with_suffix(".json")
            cast_hz = load_json(meta).get("median_hz") if meta.exists() else None
            if not cast_hz:
                continue
            wav = p.d("drift", "_work") / f"{video.stem}_{ln['start']:.2f}.wav"
            media.extract_audio(video, wav, ln["start"], ln["end"])
            hz = speech.profile(wav, ref_hz=cast_hz).get("median_hz")
            wav.unlink(missing_ok=True)
            if hz:
                shifts.setdefault(p.base(who), []).append(12 * math.log2(hz / cast_hz))
    return {c: {"median_st": round(st.median(v), 2), "n": len(v)} for c, v in shifts.items()}


def episode(p: Project, ep: str) -> dict:
    """Measure one assembled episode; drift/<EP>.json."""
    master = p.root / "edit" / ep / f"{p.master_name()}.mp4"
    try:
        ident = identity(p, ep)
    except (FileNotFoundError, ImportError) as e:
        log(f"drift {ep}: identity skipped ({e})")
        ident = {}
    data = {"identity": ident, "colour": colour(master) if master.exists() else {},
            "voice": voice(p, ep) if p.cfg["render"]["dialogue"] == "h3" else {}}
    save_json(p.d("drift") / f"{ep}.json", data)
    return data


def alarms(p: Project) -> dict[str, list[str]]:
    """Episode -> alarms, judged against the series so far (running medians of the episodes before it)."""
    from .project import episode_key
    rows = sorted(((f.stem, load_json(f)) for f in (p.root / "drift").glob("*.json")), key=lambda r: episode_key(r[0]))
    out, seen_id, seen_col = {}, {}, []
    for ep, d in rows:
        a = []
        for c, v in d.get("identity", {}).items():
            if v["median"] < SAME_PERSON:
                a.append(f"{c}: face {v['median']:.2f}, under the same-person threshold {SAME_PERSON} ({v['n']} shots"
                         + (f"; lowest {', '.join(f'{x[0]} cut {x[1]}' for x in v['low'][:4])}" if v["low"] else "") + ")")
            elif len(seen_id.get(c, [])) >= 2 and v["median"] < st.median(seen_id[c]) - IDENTITY_DROP:
                a.append(f"{c}: face {v['median']:.2f}, {st.median(seen_id[c]) - v['median']:.2f} under its series median")
            seen_id.setdefault(c, []).append(v["median"])
        col = d.get("colour") or {}
        if col and len(seen_col) >= 2:
            ref = {k: st.median(x[k] for x in seen_col) for k in ("luma", "black", "u", "v")}
            if abs(col["luma"] - ref["luma"]) > LUMA_DRIFT:
                a.append(f"colour: mean luma {col['luma']:.0f} vs the series' {ref['luma']:.0f}")
            if abs(col["black"] - ref["black"]) > LUMA_DRIFT:
                a.append(f"colour: black level {col['black']:.0f} vs the series' {ref['black']:.0f}")
            if max(abs(col["u"] - ref["u"]), abs(col["v"] - ref["v"])) > WB_DRIFT:
                a.append(f"colour: white balance U {col['u']:+.1f} V {col['v']:+.1f} vs the series' "
                         f"U {ref['u']:+.1f} V {ref['v']:+.1f}")
        if col:
            seen_col.append(col)
        for c, v in d.get("voice", {}).items():
            if abs(v["median_st"]) > VOICE_DRIFT_ST:
                a.append(f"{c}: voice {v['median_st']:+.1f} semitones from the cast voice ({v['n']} lines)")
        out[ep] = a
    return out


def write(p: Project) -> Path:
    from .project import episode_key
    rows = sorted(((f.stem, load_json(f)) for f in (p.root / "drift").glob("*.json")), key=lambda r: episode_key(r[0]))
    chars = sorted({c for _, d in rows for c in list(d.get("identity", {})) + list(d.get("voice", {}))})
    al = alarms(p)
    head = ["episode"] + [f"{c} face" for c in chars] + [f"{c} voice st" for c in chars] + ["luma", "black", "U", "V", "alarms"]
    lines = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    for ep, d in rows:
        idn, vo, col = d.get("identity", {}), d.get("voice", {}), d.get("colour", {})
        cells = [ep] + [f"{idn[c]['median']:.2f}" if c in idn else "" for c in chars] \
            + [f"{vo[c]['median_st']:+.1f}" if c in vo else "" for c in chars] \
            + [f"{col[k]:.1f}" if k in col else "" for k in ("luma", "black", "u", "v")] + ["; ".join(al.get(ep, [])) or "-"]
        lines.append("| " + " | ".join(cells) + " |")
    dest = p.root / "drift.md"
    dest.write_text(f"# Drift across the series, {p.root.name}\n\nFace: median SFace similarity of single-person shots "
                    f"to the character's close-up (same person from {SAME_PERSON}). Voice: median pitch against the "
                    "cast voice, in semitones. Colour: the master's mean luma, black level and white balance.\n\n"
                    + "\n".join(lines) + "\n", encoding="utf-8")
    return dest
