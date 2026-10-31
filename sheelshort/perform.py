"""Performance first: act every line, choose the best take, and lay out each clip's dialogue track.

Why: H3 voices lines flat. drama-studio measured it over several episodes; the emotion reached the faces but not the
voices. Its acted voice *references* helped only partly, because H3 still re-speaks the line itself. Here the
dialogue is decided before any picture exists:

1. every line is acted by Fish Audio Drama 3 in the character's cast voice, with the episode's acting direction as an
   inline tag, `perform.takes` times;
2. every take is measured: the words (ASR character error rate against the line), voice identity (median pitch
   against the cast voice, in a band that is wider for crying, shouting, fear and pleading), and expressiveness
   (semitones the pitch moves between syllables; capped for calm lines so a calm line stays calm);
3. the best passing take per line is kept and trimmed to its speech;
4. per clip, the lines are laid out in their cuts: each cut is lengthened to hold its line (lead-in, line, breath),
   so **shot timing comes from the performance**, and the clip's dialogue track is written with exact line times
   (they become the subtitles).

render.py then renders the picture around that track (lips follow it) and assemble.py uses the clean track itself.
"""
from __future__ import annotations

import math
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import flows, h3prompt, speech
from .project import Project, load_json, log, save_json

INTENSE = {"sob", "cry", "crying", "anger", "angry", "shout", "fear", "plead", "shock", "broken", "panic", "rage", "despair"}
CALM = {"calm", "cold", "firm", "neutral", "warm", "flat", "whisper", "tired"}
TAKE_RATE = 48000


def lines(p: Project, ep: str) -> list[dict]:
    """Every spoken line of an episode with its place: clip, cut (1-based), speaker, text."""
    out = []
    for clip in p.clips(ep):
        for n, cut in enumerate(clip["cuts"], 1):
            if cut.get("line"):
                out.append({"key": f"{clip['id']}_{n}", "clip": clip["id"], "cut": n, "speaker": cut["speaker"],
                            "line": cut["line"], "say": cut.get("say") or cut["line"], "act": cut.get("act", ""),
                            "emotion": (cut.get("emotion") or "").lower(), "type": cut["type"]})
    return out


def drama3_text(ln: dict) -> str:
    return f"[{ln['act']}] {ln['say']}" if ln["act"] else ln["say"]


def takes(p: Project, ep: str, only: set[str] | None = None) -> None:
    """Generate `perform.takes` Drama 3 takes per line (resumable; done takes are kept)."""
    n = int(p.cfg["perform"]["takes"])
    jobs = []
    for ln in lines(p, ep):
        if only and ln["key"] not in only and ln["clip"] not in only:
            continue
        voice = p.voice(ln["speaker"])
        meta = voice.with_suffix(".json")
        if not (voice.exists() and meta.exists()):
            raise FileNotFoundError(f"{ln['speaker']} has no cast voice; run `sheelshort cast` first")
        transcript = load_json(meta)["transcript"]
        out = p.d("perform", ep, "takes")
        for t in range(1, n + 1):
            key = f"perform/{ep}/{ln['key']}_t{t}"
            if any(out.glob(f"{ln['key']}_t{t}.*")):
                continue
            jobs.append(lambda key=key, ln=ln, t=t, voice=voice, transcript=transcript, out=out: p.run_job(
                key, lambda cl: flows.drama3(p.put(cl, voice), transcript, drama3_text(ln), p.seed(key) + t,
                                             f"sheelshort/{ep}_{ln['key']}_t{t}", max_frames=_max_frames(ln)),
                out, f"{ln['key']}_t{t}", ("audio",), 1800))
    with ThreadPoolExecutor(max(1, len(p.cfg["comfy"]["servers"]) * 2)) as ex:
        list(ex.map(lambda f: f(), jobs))


def _max_frames(ln: dict) -> int:
    # Drama 3 makes ~21.5 frames per second of audio; allow 3x the natural length of the line plus a breath.
    return int(min(800, max(120, (h3prompt.spoken_len(ln["say"], "zh") * 3 + 2) * 21.5)))


def score(p: Project, ln: dict, path: Path, cast_hz: float) -> dict:
    """Measure one take of one line and decide whether it may be used."""
    pc = p.cfg["perform"]
    prof = speech.profile(path, cast_hz)
    heard = speech.transcribe(path, p.cfg["project"]["language"], pc["whisper_model"])
    cer = speech.error_rate(ln["say"], heard)
    ratio = (prof["median_hz"] or 0) / cast_hz if cast_hz else 1.0
    intense = ln["emotion"] in INTENSE
    lo, hi = pc["pitch_band_intense"] if intense else pc["pitch_band"]
    natural = h3prompt.spoken_len(ln["say"], "zh")
    problems = []
    if cer > pc["max_cer"]:
        problems.append(f"words: heard {heard!r} (CER {cer:.2f})")
    if prof["median_hz"] and not lo <= ratio <= hi:
        problems.append(f"voice: pitch x{ratio:.2f} of the cast voice (band {lo}-{hi})")
    if prof["speech_s"] < 0.3:
        problems.append("silent take")
    if prof["speech_s"] > max(2.5, natural * 2.4 + 1.0):
        problems.append(f"too slow: {prof['speech_s']:.1f}s for a ~{natural:.1f}s line")
    cap = 2.2 if ln["emotion"] in CALM else 6.0
    value = min(prof["motion"], cap) + 0.15 * min(prof["loud_std_db"], 8) - 3.0 * cer
    return {**prof, "file": str(path), "heard": heard, "cer": round(cer, 3), "pitch_ratio": round(ratio, 3),
            "ok": not problems, "problems": problems, "score": round(value, 3)}


def choose(p: Project, ep: str) -> dict:
    """Score every take, keep the best passing one per line, trim it to its speech. Writes perform/<EP>/choice.json."""
    out_dir = p.d("perform", ep)
    choice_path = out_dir / "choice.json"
    choice = load_json(choice_path) if choice_path.exists() else {}
    for ln in lines(p, ep):
        files = sorted((out_dir / "takes").glob(f"{ln['key']}_t*.*"))
        if not files:
            log(f"{ln['key']}: no takes yet")
            continue
        cast_hz = load_json(p.voice(ln["speaker"]).with_suffix(".json"))["median_hz"]
        prev = {s["file"]: s for s in choice.get(ln["key"], {}).get("takes", [])}
        scored = [prev.get(str(f)) or score(p, ln, f, cast_hz) for f in files]
        ok = [s for s in scored if s["ok"]]
        best = max(ok, key=lambda s: s["score"]) if ok else min(scored, key=lambda s: (s["cer"], abs(1 - s["pitch_ratio"])))
        dest = out_dir / f"{ln['key']}.wav"
        trim(Path(best["file"]), dest, best["speech_start"], best["speech_end"])
        choice[ln["key"]] = {**{k: ln[k] for k in ("clip", "cut", "speaker", "line", "say", "act", "emotion")},
                             "chosen": best["file"], "passed": bool(ok), "wav": str(dest),
                             "speech_s": round(best["speech_end"] - best["speech_start"] + 0.08, 3), "takes": scored}
        flag = "" if ok else "  ** no take passed: " + "; ".join(best["problems"])
        log(f"{ln['key']} {ln['speaker']} 「{ln['say']}」 -> {Path(best['file']).name}: motion {best['motion']}, "
            f"pitch x{best['pitch_ratio']}, CER {best['cer']} ({len(ok)}/{len(scored)} passed){flag}")
    save_json(choice_path, choice)
    return choice


def trim(src: Path, dest: Path, start: float, end: float) -> None:
    a, b = max(0.0, start - 0.04), end + 0.04
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-ss", f"{a:.3f}", "-to", f"{b:.3f}",
                    "-af", "afade=t=in:d=0.02,areverse,afade=t=in:d=0.04,areverse", "-ac", "1", "-ar", str(TAKE_RATE),
                    str(dest)], check=True)


def layout(p: Project, ep: str) -> dict:
    """Retime every clip's cuts around its chosen lines and write one dialogue track per clip.

    Writes perform/<EP>/<clip>.wav (the drive track, padded to whole seconds) and perform/<EP>/timing.json:
    per clip the H3 length in seconds, each cut's start and duration, and each line's exact start and end."""
    pc = p.cfg["perform"]
    choice = load_json(p.root / "perform" / ep / "choice.json")
    tpath = p.root / "perform" / ep / "timing.json"
    timing = load_json(tpath) if tpath.exists() else {}   # test mode lays out only some clips: keep the others
    for clip in p.clips(ep):
        t, cuts, placed = 0.0, [], []
        for n, cut in enumerate(clip["cuts"], 1):
            dur = h3prompt.cut_len(cut)
            c = choice.get(f"{clip['id']}_{n}")
            if cut.get("line") and c:
                start = t + pc["lead_s"]
                dur = max(dur, pc["lead_s"] + c["speech_s"] + pc["tail_s"])
                placed.append({"cut": n, "speaker": c["speaker"], "text": c["line"], "start": round(start, 3),
                               "end": round(start + c["speech_s"], 3), "wav": c["wav"]})
            cuts.append({"cut": n, "start": round(t, 3), "dur": round(dur, 3)})
            t += dur
        seconds = max(4, math.ceil(t - 1e-6))
        if seconds > 15:
            raise ValueError(f"{ep} {clip['id']}: the acted lines need {t:.1f}s; H3 clips are at most 15 s. Split the clip.")
        cuts[-1]["dur"] = round(cuts[-1]["dur"] + seconds - t, 3)   # the last cut holds the remainder
        track = p.root / "perform" / ep / f"{clip['id']}.wav"
        if placed:
            mix(placed, seconds, track)
        timing[clip["id"]] = {"seconds": seconds, "cuts": cuts, "lines": placed, "track": str(track) if placed else None}
        log(f"{clip['id']}: {seconds}s, cuts " + " / ".join(f"{c['dur']:.1f}" for c in cuts) + f", {len(placed)} line(s)")
    save_json(p.root / "perform" / ep / "timing.json", timing)
    return timing


def mix(placed: list[dict], seconds: int, dest: Path) -> None:
    ins, filt = [], []
    for i, pl in enumerate(placed):
        ins += ["-i", pl["wav"]]
        filt.append(f"[{i}:a]aresample={TAKE_RATE},aformat=channel_layouts=mono,adelay={int(pl['start'] * 1000)}[a{i}]")
    filt.append("".join(f"[a{i}]" for i in range(len(placed))) +
                f"amix=inputs={len(placed)}:normalize=0,apad=whole_dur={seconds},atrim=0:{seconds}[out]")
    subprocess.run(["ffmpeg", "-y", "-v", "error", *ins, "-filter_complex", ";".join(filt), "-map", "[out]",
                    "-ac", "2", "-ar", str(TAKE_RATE), str(dest)], check=True)


def retimed(clip: dict, timing: dict) -> dict:
    """The clip with each cut's duration replaced by the performed timing (for the prompt's [Shot N] At mm:ss)."""
    tm = timing[clip["id"]]
    return {**clip, "cuts": [{**cut, "dur": c["dur"]} for cut, c in zip(clip["cuts"], tm["cuts"])]}


def run(p: Project, ep: str, only: set[str] | None = None) -> dict:
    takes(p, ep, only)
    choose(p, ep)
    return layout(p, ep)
