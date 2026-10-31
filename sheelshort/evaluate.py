"""A/B the dialogue of rendered clips line by line: acted (rendered to the performed track) vs. h3 (H3 voices itself).

Each line is measured inside its own cut window of the delivered clip's audio (one speaker per window), so the two
modes are compared on what a viewer hears: expressiveness (pitch motion), pitch against the cast voice, and words.
For acted clips it also checks that the render kept the locked track (envelope correlation with the drive track).
"""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

import numpy as np

from . import h3prompt, speech
from .project import Project, load_json, log, save_json


def _segment(video: Path, start: float, end: float, dest: Path) -> Path:
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(video),
                    "-vn", "-ac", "1", "-ar", str(speech.SR), str(dest)], check=True)
    return dest


def envelope_corr(a: Path, b: Path) -> float:
    ya, yb = speech.load(a), speech.load(b)
    n = min(len(ya), len(yb))
    ea = np.sqrt(np.convolve(ya[:n] ** 2, np.ones(800) / 800, mode="same"))[::160]
    eb = np.sqrt(np.convolve(yb[:n] ** 2, np.ones(800) / 800, mode="same"))[::160]
    return round(float(np.corrcoef(ea, eb)[0, 1]), 3)


def windows(p: Project, ep: str, clip: dict, mode: str) -> list[dict]:
    """(cut, speaker, line, start, end) of every line, from the performed timing or the written cut lengths."""
    if mode == "acted":
        tm = load_json(p.root / "perform" / ep / "timing.json")[clip["id"]]
        cuts = tm["cuts"]
    else:
        t, cuts = 0.0, []
        for cut in clip["cuts"]:
            cuts.append({"start": t, "dur": h3prompt.cut_len(cut)})
            t += h3prompt.cut_len(cut)
    out = []
    for n, (cut, c) in enumerate(zip(clip["cuts"], cuts), 1):
        if cut.get("line"):
            out.append({"cut": n, "speaker": cut["speaker"], "line": cut.get("say") or cut["line"],
                        "start": c["start"], "end": c["start"] + c["dur"]})
    return out


def run(p: Project, ep: str, take: int = 1) -> dict:
    lang, wm = p.cfg["project"]["language"], p.cfg["perform"]["whisper_model"]
    res = {}
    for mode, suffix in (("acted", ""), ("h3", "_h3")):
        rows = []
        for clip in p.clips(ep):
            video = p.root / "clips" / ep / f"{clip['id']}{suffix}_t{take}.mp4"
            if not video.exists():
                continue
            with tempfile.TemporaryDirectory() as tmp:
                if mode == "acted":
                    track = load_json(p.root / "perform" / ep / "timing.json")[clip["id"]]["track"]
                    whole = _segment(video, 0, 999, Path(tmp) / "whole.wav")
                    lock = envelope_corr(whole, Path(track)) if track else None
                else:
                    lock = None
                wins = windows(p, ep, clip, mode)
                if mode == "h3":
                    # H3 places speech freely, so find each line by its recognised words instead of its cut
                    whole = _segment(video, 0, 999, Path(tmp) / "whole.wav")
                    spans = speech.locate([w["line"] for w in wins], speech.words(whole, lang, wm))
                    for w, sp in zip(wins, spans):
                        if sp:
                            w["start"], w["end"] = max(0, sp[0] - 0.05), sp[1] + 0.1
                for w in wins:
                    seg = _segment(video, w["start"], w["end"], Path(tmp) / f"{w['cut']}.wav")
                    cast_hz = load_json(p.voice(w["speaker"]).with_suffix(".json"))["median_hz"]
                    prof = speech.profile(seg, cast_hz)
                    heard = speech.transcribe(seg, lang, wm)
                    rows.append({"clip": clip["id"], **w, "motion": prof["motion"], "range_st": prof["range_st"],
                                 "pitch_ratio": round((prof["median_hz"] or 0) / cast_hz, 3), "heard": heard,
                                 "cer": round(speech.error_rate(w["line"], heard), 3), "lock_corr": lock})
        if rows:
            res[mode] = {"lines": rows, "median_motion": float(np.median([r["motion"] for r in rows])),
                         "mean_cer": float(np.mean([r["cer"] for r in rows]))}
            log(f"{mode}: median motion {res[mode]['median_motion']:.2f}, mean CER {res[mode]['mean_cer']:.2f} over {len(rows)} lines")
    save_json(p.d("edit", ep) / "dialogue_ab.json", res)
    md = ["| line | acted motion | h3 motion | acted pitch× | h3 pitch× | acted CER | h3 CER |", "|---|---|---|---|---|---|---|"]
    by = {m: {(r["clip"], r["cut"]): r for r in v["lines"]} for m, v in res.items()}
    for key, r in by.get("acted", by.get("h3", {})).items():
        a, h = by.get("acted", {}).get(key, {}), by.get("h3", {}).get(key, {})
        md.append(f"| {key[0]}/{key[1]} {r['speaker']} 「{r['line']}」 | {a.get('motion', '–')} | {h.get('motion', '–')} | "
                  f"{a.get('pitch_ratio', '–')} | {h.get('pitch_ratio', '–')} | {a.get('cer', '–')} | {h.get('cer', '–')} |")
    (p.root / "edit" / ep / "dialogue_ab.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    return res
