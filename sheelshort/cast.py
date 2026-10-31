"""Casting: the locked reference pack (close-up, three-view sheet, props, location plates) and the cast voices.

Everything here is made once per series and then reused by every episode: identity comes from these files, never
from re-describing a character. A file the user puts in inputs/ (char_<id>.png, sheet_<id>.png, loc_<id>.png,
prop_<id>.png, voice_<id>.wav + voice_<id>.txt transcript) replaces the generated one.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from . import flows, media, speech
from .project import AUDIO_EXTS, Project, load_json, log, save_json

VOICE_MAX_S = 12.0      # Drama 3 clones from the whole clip
H3_VOICE_MAX_S = 7.0    # H3 keeps at most ~7 s of a reference clip


def portrait_prompt(ch: dict) -> str:
    return ("Photorealistic character reference portrait of an original character who does not resemble any real person or "
            f"celebrity. {ch['look']} Front view, head and shoulders, looking straight at the camera with a neutral expression, "
            "plain light-grey seamless studio backdrop, soft even key light, sharp focus on the eyes, long eyelashes and fine skin "
            "texture visible, 85mm lens, high detail, no text, no watermark.")


def sheet_prompt(ch: dict) -> str:
    return ("Turn image 1 into a character reference sheet of exactly the same person on a plain light-grey studio background: "
            "three full-body views side by side at the same scale - front view, side view facing left, and back view - standing in a "
            f"relaxed neutral pose. Hair: {ch['hair']}. Costume: {ch['costume']}. Keep the face identical to image 1. "
            "Even soft studio lighting, photorealistic, no text, no labels, no watermark.")


def prop_prompt(pr: dict) -> str:
    return (f"Photorealistic product reference photo of {pr['look']} Centered on a plain light-grey background, soft studio "
            "lighting, sharp focus, no hands, no text, no watermark.")


def plate_prompt(loc: dict) -> str:
    return (f"Photorealistic cinematic establishing still of {loc['look']} The place is empty with no people. Vertical 9:16 "
            f"composition, eye-level camera, realistic {loc.get('time', '')} lighting, high detail, no text, no watermark.")


def images(p: Project) -> None:
    ic, b = p.cfg["images"], p.bible
    chars, locs, props = p.in_use()
    jobs = []
    for c, ch in b["characters"].items():
        if c in chars and not p.asset(f"char_{c}").exists():
            key = f"cast/char_{c}"
            jobs.append(lambda c=c, ch=ch, key=key: p.run_job(key, lambda cl: flows.t2i(
                p.wf("t2i"), portrait_prompt(ch), *ic["portrait_size"], p.seed(key), f"sheelshort/char_{c}"),
                p.d("assets"), f"char_{c}", ("images",), 900))
    for pr, v in b.get("props", {}).items():
        if pr in props and not p.asset(f"prop_{pr}").exists():
            key = f"cast/prop_{pr}"
            jobs.append(lambda pr=pr, v=v, key=key: p.run_job(key, lambda cl: flows.t2i(
                p.wf("t2i"), prop_prompt(v), *ic["prop_size"], p.seed(key), f"sheelshort/prop_{pr}"),
                p.d("assets"), f"prop_{pr}", ("images",), 900))
    for loc, v in b["locations"].items():
        if loc in locs and not p.asset(f"loc_{loc}").exists():
            key = f"cast/loc_{loc}"
            jobs.append(lambda loc=loc, v=v, key=key: p.run_job(key, lambda cl: flows.t2i(
                p.wf("t2i"), plate_prompt(v), *ic["plate_size"], p.seed(key), f"sheelshort/loc_{loc}"),
                p.d("assets"), f"loc_{loc}", ("images",), 900))
    _parallel(p, jobs)
    jobs = []
    for c, ch in b["characters"].items():
        if c not in chars or p.asset(f"sheet_{c}").exists():
            continue
        src = p.asset(f"char_{c}")
        if not src.exists():
            log(f"sheet {c}: no close-up yet")
            continue
        key = f"cast/sheet_{c}"
        jobs.append(lambda c=c, ch=ch, key=key, src=src: p.run_job(key, lambda cl: flows.edit(
            p.wf("edit"), sheet_prompt(ch), [p.put(cl, src)], ic["sheet_long_side"], p.seed(key), f"sheelshort/sheet_{c}",
            aspect="16:9", background="#D9D9D9"), p.d("assets"), f"sheet_{c}", ("images",), 900))
    _parallel(p, jobs)


def speakers(p: Project) -> list[str]:
    out = {}
    for ep in p.episodes():
        for clip in p.clips(ep):
            for cut in clip["cuts"]:
                if cut.get("line"):
                    out.setdefault(cut["speaker"])
    return list(out) or [c for c, ch in p.chars.items() if ch.get("voice_design")]


def voices(p: Project) -> None:
    """One cast voice per speaking character: the user's recording (inputs/voice_<id>.* with voice_<id>.txt), else a
    Qwen3-TTS designed voice from the character's timbre description saying `voice_sample`."""
    jobs = []
    for c in speakers(p):
        dest, meta = p.voice(c), p.voice(c).with_suffix(".json")
        if dest.exists() and meta.exists():
            continue
        ch = p.chars[c]
        rec = p.supplied(f"voice_{c}", AUDIO_EXTS)
        if rec:
            txt = rec.with_suffix(".txt")
            transcript = txt.read_text(encoding="utf-8").strip() if txt.exists() else speech.transcribe(rec)
            _finish_voice(p, c, rec, transcript, "recording")
            continue
        key = f"cast/voice_{c}"
        jobs.append(lambda c=c, ch=ch, key=key: _designed(p, c, ch, key))
    _parallel(p, jobs)


def _designed(p: Project, c: str, ch: dict, key: str) -> None:
    files = p.run_job(key, lambda cl: flows.voice_design(p.wf("voice_design"), ch["voice_sample"], ch["voice_design"],
                                                         "Chinese" if p.cfg["project"]["language"].startswith("zh") else "English",
                                                         f"sheelshort/voice_{c}"),
                      p.d("voices", "raw"), c, ("audio",), 900)
    if files:
        _finish_voice(p, c, files[0], ch["voice_sample"], "designed")


def _finish_voice(p: Project, c: str, src, transcript: str, origin: str) -> None:
    dest = p.voice(c)
    media.voice_reference(src, dest, VOICE_MAX_S)
    media.voice_reference(src, dest.with_name(f"{c}_h3.wav"), H3_VOICE_MAX_S)
    prof = speech.profile(dest)
    heard = speech.transcribe(dest, p.cfg["project"]["language"], p.cfg["perform"]["whisper_model"])
    cer = speech.error_rate(transcript, heard)
    if cer > 0.15:
        log(f"voice {c}: WARNING the clip says {heard!r}, not its transcript (CER {cer:.2f}); Drama 3 clones best from an exact transcript")
    save_json(dest.with_suffix(".json"), {"transcript": transcript, "heard": heard, "cer": round(cer, 3), "origin": origin, **prof})
    log(f"voice {c}: {origin}, median {prof['median_hz']} Hz, {prof['speech_s']}s -> {dest.name}")


def _parallel(p: Project, jobs: list) -> None:
    if not jobs:
        return
    with ThreadPoolExecutor(max(1, len(p.cfg["comfy"]["servers"]) * 2)) as ex:
        list(ex.map(lambda f: f(), jobs))
