"""Build MiniMax H3 full-reference (Ref2VA) prompts and enforce the house shot rules.

Prompt format follows the official h3-prompt-writing skill
(github.com/MiniMax-AI/MiniMax-H3/tree/main/skills/h3-prompt-writing, references/ref-en.txt):
six English sections in fixed order, `<Subject N>/<Picture N>/<Audio N>` labels, `[Shot N] At MM:SS.mmm`
cut markers, `(Sx)` speaker IDs and dialogue kept in its original language inside `<d>[Chinese] ...</d>`.

House rules (from the user, apply to every generation):
- No on-screen text, subtitles or background music.
- Default cut length 3 s. Speaking character -> close-up; two-person dialogue -> over-the-shoulder;
  positioning and movement -> medium shot; actions and expressions -> close-up;
  voice-over (OS) -> starts as a close-up and slowly pushes in to an extreme close-up.
- Cinematic composition, fine facial detail (eyelashes, makeup, skin texture).
- A character's appearance never changes between clips; no resemblance to real celebrities.
"""
from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass, field

DEFAULT_CUT_S = 3
CJK_CHARS_PER_S = 4.5          # comfortable dramatic Mandarin delivery
WORDS_PER_S = 2.6
SHOT_PHRASE = {
    "MS": "A medium shot",
    "CU": "A close-up",
    "ECU": "An extreme close-up",
    "OTS": "An over-the-shoulder shot",
    "VO": "A close-up",
    "RX": "A close-up",   # reaction: the listener, while the previous cut's line continues off-screen
}
NO_TEXT = "No on-screen text, subtitles, captions, logos or watermarks appear at any point."
NO_MUSIC = "None. There is no background music or score at any point; only diegetic sound is heard."
LANG_TAG = {"zh": "Chinese", "en": "English", "ja": "Japanese", "ko": "Korean"}
REFERENCE_MEAN_SHOT_S = 2.07   # 全村抢 001 as released: 151 s, 73 shots
LINE_SLACK_S = 0.3             # breathing room a line needs beyond its spoken length


def cut_len(cut: dict) -> float:
    return float(cut.get("dur", DEFAULT_CUT_S))


def clip_len(clip: dict) -> float:
    return sum(cut_len(c) for c in clip["cuts"])


def spoken_len(line: str, lang: str) -> float:
    """Rough seconds needed to deliver a line naturally."""
    if lang.startswith(("zh", "ja")):
        n = sum(1 for ch in line if not unicodedata.category(ch).startswith(("P", "Z", "S")))
        return n / CJK_CHARS_PER_S
    return len(line.split()) / WORDS_PER_S


def check_rules(scene: dict) -> list[str]:
    """Return rule violations; an empty list means the scene may be generated."""
    errs = []
    lang = scene.get("language", "zh-CN")
    chars, locs = scene["characters"], scene["locations"]
    for clip in scene["clips"]:
        cid = clip["id"]
        total = clip_len(clip)
        if not 4 <= total <= 15:
            errs.append(f"{cid}: clip is {total}s; H3 clips must be 4-15 s")
        if clip["location"] not in locs:
            errs.append(f"{cid}: unknown location {clip['location']!r}")
        for i, cut in enumerate(clip["cuts"], 1):
            where = f"{cid} cut {i}"
            for c in cut.get("subjects", []) + [cut.get("speaker"), cut.get("over")]:
                if c and c not in chars:
                    errs.append(f"{where}: unknown character {c!r}")
            for pr in cut.get("props", []):
                if pr not in scene.get("props", {}):
                    errs.append(f"{where}: unknown prop {pr!r}; props must be locked in scene.props first")
            t = cut["type"]
            if t not in SHOT_PHRASE:
                errs.append(f"{where}: shot type {t!r} not one of {sorted(SHOT_PHRASE)}")
            if cut.get("line"):
                if not cut.get("speaker"):
                    errs.append(f"{where}: a line needs a speaker")
                if t not in ("CU", "OTS", "VO"):
                    errs.append(f"{where}: dialogue must be a close-up, over-the-shoulder or voice-over shot, not {t}")
                need = spoken_len(cut["line"], lang)
                room = cut_len(cut) + sum(cut_len(c) for c in _reactions_after(clip["cuts"], i - 1))
                if need > room - LINE_SLACK_S:
                    errs.append(f"{where}: line needs ~{need:.1f}s but the cut (and its reaction shots) last {room:.1f}s; "
                                "split it, add a reaction cut, or lengthen the cut")
            if t == "OTS":
                if not cut.get("over"):
                    errs.append(f"{where}: over-the-shoulder shot needs 'over' (whose shoulder)")
                elif cut.get("speaker") == cut["over"]:
                    errs.append(f"{where}: the speaker should face camera, not be the shoulder in the foreground")
            if t == "RX":
                if cut.get("line"):
                    errs.append(f"{where}: a reaction cut is silent; give the line its own close-up")
                if len(cut.get("subjects", [])) != 1:
                    errs.append(f"{where}: a reaction cut shows exactly one listener")
                if i == 1 or not (clip["cuts"][i - 2].get("line") or clip["cuts"][i - 2]["type"] == "RX"):
                    errs.append(f"{where}: a reaction cut must follow a line (the line continues over it)")
            if cut_len(cut) < 1.2:
                errs.append(f"{where}: cuts shorter than 1.2 s read as a glitch")
            if t == "VO" and not cut.get("line"):
                errs.append(f"{where}: voice-over shot without a line")
            if t == "MS" and cut.get("line"):
                errs.append(f"{where}: medium shots are for positioning/movement; give dialogue its own close-up")
    return errs


@dataclass
class Refs:
    """Reference assets for one clip, in label order."""
    char_pictures: dict[str, list[str]] = field(default_factory=dict)   # char id -> [close-up, three-view sheet]
    prop_pictures: dict[str, list[str]] = field(default_factory=dict)   # prop id -> reference image(s)
    location_pictures: list[str] = field(default_factory=list)
    keyframe: str | None = None
    voices: dict[str, str] = field(default_factory=dict)                # char id -> audio file name
    animatic: str | None = None                                          # blockout animatic of the whole clip
    cut_backgrounds: dict[int, str] = field(default_factory=dict)       # cut (1-based) -> location picture it shows
    dialogue_track: bool = False   # <Audio 1> is the clip's performed dialogue, rendered to (perform.py)

    def images(self) -> list[str]:
        return (([self.keyframe] if self.keyframe else []) + [p for ps in self.char_pictures.values() for p in ps]
                + [p for ps in self.prop_pictures.values() for p in ps] + self.location_pictures)

    def audios(self) -> list[str]:
        return list(self.voices.values())

    def videos(self) -> list[str]:
        return [self.animatic] if self.animatic else []


def _reactions_after(cuts: list[dict], idx: int) -> list[dict]:
    """The reaction cuts that directly follow cut `idx` (0-based)."""
    out = []
    for c in cuts[idx + 1:]:
        if c["type"] != "RX":
            break
        out.append(c)
    return out


def fit_durations(scene: dict) -> list[str]:
    """Lengthen each dialogue cut whose line (with its reaction cuts) has too little room. Edits the clips in place and
    returns what changed; never shortens a cut."""
    lang, changes = scene.get("language", "zh-CN"), []
    for clip in scene["clips"]:
        for i, cut in enumerate(clip["cuts"]):
            if not cut.get("line"):
                continue
            need = spoken_len(cut["line"], lang) + LINE_SLACK_S
            room = cut_len(cut) + sum(cut_len(c) for c in _reactions_after(clip["cuts"], i))
            if need > room:
                old = cut_len(cut)
                cut["dur"] = round(old + math.ceil((need - room) * 10) / 10 + 0.1, 1)
                changes.append(f"{clip['id']} cut {i + 1}: {old:g}s -> {cut['dur']:g}s")
    return changes


def pacing(scene: dict) -> dict:
    """Planned shot statistics of an episode, comparable with media.pacing() on a finished master."""
    lens = [cut_len(c) for clip in scene["clips"] for c in clip["cuts"]]
    total = sum(lens)
    return {"clips": len(scene["clips"]), "shots": len(lens), "duration_s": round(total, 1),
            "mean_shot_s": round(total / max(len(lens), 1), 2), "over_5s": sum(x > 5 for x in lens)}


def clip_characters(clip: dict) -> list[str]:
    seen: dict[str, None] = {}
    for cut in clip["cuts"]:
        for c in cut.get("subjects", []) + [cut.get("over"), cut.get("speaker")]:
            if c:
                seen.setdefault(c)
    return list(seen)


def clip_props(clip: dict) -> list[str]:
    return list(dict.fromkeys(pr for cut in clip["cuts"] for pr in cut.get("props", [])))


def speaker_order(clip: dict) -> list[str]:
    return list(dict.fromkeys(c["speaker"] for c in clip["cuts"] if c.get("line")))


class _Labels(dict):
    """Subject labels for the clip's cast; a character or prop mentioned but not on screen falls back to its name."""

    def __init__(self, labels: dict, names: dict):
        super().__init__(labels)
        self.names = names

    def __missing__(self, key):
        return self.names.get(key, key)


def ts(seconds: float) -> str:
    return f"{int(seconds // 60):02d}:{seconds % 60:06.3f}"


def build(scene: dict, clip: dict, refs: Refs, cameras: dict[int, dict] | None = None) -> str:
    """cameras: per cut (1-based), camera text measured from a blockout (blockout.camera_texts). With "replace",
    it stands in for the cut's written shot size, composition and camera move (a camera placed by hand in the set);
    otherwise it is added after the composition. Voice-over cuts keep the house push-in either way."""
    cameras = cameras or {}
    chars, loc = scene["characters"], scene["locations"][clip["location"]]
    lang = LANG_TAG.get(scene.get("language", "zh-CN").split("-")[0], "Chinese")
    cast = clip_characters(clip)
    props = clip_props(clip)
    prop_defs = scene.get("props", {})
    sid = {c: i for i, c in enumerate(speaker_order(clip), 1)}

    # ---- labels ----
    pic_no: dict[str, list[int]] = {}
    n = 0
    if refs.keyframe:
        n += 1
        pic_no["__keyframe__"] = [n]
    for c, files in refs.char_pictures.items():
        pic_no[c] = list(range(n + 1, n + 1 + len(files)))
        n += len(files)
    for pr, files in refs.prop_pictures.items():
        pic_no[pr] = list(range(n + 1, n + 1 + len(files)))
        n += len(files)
    if refs.location_pictures:
        pic_no["__location__"] = list(range(n + 1, n + 1 + len(refs.location_pictures)))
    subj = {k: i for i, k in enumerate(cast + props, 1)}
    loc_subj = len(subj) + 1
    aud = {c: i for i, c in enumerate(refs.voices, 1)}
    labels = _Labels({c: f"<Subject {i}>" for c, i in subj.items()} | {"loc": f"<Subject {loc_subj}>"},
                     {c: v["name"] for c, v in chars.items()} | {pr: f"the {v['name']}" for pr, v in prop_defs.items()})

    def f(text: str) -> str:
        """Scene text may name characters as {linxia} and the location as {loc}."""
        return text.format_map(labels)

    def pics(key: str) -> str:
        labels = [f"<Picture {i}>" for i in pic_no.get(key, [])]
        return labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + f" and {labels[-1]}"

    def S(c: str) -> str:
        return f"<Subject {subj[c]}>"

    # ---- subject_definitions ----
    defs = []
    if refs.keyframe:
        defs.append(f"<Picture {pic_no['__keyframe__'][0]}> is the first frame of [Shot 1]: {f(clip['cuts'][0]['composition'])}")
    for c in cast:
        own = pic_no.get(c, [])
        if len(own) >= 2:
            src = (f"whose face comes from the close-up reference <Picture {own[0]}> and whose hair, costume and full-body "
                   f"proportions come from the front, side and back reference sheet <Picture {own[1]}>")
        else:
            src = f"in {pics(c) if own else pics('__keyframe__')}"
        defs.append(f"{S(c)} is {chars[c]['name']}, the {chars[c]['role']} {src}: {chars[c]['look']}")
    for pr in props:
        src = pics(pr) if pr in pic_no else pics("__keyframe__")
        defs.append(f"{S(pr)} is the {prop_defs[pr]['name']} in {src}: {prop_defs[pr]['look']}")
    loc_src = pics("__location__") if "__location__" in pic_no else pics("__keyframe__")
    defs.append(f"<Subject {loc_subj}> is the {loc['name']} in {loc_src}: {loc['look']}")
    if refs.animatic:
        defs.append("<Video 1> is a grey clay blockout animatic of this clip, with untextured mannequins standing in for the "
                    "characters; it provides only the camera position, framing, cut timing and camera movement of every shot "
                    "and where each character stands.")
    for c, i in aud.items():
        defs.append(f"<Audio {i}> is the voice-timbre reference for {S(c)} (S{sid[c]}).")
    if refs.dialogue_track and sid:
        who = " and ".join(f"{S(c)} (S{i})" for c, i in sid.items())
        defs.append(f"<Audio 1> is the recorded dialogue of this clip, performed by {who}.")

    # ---- summary ----
    kinds = ((["keyframe completion"] if refs.keyframe else []) + ["reference generation"] + (["audio reference"] if aud else [])
             + (["audio reuse"] if refs.dialogue_track and sid else []))
    summary = f"[{' + '.join(dict.fromkeys(kinds))}] {f(clip['summary'])}"
    if refs.animatic:
        summary += " <Video 1> guides the camera placement, framing, cut timing and camera movement of every shot."
    if aud:
        summary += " " + " ".join(f"{S(c)} speaks with the voice timbre of <Audio {i}>." for c, i in aud.items())

    # ---- retention_analysis ----
    shots_of = {c: [i for i, cut in enumerate(clip["cuts"], 1)
                    if c in cut.get("subjects", []) or c == cut.get("over") or (c == cut.get("speaker") and cut["type"] != "VO")]
                for c in cast}
    ret = []
    if refs.keyframe:
        ret.append(f"<Picture {pic_no['__keyframe__'][0]}> ([Shot 1] first frame): fully_preserved - the shot begins exactly on this frame's composition, lighting and both characters' appearance.")
    for c in cast:
        where = ", ".join(f"[Shot {i}]" for i in shots_of[c]) or "[Shot 1]"
        ret.append(f"{S(c)} (appears in {where}): fully_preserved - {chars[c]['name']}'s face, {chars[c]['keep']} stay identical in every shot.")
    for pr in props:
        where = ", ".join(f"[Shot {i}]" for i, cut in enumerate(clip["cuts"], 1) if pr in cut.get("props", []))
        ret.append(f"{S(pr)} (appears in {where}): fully_preserved - {prop_defs[pr]['keep']} stay identical.")
    ret.append(f"<Subject {loc_subj}> (appears in {', '.join(f'[Shot {i}]' for i in range(1, len(clip['cuts']) + 1))}): "
               f"fully_preserved - {loc['keep']} stay unchanged.")
    if refs.animatic:
        ret.append("<Video 1> (camera, framing, cut timing and blocking structure): weak_reference - each shot follows the "
                   "camera position, framing, cut point and camera movement of <Video 1>; its grey clay surfaces, mannequins and "
                   "missing detail are not reproduced, and the characters, location and lighting come from the other references.")
    if refs.dialogue_track and sid:
        ret.append("<Audio 1>: partially_copy - the dialogue of <Audio 1> is reused 1:1 as the target video's dialogue, every "
                   "line in sync with its speaker's lips, with the speaker's face acting the emotion heard in the voice; the "
                   "room tone and soundscape are added.")
    for c, i in aud.items():
        ret.append(f"<Audio {i}>: reference - {S(c)} speaks with this voice timbre and delivery without copying the original signal or its words.")

    # ---- detailed_description ----
    light = f" Lighting in every shot: {loc['light'].rstrip('.')}." if loc.get("light") else ""
    body = [f"{scene['style']}{light} {NO_TEXT}"]
    introduced: set[str] = set()
    t = 0.0
    for i, cut in enumerate(clip["cuts"], 1):
        head = f"[Shot {i}]" if i == 1 else f"[Shot {i}] At {ts(t)},"
        typ = cut["type"]
        cam = cameras.get(i)
        phrase = SHOT_PHRASE[typ] if i == 1 else SHOT_PHRASE[typ][0].lower() + SHOT_PHRASE[typ][1:]
        if cam and cam.get("replace") and typ != "VO":
            size = cam["size"]
            phrase = size[0].upper() + size[1:] if i == 1 else size
            frame = f"{phrase} of {' and '.join(S(c) for c in cut['subjects'])}" if cut.get("subjects") else phrase
        elif typ == "OTS":
            frame = f"{phrase} from behind {S(cut['over'])}'s shoulder, which is soft and out of focus in the foreground, onto {S(cut['subjects'][0])}"
        else:
            frame = f"{phrase} of {' and '.join(S(c) for c in cut['subjects'])}" if cut.get("subjects") else phrase
        replaced = bool(cam and cam.get("replace") and typ != "VO")
        parts = [f"{head} {frame}. {f(cam['text']) if replaced else f(cut['composition'])}"]
        if cam and not replaced:
            parts.append(f(cam["text"]))
        bg = refs.cut_backgrounds.get(i)
        if bg in refs.location_pictures:
            parts.append(f"The background in this shot is the part of <Subject {loc_subj}> shown in "
                         f"<Picture {pic_no['__location__'][refs.location_pictures.index(bg)]}>.")
        if refs.animatic:
            parts.append(f"Camera placement and framing follow <Video 1> from {ts(t)} to {ts(t + cut_len(cut))}.")
        for c in cut.get("subjects", []):
            if c not in introduced:
                parts.append(f"{S(c)} keeps the exact appearance of {pics(c) if c in pic_no else 'the reference'}, including the {chars[c]['keep']}.")
                introduced.add(c)
        for pr in cut.get("props", []):
            if pr not in introduced:
                parts.append(f"{S(pr)} keeps the exact appearance of {pics(pr) if pr in pic_no else 'the reference'}: {prop_defs[pr]['keep']}.")
                introduced.add(pr)
        if typ in ("CU", "ECU", "VO", "OTS", "RX"):
            faces = [cut["speaker"]] if typ == "OTS" and cut.get("speaker") else cut.get("subjects", [])
            for c in faces:
                parts.append(f"Fine facial detail on {S(c)}: {chars[c]['face_detail']}")
        parts.append(f(cut["action"]))
        if typ == "VO":
            parts.append("The camera starts as a close-up and slowly pushes in to an extreme close-up of the eyes, with small amplitude and at slow speed.")
        else:
            parts.append(cam["move"] if replaced else f(cut["camera"]))
        if cut.get("sound"):
            parts.append(f(cut["sound"]))
        if cut.get("line"):
            c = cut["speaker"]
            if refs.dialogue_track:
                voice = ", speaking the line heard in <Audio 1> at this moment"
            else:
                voice = f" with the voice timbre of <Audio {aud[c]}>" if c in aud else f", {chars[c]['voice_en']}"
            said = cut.get("say") or cut["line"]   # the acted form keeps its punctuation (…… —— ！) verbatim
            if typ == "VO":
                parts.append(f"{S(c)} (S{sid[c]}) off-screen, as inner voice-over{voice}, {cut['delivery']}, <d>[{lang}] {said}</d> {S(c)}'s lips stay closed throughout the line.")
            else:
                parts.append(f"{S(c)} (S{sid[c]}){voice}, {cut['delivery']}, <d>[{lang}] {said}</d>")
        if typ == "RX":
            prev = next((c for c in reversed(clip["cuts"][:i - 1]) if c.get("line")), None)
            if prev:
                parts.append(f"{S(prev['speaker'])}'s line continues off-screen over this shot without a break; "
                             f"{' and '.join(S(c) for c in cut['subjects'])} listens and reacts silently, lips closed.")
        if cut.get("end"):
            parts.append(f(cut["end"]))
        body.append(" ".join(p.strip() for p in parts if p))
        t += cut_len(cut)

    soundscape = f"{f(clip['soundscape'])} There is no background music."
    sections = [
        ("subject_definitions", "\n".join(defs)),
        ("summary", summary),
        ("retention_analysis", "\n".join(ret)),
        ("detailed_description", "\n".join(body)),
        ("overall_soundscape", soundscape),
        ("non_diegetic_music", NO_MUSIC),
    ]
    return "\n\n".join(f"{k}:\n{v}" for k, v in sections)
