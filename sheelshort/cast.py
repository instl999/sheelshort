"""Casting: the locked reference pack (close-up, three-view sheet, props, location plates) and the cast voices.

Everything here is made once per series and then reused by every episode: identity comes from these files, never
from re-describing a character. A file the user puts in inputs/ (char_<id>.png, sheet_<id>.png, loc_<id>.png,
prop_<id>.png, voice_<id>.wav + voice_<id>.txt transcript) replaces the generated one.
"""
from __future__ import annotations

import re
import shutil
from concurrent.futures import ThreadPoolExecutor

from . import flows, media, qc, speech
from .project import AUDIO_EXTS, IMAGE_EXTS, Project, load_json, log, save_json

VOICE_MAX_S = 12.0      # Drama 3 clones from the whole clip
H3_VOICE_MAX_S = 7.0    # H3 keeps at most ~7 s of a reference clip


def portrait_prompt(ch: dict) -> str:
    return ("Photorealistic character reference portrait of an original character who does not resemble any real person or "
            f"celebrity. {ch['look']} Front view, head and shoulders, looking straight at the camera with a neutral expression, "
            "plain light-grey seamless studio backdrop, soft even key light, sharp focus on the eyes, long eyelashes and fine skin "
            "texture visible, 85mm lens, high detail, no text, no watermark.")


def outfit_prompt(ch: dict) -> str:
    """Re-dress a character's close-up for another outfit: the image editor keeps the face, so it stays the same person."""
    # the editor smooths small marks away unless they are named (林晚's beauty mark vanished from her evening look)
    marks = "; ".join(a for a in qc.anchors(ch) if re.search(r"scar|mole|beauty mark|freckle|birthmark|tattoo|dimple", a, re.I))
    keep = f" Keep every identifying mark exactly where it is, unchanged in size: {marks}." if marks else ""
    return ("Keep exactly the same person as image 1: the identical face, features, skin, marks, age and expression, "
            f"the same head-and-shoulders framing, front view, plain light-grey studio backdrop and soft even light.{keep} "
            f"Change only the clothing{' and hair' if ch.get('hair') else ''} to: {ch['costume']}"
            f"{'; hair: ' + ch['hair'] if ch.get('hair') else ''}. Photorealistic, no text, no watermark.")


def relight_prompt(loc: dict) -> str:
    """The same place under another light (a location's `lights`): the editor keeps the room, the camera and every
    object, so a night scene is the same room as the day scene."""
    return ("Keep exactly the same place as image 1: the same camera position and framing, the same architecture, "
            "furniture, objects, materials and layout, nothing added or removed, still empty of people. Change only the "
            f"lighting and time of day to: {loc['light'].rstrip('.')}. Photorealistic cinematic film still, no text, no watermark.")


def age_hint(problems: list[str], ch: dict) -> str:
    """A sentence that pushes the next attempt toward the written age when the last one read older or younger."""
    for pr in problems:
        m = re.search(r"looks about (\d+)", pr)
        want = re.search(r"(\d{2})-year-old", ch["look"])
        if m and want:
            seen, age = int(m.group(1)), int(want.group(1))
            if seen > age:
                return (f" The person is exactly {age} years old and looks it: firm skin with only the fine lines of that "
                        "age, no deep wrinkles, no sagging, no grey hair unless written above.")
            if seen < age:
                return (f" The person is exactly {age} years old and looks it: visible lines at the eyes and mouth, mature "
                        "features, the skin texture of that age.")
    return ""


def sheet_prompt(ch: dict) -> str:
    return ("Turn image 1 into a character reference sheet of exactly the same person on a plain light-grey studio background: "
            "three full-body views side by side at the same scale - front view, side view facing left, and back view - standing in a "
            f"relaxed neutral pose. Hair: {ch['hair']}. Costume: {ch['costume']}. Keep the face identical to image 1. "
            "Even soft studio lighting, photorealistic, no text, no labels, no watermark.")


def prop_prompt(pr: dict) -> str:
    return (f"Photorealistic product reference photo of {pr['look']} Centered on a plain light-grey background, soft studio "
            "lighting, sharp focus, no hands, no text, no watermark.")


PLATE_LOOK = {   # the look preset's light and colour (h3prompt.LOOKS), so the plate does not pull the video elsewhere
    "cinematic": "natural film colour, gentle contrast, light with a clear direction and real shadows",
    "bright": "fresh natural colour, clean high-key light with soft gentle shadows",
    "glossy": "rich soft colour, warm practical lights with a gentle glow and creamy bokeh",
}


def plate_prompt(loc: dict, preset: str = "cinematic") -> str:
    """The location plate is a reference picture in every clip there, so its light and look carry into the video: it is
    lit as the location's `light` says and shot like a film still, not a phone snapshot (owner review 2026-10-31)."""
    light = loc.get("light") or f"realistic {loc.get('time', '')} lighting"
    return (f"Cinematic film still of {loc['look'].rstrip('.')}. The place is empty with no people. Light: {light.rstrip('.')}. "
            f"Shot on a cinema camera with a 35mm prime lens at eye level, {PLATE_LOOK.get(preset, PLATE_LOOK['cinematic'])}, "
            "real textures and wear, vertical 9:16 composition, no text, no watermark.")


def images(p: Project) -> None:
    ic, b = p.cfg["images"], p.bible
    chars, locs, props = p.in_use()
    jobs = []
    for pr, v in b.get("props", {}).items():
        if pr in props and not p.asset(f"prop_{pr}").exists():
            key = f"cast/prop_{pr}"
            jobs.append(lambda pr=pr, v=v, key=key: p.run_job(key, lambda cl: flows.t2i(
                p.wf("t2i"), prop_prompt(v), *ic["prop_size"], p.seed(key), f"sheelshort/prop_{pr}"),
                p.d("assets"), f"prop_{pr}", ("images",), 900))
    lit = {x for x in locs if p.locs.get(x, {}).get("base")}   # a location under another light: relit from its plates
    for loc, v in b["locations"].items():
        if (loc in locs or loc in {p.locs[x]["base"] for x in lit}) and not p.asset(f"loc_{loc}").exists():
            key = f"cast/loc_{loc}"
            jobs.append(lambda loc=loc, v=v, key=key: p.run_job(key, lambda cl: flows.t2i(
                p.wf("t2i"), plate_prompt(v, b.get("look_preset", "cinematic")), *ic["plate_size"], p.seed(key), f"sheelshort/loc_{loc}"),
                p.d("assets"), f"loc_{loc}", ("images",), 900))
    outfits = {c for c in chars if c in p.chars and p.base(c) != c}
    jobs += [lambda c=c: character(p, c) for c in b["characters"] if c in chars | {p.base(o) for o in outfits}]
    _parallel(p, jobs)
    later = [lambda c=c: character(p, c) for c in sorted(outfits)]   # re-dressed from the close-ups made above
    for x in sorted(lit):
        base = p.locs[x]["base"]
        for src in p.plates(base):   # every angle of the room, relit: the same room and furniture, another light
            dest = p.root / "assets" / src.name.replace(f"loc_{base}", f"loc_{x}", 1)
            dest = dest.with_suffix(".png")
            if not dest.exists():
                key = f"cast/{dest.stem}"
                later.append(lambda src=src, dest=dest, key=key, x=x: p.run_job(key, lambda cl: flows.edit(
                    p.wf("edit"), relight_prompt(p.locs[x]), [p.put(cl, src)], ic["sheet_long_side"], p.seed(key),
                    f"sheelshort/{dest.stem}"), p.d("assets"), dest.stem, ("images",), 900))   # plates are 9:16 already: no letterbox
    _parallel(p, later)


def character(p: Project, c: str) -> None:
    """Close-up, then three-view sheet, each generated until the vision check passes (`qc.attempts` tries; the
    candidate with the fewest problems is kept and its problems logged). A close-up the user supplied is checked and
    reported but never replaced. A replaced close-up invalidates the sheet made from it."""
    ic, ch = p.cfg["images"], p.chars[c]
    qcfg = p.cfg.get("qc", {})
    done = load_json(p.root / "assets" / "qc.json") if (p.root / "assets" / "qc.json").exists() else {}
    portrait = p.asset(f"char_{c}")

    base = p.base(c)

    def make_portrait(a: int, last: list[str]):
        key = f"cast/char_{c}" if a == 0 else f"cast/char_{c}_a{a}"
        if base != c:   # an outfit: the character's own close-up, re-dressed
            src = p.asset(f"char_{base}")
            if not src.exists():
                log(f"cast {c}: no close-up of {base} to re-dress")
                return None
            run = lambda cl: flows.edit(p.wf("edit"), outfit_prompt(ch), [p.put(cl, src)], ic["sheet_long_side"],
                                        p.seed(key), f"sheelshort/char_{c}")
        else:
            run = lambda cl: flows.t2i(p.wf("t2i"), portrait_prompt(ch) + age_hint(last, ch), *ic["portrait_size"],
                                       p.seed(key), f"sheelshort/char_{c}")
        out = p.run_job(key, run, p.d("assets", "candidates"), f"char_{c}_a{a}", ("images",), 900)
        return out[0] if out else None

    def make_sheet(a: int, last: list[str]):
        key = f"cast/sheet_{c}" if a == 0 else f"cast/sheet_{c}_a{a}"
        out = p.run_job(key, lambda cl: flows.edit(p.wf("edit"), sheet_prompt(ch), [p.put(cl, portrait)], ic["sheet_long_side"],
                                                   p.seed(key), f"sheelshort/sheet_{c}", aspect="16:9", background="#D9D9D9"),
                        p.d("assets", "candidates"), f"sheet_{c}_a{a}", ("images",), 900)
        return out[0] if out else None

    user_portrait = p.supplied(f"char_{c}", IMAGE_EXTS) is not None
    if not qcfg.get("enabled", True):
        if not portrait.exists():
            f = make_portrait(0, [])
            f and shutil.copy2(f, portrait)
    elif f"char_{c}" not in done:
        canon = user_portrait or (qc.locked(p) and portrait.exists())   # only images already rendered are canon
        changed = _best_of(p, f"char_{c}", portrait, make_portrait, qc.check_portrait, c,
                           attempts=1 if canon else int(qcfg.get("attempts", 3)), keep_existing=True)
        if changed:   # the sheet was made from the old close-up
            p.asset(f"sheet_{c}").unlink(missing_ok=True)
            done.pop(f"sheet_{c}", None)
    qpath = p.root / "assets" / "qc.json"
    rec = load_json(qpath).get(f"char_{c}") if qpath.exists() else None
    if rec and rec["problems"] and rec.get("seen") and base == c:   # what regeneration could not fix, the text is aligned to
        # (an outfit's text is the writer's; its face is its character's, aligned there)
        left = qc.align_text(p, c, rec["problems"], rec["seen"])
        if left != rec["problems"]:
            qc.report(p, {f"char_{c}": {**rec, "problems": left}})
    sheet = p.asset(f"sheet_{c}")
    if not portrait.exists():
        log(f"sheet {c}: no close-up")
        return
    if not qcfg.get("enabled", True):
        if not sheet.exists():
            f = make_sheet(0, [])
            f and shutil.copy2(f, sheet)
    elif f"sheet_{c}" not in done or not sheet.exists():
        _best_of(p, f"sheet_{c}", sheet, make_sheet, qc.check_sheet, c,
                 attempts=1 if qc.locked(p) and sheet.exists() else int(qcfg.get("attempts", 3)), keep_existing=True)


def _best_of(p: Project, name: str, dest, make, check, c: str, attempts: int, keep_existing: bool) -> bool:
    """Check the existing file (attempt 0) or make candidates until one passes; install the best. Returns True when
    `dest` was replaced."""
    tried = []
    for a in range(attempts):
        f = dest if (a == 0 and keep_existing and dest.exists()) else make(a, tried[-1][3] if tried else [])
        if f is None:
            continue
        try:
            problems, raw = check(p, c, f, a)
        except Exception as e:  # noqa: BLE001 - a failed check must not stop casting
            log(f"qc {name}: check failed ({e}); keeping the image unchecked")
            problems, raw = [f"unchecked: {e}"], {}
        tried.append((len(problems), a, f, problems, raw))
        log(f"qc {name} attempt {a}: " + ("pass" if not problems else "; ".join(problems)))
        if not problems:
            break
    if not tried:
        return False
    n, a, f, problems, raw = min(tried, key=lambda t: (t[0], t[1]))
    replaced = f != dest
    if replaced:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(f, dest)
    qc.report(p, {name: {"attempt": a, "attempts": len(tried), "problems": problems, "seen": raw}})
    return replaced


def speakers(p: Project) -> list[str]:
    """Characters with a line in the clips in scope (a test run casts voices only for its own opening)."""
    out = {}
    for ep in (p.only_eps or p.episodes()) if p.limit_s else p.episodes():
        for clip in p.clips(ep):
            for cut in clip["cuts"]:
                if cut.get("line"):
                    out.setdefault(p.base(cut["speaker"]))
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
    for line in voice_report(p):
        log(f"cast: {line}")


VOICE_HZ = {"man": (75, 165), "woman": (160, 320)}   # median speaking pitch of an adult voice of that sex
VOICE_HINT = {"man": "。这是成年男性的声音，音调低沉，绝不是女声或童声。",
              "woman": "。这是成年女性的声音，音色清晰自然，绝不是男声。"}


def voice_mismatch(ch: dict, median_hz: float | None) -> str:
    """Why a designed voice does not fit the role's sex, or "" (waimai2's 29-year-old man came out at 261 Hz)."""
    sex = qc.written_sex(ch)
    if not sex or not median_hz:
        return ""
    lo, hi = VOICE_HZ[sex]
    return "" if lo <= median_hz <= hi else f"median pitch {median_hz:.0f} Hz is outside a {sex}'s {lo}-{hi} Hz"


def _designed(p: Project, c: str, ch: dict, key: str) -> None:
    """A designed voice, checked against the role's sex; a voice that misses is designed again with the sex spelled
    out in its description (the node has no seed: the same request would bring back the same voice)."""
    lang = "Chinese" if p.cfg["project"]["language"].startswith("zh") else "English"
    instruct, files = ch["voice_design"], None
    for a in range(3):
        k = key if a == 0 else f"{key}_a{a}"
        files = p.run_job(k, lambda cl, instruct=instruct: flows.voice_design(p.wf("voice_design"), ch["voice_sample"], instruct,
                                                                              lang, f"sheelshort/voice_{c}"),
                          p.d("voices", "raw"), c if a == 0 else f"{c}_a{a}", ("audio",), 900)
        if not files:
            return
        why = voice_mismatch(ch, speech.profile(files[0]).get("median_hz"))
        if not why:
            break
        log(f"voice {c}: {why}; designing it again")
        instruct = ch["voice_design"].rstrip("。") + VOICE_HINT.get(qc.written_sex(ch) or "", "")
    _finish_voice(p, c, files[0], ch["voice_sample"], "designed")


def voice_report(p: Project) -> list[str]:
    """Cast voices that do not fit their role or sound too alike (same sex, under a semitone apart): for the owner."""
    import math
    out, prof = [], {}
    for c in speakers(p):
        meta = p.voice(c).with_suffix(".json")
        if meta.exists():
            prof[c] = load_json(meta).get("median_hz")
            why = voice_mismatch(p.chars[c], prof[c])
            if why:
                out.append(f"voice {c}: {why}")
    names = sorted(c for c in prof if prof[c])
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if qc.written_sex(p.chars[a]) == qc.written_sex(p.chars[b]) and abs(12 * math.log2(prof[a] / prof[b])) < 1.0:
                out.append(f"voices {a} and {b}: {prof[a]:.0f} and {prof[b]:.0f} Hz, under a semitone apart (may sound alike)")
    return out


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
    p.wake()
    with ThreadPoolExecutor(max(2, len(p.cfg["comfy"]["servers"]) * 2)) as ex:
        list(ex.map(lambda f: f(), jobs))
