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
import re
import unicodedata
from dataclasses import dataclass, field

DEFAULT_CUT_S = 3
CJK_CHARS_PER_S = 4.5          # comfortable dramatic Mandarin delivery
WORDS_PER_S = 2.6
SHOT_PHRASE = {
    "WS": "A wide shot",
    "MS": "A medium shot",
    "MCU": "A medium close-up from mid-chest up",
    "CU": "A close-up",
    "ECU": "An extreme close-up",
    "OTS": "An over-the-shoulder shot",
    "VO": "A close-up",
    "RX": "A close-up",   # reaction: the listener, while the previous cut's line continues off-screen
}
# Lens per shot size, as on a drama set: long lenses and shallow focus for faces (a wide lens on a close-up is the
# phone look the reference series never has)
SHOT_LENS = {
    "WS": "on a 35mm lens",
    "MS": "on a 50mm lens",
    "MCU": "on a 65mm lens with shallow depth of field",
    "CU": "on an 85mm lens with shallow depth of field",
    "RX": "on an 85mm lens with shallow depth of field",
    "VO": "on an 85mm lens with shallow depth of field",
    "ECU": "on a 100mm macro lens with very shallow depth of field",
    "OTS": "on a 75mm lens",
}
# Camera angle (`angle` on a cut, default "front"). A cut to the same person at the same size reads as a jump unless
# the camera moves at least ~30 degrees around them, so a different angle is a real cut (the editors' 30-degree rule)
ANGLE_PHRASE = {
    "front": "",
    "three-quarter": "from a three-quarter angle",
    "profile": "in profile from the side",
    "high": "from a high angle looking down",
    "low": "from a low angle looking up",
}
# Longest stretch of screen time at one location made only of close-ups and over-the-shoulder shots before a medium or
# wide shot shows the room again (E01 2026-10-31: 50 s of faces in a row lost the crowd and the table)
CLOSE_RUN_S = 25.0
# The size a cut reads as on screen: two adjacent cuts of the same subjects at the same size are a jump cut
SIZE_CLASS = {"WS": "wide", "MS": "medium", "MCU": "mcu", "CU": "close", "RX": "close", "VO": "close", "ECU": "insert", "OTS": "ots"}
NO_TEXT = "No on-screen text, subtitles, captions, logos or watermarks appear at any point."
# What the reference series' look has and ours lacked (REFERENCE_STUDY §3, owner review 2026-10-31: "lacks realism")
CINEMA = ("Filmed like a high-end streaming drama on a cinema camera with prime lenses: faces are shot on long lenses with "
          "shallow depth of field, the background falling into soft bokeh, and never with wide-angle distortion. The light "
          "has a clear direction: a key light from one side models the faces, a gentle rim of backlight separates hair and "
          "shoulders from the background, and shadows have real depth; never flat, even front light. Natural film colour "
          "with gentle contrast and soft highlight roll-off. Skin is real skin with pores and fine imperfections, never "
          "airbrushed, waxy or over-sharpened. The actors play naturally and with restraint: emotion shows in the eyes, the "
          "breath and small movements, never in exaggerated grimaces. Characters look at each other, never into the lens.")
# Look presets (`look_preset` in the bible), from the five reference hits studied 2026-10-31: a rural or period drama
# is shot moody and directional (cinematic), a comedy or bright urban story high-key and clean (bright: 王妈在顾家当保姆),
# a romance glossy, glowing and warm (glossy: 备用联系人, 反派拿捏). Each preset has its own finishing grade (assemble.GRADES).
LOOKS = {
    "cinematic": CINEMA,
    "bright": ("Filmed like a bright, polished streaming comedy on a cinema camera: clean high-key light, faces lit softly and "
               "flatteringly by bright daylight or bright interior light with gentle shadows, fresh natural colour with clean "
               "whites. Faces are shot on long lenses with the background softly out of focus, never with wide-angle "
               "distortion. Skin is real skin with natural texture, never waxy or airbrushed. The actors play with energy and "
               "clear, readable expressions and timed reactions. Characters look at each other, never into the lens."),
    "glossy": ("Filmed like a glossy romance drama: luminous skin with a soft natural sheen, warm practical lights and creamy "
               "bokeh behind the actors, a gentle glow around highlights, rich but soft colour, flattering light with a soft "
               "key and a warm rim. Faces are shot on long lenses with very shallow depth of field, never wide-angle. Eyes are "
               "bright and expressive; the actors play intimate and restrained, with lingering looks and small touches. "
               "Characters look at each other, never into the lens."),
}
# What the reference hits measure (100 episodes of 5 series, 2026-10-31): 4 of 5 run a median 64-88 s per episode
EPISODE_S = (45, 100)
FLAT_LIGHT = re.compile(r"\b(overcast|midday|noon|clear sky|even(ly)? lit|flat|soft shadows|shadowless|bright natural)\b", re.I)
STATIC = re.compile(r"^\s*static", re.I)
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
    if scene.get("look_preset", "cinematic") not in LOOKS:
        errs.append(f"look_preset {scene['look_preset']!r} not one of {sorted(LOOKS)}")
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
                if t not in ("CU", "MCU", "OTS", "VO"):
                    errs.append(f"{where}: dialogue must be a close-up, medium close-up, over-the-shoulder or voice-over "
                                f"shot, not {t}")
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
            if t in ("MS", "WS") and cut.get("line"):
                errs.append(f"{where}: medium and wide shots are for positioning/movement; give dialogue its own close-up")
            if cut.get("via"):
                if cut["via"] != "phone":
                    errs.append(f"{where}: via {cut['via']!r}; the only value is \"phone\"")
                elif not cut.get("line"):
                    errs.append(f"{where}: via phone without a line")
                elif cut.get("speaker") in cut.get("subjects", []):
                    errs.append(f"{where}: a phone line is heard from the other end; the shot shows the listener, not "
                                "the speaker (cut to the speaker without `via` to show them talking)")
            if cut.get("extras") and t in ("CU", "ECU", "VO"):
                errs.append(f"{where}: extras need room in the frame; give them to a medium, wide or over-the-shoulder shot")
            if cut.get("angle", "front") not in ANGLE_PHRASE:
                errs.append(f"{where}: angle {cut['angle']!r} not one of {sorted(ANGLE_PHRASE)}")
            if cut.get("to") and cut["to"] not in chars:
                errs.append(f"{where}: unknown character {cut['to']!r} in 'to'")
    errs += jump_cuts(scene)
    return errs


def _framing(cut: dict) -> tuple:
    return SIZE_CLASS.get(cut["type"]), frozenset(cut.get("subjects", [])), cut.get("over"), cut.get("angle", "front")


def jump_cuts(scene: dict) -> list[str]:
    """Two adjacent cuts (also across a clip boundary in the same location) showing the same people at the same size:
    on screen the picture jumps instead of cutting. The usual cause is "A speaks / RX on B / B answers": let B's
    close-up hold the reaction and then the answer, show another listener, or answer in an over-the-shoulder shot."""
    errs, prev = [], None
    for clip in scene["clips"]:
        for i, cut in enumerate(clip["cuts"], 1):
            here = (clip["location"], _framing(cut))
            if prev and prev[0] == here and here[1][1]:
                errs.append(f"{clip['id']} cut {i}: jump cut, the same framing of "
                            f"{', '.join(sorted(here[1][1]))} as {prev[1]}; change the size or angle, show someone else, "
                            "or merge the two cuts")
            prev = (here, f"{clip['id']} cut {i}")
    return errs


def fix_jump_cuts(scene: dict) -> list[str]:
    """Repair jump cuts in place. A close-up line right after a close-up of the same person becomes an
    over-the-shoulder shot past the person addressed (the reverse angle). Any other jump, such as one person alone on
    the phone, moves the camera around the subject instead: the second shot is taken in profile (or from a
    three-quarter angle when the first already was), which is a real cut by the 30-degree rule."""
    changes, prev = [], None
    for clip in scene["clips"]:
        for i, cut in enumerate(clip["cuts"]):
            here = (clip["location"], _framing(cut))
            if prev == here and here[1][1] and cut["type"] in ("CU", "MCU") and cut.get("line"):
                who, other = cut["speaker"], partner(clip, i)
                if other and other != who and (other in clip_characters(clip) or len(clip_characters(clip)) < 3):
                    cut.update(type="OTS", over=other, subjects=[who],
                               composition=f"Over-the-shoulder shot past {{{other}}}'s blurred shoulder onto {{{who}}}.")
                    here = (clip["location"], _framing(cut))
                    changes.append(f"{clip['id']} cut {i + 1}: close-up -> over-the-shoulder past {other}")
            if prev == here and here[1][1] and cut["type"] != "OTS":
                cut["angle"] = "three-quarter" if prev[1][3] == "profile" else "profile"
                here = (clip["location"], _framing(cut))
                changes.append(f"{clip['id']} cut {i + 1}: same framing again -> {cut['angle']} angle")
            prev = here
    return changes


def close_runs(scene: dict) -> list[tuple[str, int, int, float]]:
    """Stretches at one location (across clips) made only of close-ups and over-the-shoulder shots, longer than
    CLOSE_RUN_S: (location, first, last, seconds) with first/last as indexes into the episode's flat cut list."""
    flat = [(cl["location"], c) for cl in scene["clips"] for c in cl["cuts"]]
    tight = [SIZE_CLASS.get(c["type"]) in ("close", "mcu", "ots") for _, c in flat]
    runs, k = [], 0
    while k < len(flat):
        if not tight[k]:
            k += 1
            continue
        j = k
        while j + 1 < len(flat) and tight[j + 1] and flat[j + 1][0] == flat[k][0]:
            j += 1
        secs = sum(cut_len(c) for _, c in flat[k:j + 1])
        if secs > CLOSE_RUN_S:
            runs.append((flat[k][0], k, j, secs))
        k = j + 1
    return runs


def fix_close_runs(scene: dict) -> list[str]:
    """Break each too-long run of faces with a medium shot: the silent reaction cut nearest the middle of the run
    becomes a medium shot of that listener among the others, so the room is seen again. A cut is only changed when
    the clip still passes the rules afterwards (a line that needs its reaction cut keeps it)."""
    changes = []
    for _ in range(20):   # each fix splits a run; look again until none is left that can be fixed
        where = [(cl, i) for cl in scene["clips"] for i in range(len(cl["cuts"]))]
        fixed = False
        for loc, a, b, secs in close_runs(scene):
            mid = (a + b) / 2
            for k in sorted(range(a, b + 1), key=lambda k: abs(k - mid)):
                clip, i = where[k]
                cut = clip["cuts"][i]
                nxt = clip["cuts"][i + 1] if i + 1 < len(clip["cuts"]) else None
                if cut["type"] != "RX" or (nxt and nxt["type"] == "RX"):
                    continue
                before = len(check_rules({**scene, "clips": [clip]}))
                old = dict(cut)
                who = cut["subjects"][0]
                cut.update(type="MS", composition=f"Medium shot of {{{who}}} among the others around them, listening and "
                                                  "reacting; whoever is speaking stays off camera.")
                cut.pop("angle", None)
                if len(check_rules({**scene, "clips": [clip]})) > before:
                    cut.clear()
                    cut.update(old)
                    continue
                changes.append(f"{clip['id']} cut {i + 1}: reaction close-up -> medium shot ({secs:.0f} s of faces at {loc})")
                fixed = True
                break
            if fixed:
                break
        if not fixed:
            break
    return changes


def craft_notes(scene: dict) -> list[str]:
    """Advice, not errors: where an episode departs from how the reference series is shot."""
    notes, cuts = [], [(cl, c) for cl in scene["clips"] for c in cl["cuts"]]
    if not cuts:
        return notes
    close = sum(SIZE_CLASS.get(c["type"]) == "close" for _, c in cuts) / len(cuts)
    if close > 0.7:
        notes.append(f"{close:.0%} of cuts are single close-ups; mix in over-the-shoulder shots for exchanges and "
                     "medium or wide shots that show where everyone stands (aim for at most 65%)")
    static = sum(bool(STATIC.match(c.get("camera", ""))) for _, c in cuts if c["type"] != "VO") / len(cuts)
    if static > 0.6:
        notes.append(f"{static:.0%} of cuts have a static camera; give the beats a motivated move (a slow push-in on a "
                     "realisation, handheld drift in a quarrel, a pull-back to isolate someone, a rack focus between two "
                     "faces)")
    for loc, a, b, secs in close_runs(scene):
        notes.append(f"{secs:.0f} s of close-ups and over-the-shoulders in a row at {loc} (cut {a + 1} to {b + 1} of the "
                     f"episode); show the room again with a medium or wide shot at least every {CLOSE_RUN_S:.0f} s "
                     "(`check --fit` turns a reaction close-up into one where it can)")
    for loc in dict.fromkeys(cl["location"] for cl, _ in cuts):
        if not any(c["type"] in ("WS", "MS", "OTS") for cl, c in cuts if cl["location"] == loc):
            notes.append(f"{loc}: no wide, medium or over-the-shoulder shot; the viewer never sees where everyone "
                         "stands. Open the scene's geography after the hook")
    dur = sum(cut_len(c) for _, c in cuts)
    if dur > EPISODE_S[1]:
        notes.append(f"the episode runs {dur:.0f} s; the reference hits run a median 64-88 s an episode. Split it at the "
                     "turn into two episodes, each with its own hook and cliffhanger")
    elif dur < EPISODE_S[0]:
        notes.append(f"the episode runs only {dur:.0f} s; the reference hits run a median 64-88 s an episode")
    cold, prev = [], None
    for n, cl in enumerate(scene["clips"]):   # the episode's own opening may be a cold open on a face
        if n and cl["location"] != prev and cl["cuts"] and cl["cuts"][0]["type"] not in ("WS", "MS"):
            cold.append(f"{cl['id']} ({cl['location']})")
        prev = cl["location"]
    if cold:
        notes.append("scenes that open without a wide or medium shot of the place: " + ", ".join(cold) + ". The reference "
                     "hits open most scenes on 1.5-3 s of a wide shot that already has action in it (the location card "
                     "sits on it), then cut in; only the episode's opening may be a cold open")
    last = scene["clips"][-1]["cuts"][-1] if scene["clips"] and scene["clips"][-1]["cuts"] else None
    if last and (SIZE_CLASS.get(last["type"]) not in ("close", "mcu", "ots") or cut_len(last) < 2.0):
        notes.append(f"the episode's last shot is {last['type']}, {cut_len(last):g} s; every reference hit ends on a face at the "
                     "peak (a close-up or reaction held 2-3 s), cut hard")
    preset = scene.get("look_preset", "cinematic")
    for loc, v in scene["locations"].items():
        if preset != "cinematic":
            break
        if FLAT_LIGHT.search(v.get("light", "")) and loc in {cl["location"] for cl, _ in cuts}:
            notes.append(f"location {loc}: flat light ({FLAT_LIGHT.search(v['light']).group(0)!r}); describe a light with a "
                         "direction (low sun behind or beside the actors, a practical lamp as key, window light)")
    if re.search(r"crisp|ultra.?sharp|8k|deep focus|wide.?angle|\b(14|18|21|24|28|35)\s?mm", scene.get("style", ""), re.I) \
            and preset != "bright":
        notes.append("style asks for sharpness or a wide lens (crisp, 35mm, deep focus...); the reference look is long "
                     "lenses, shallow focus and soft natural detail")
    return notes


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


# In a close-up, "looks up at" makes H3 move the eyes alone with the head level: on screen it reads as an eye-roll,
# contempt the story never asked for (林晚 in the 蓝城雲境 showcase). The head has to go back with the eyes.
UPWARD = re.compile(r"\b(looks?|glances?|gazes?|peers?|stares?) up\b|\b(looks?|glances?|gazes?) upward|\b(raises?|lifts?) "
                    r"(her|his|their) (eyes|gaze)\b", re.I)
EYE_ROLL = re.compile(r"roll|白眼|contempt|scorn|disdain|sneer|不屑|鄙夷", re.I)
FACE_SHOTS = ("CU", "MCU", "ECU", "VO", "RX", "OTS")


def look_up_note(cut: dict) -> str:
    """The sentence that keeps a written upward look from becoming an eye-roll, or "" (none needed, or one written)."""
    if cut["type"] not in FACE_SHOTS or not UPWARD.search(cut.get("action", "")):
        return ""
    if EYE_ROLL.search(" ".join(str(cut.get(k, "")) for k in ("action", "act", "delivery", "emotion"))):
        return ""
    return ("To look up, the whole head tilts back with the eyes, chin rising, the gaze open and steady; the eyes never "
            "roll upward on their own (that would read as an eye-roll).")


SMALL_MARK = re.compile(r"\b(mole|beauty mark|freckles?|birthmark|small scar|thin scar|tiny scar|dimple)\b", re.I)
# Actions H3 misreads (it takes words literally and fills gaps with its own acting): each with how to write it instead
VAGUE = [
    (re.compile(r"\b(glances?|peeks?)\b", re.I), "a glance: say at what, with the eyes only or the head too, and for how long"),
    (re.compile(r"\breacts?\b", re.I), "\"reacts\": write the physical reaction (eyes widen, a breath held, the hand stops)"),
    (re.compile(r"\b(smirks?|sneers?)\b", re.I), "a smirk: write it (one corner of the mouth lifts slightly, the eyes stay cold)"),
    (re.compile(r"\bturns?( around| away)?\b(?! (to|toward|towards|her|his|their|back|left|right))", re.I),
     "a turn: say which way and to face what (turns her head left toward the door)"),
    (re.compile(r"\b(unreadable|mysterious|complicated|meaningful|subtle)\b", re.I),
     "a feeling H3 can't show: describe the face (eyes level and still, lips closed, no smile)"),
]
MIN_ACTION_WORDS = 10   # a face shot's action shorter than this leaves H3 to invent the acting


def cast_notes(bible: dict) -> list[str]:
    """Advice on the cast: identity anchors H3 can hold from shot to shot."""
    out = []
    for c, ch in bible.get("characters", {}).items():
        if ch.get("card") and not ch.get("demeanor"):
            out.append(f"character {c}: no `demeanor`; give main characters their standing manner (\"calm and composed, "
                       "warm attentive eyes; never sarcastic or contemptuous\") so the video model fills unwritten "
                       "moments in character")
        m = SMALL_MARK.search(ch.get("keep", ""))
        if m:
            out.append(f"character {c}: '{m.group(0)}' as an identity anchor: small facial marks drift and flicker from "
                       "shot to shot in the video model (林晚's beauty mark moved in the showcase). Unless the story needs "
                       "it, anchor the character on something large and stable: hairstyle, glasses, earrings, a hair clip, "
                       "a coat's colour")
    return out


def action_notes(scene: dict) -> list[str]:
    """Face-shot actions too short or too vague for H3, which acts out words literally (an unwritten eye-roll)."""
    out = []
    for cl in scene["clips"]:
        for i, cut in enumerate(cl["cuts"], 1):
            if cut["type"] not in FACE_SHOTS:
                continue
            act = cut.get("action", "")
            if len(re.findall(r"[A-Za-z']+", act)) < MIN_ACTION_WORDS:
                out.append(f"{cl['id']} cut {i}: action too short for H3 ({act!r}); spell out the eyes, head, mouth and "
                           "hands from the first frame to the last")
            for rx, how in VAGUE:
                if rx.search(act):
                    out.append(f"{cl['id']} cut {i}: {how}")
            if UPWARD.search(act) and "head" not in act.lower():
                out.append(f"{cl['id']} cut {i}: 'looks up' in a face shot reads as an eye-roll; write 'tilts her head "
                           "back to look up at...'")
    return out


def phone_only(clip: dict) -> set[str]:
    """Characters heard only through a phone in this clip (`via: "phone"`), never seen in it."""
    seen = {c for cut in clip["cuts"] for c in cut.get("subjects", []) + [cut.get("over")] if c}
    return {cut["speaker"] for cut in clip["cuts"] if cut.get("via") == "phone" and cut.get("speaker")} - seen


def speaker_order(clip: dict) -> list[str]:
    return list(dict.fromkeys(c["speaker"] for c in clip["cuts"] if c.get("line")))


def partner(clip: dict, i: int) -> str | None:
    """Whom the subject of cut `i` (0-based) is looking at: the cut's `to`; for a reaction, the speaker whose line runs
    over it; for a speaker, the nearest other speaker in the clip (the one answering or being answered); otherwise the
    nearest speaker, then anyone else in the clip."""
    cuts = clip["cuts"]
    cut = cuts[i]
    if cut.get("to"):
        return cut["to"]
    me = cut.get("speaker") if cut.get("line") else (cut.get("subjects") or [None])[0]
    if cut["type"] == "RX":
        prev = next((c for c in reversed(cuts[:i]) if c.get("line")), None)
        if prev and prev["speaker"] != me:
            return prev["speaker"]
    order = sorted(range(len(cuts)), key=lambda j: (abs(j - i), j < i))   # nearest first, later before earlier
    for j in order:
        c = cuts[j]
        if j != i and c.get("line") and c["speaker"] != me:
            return c["speaker"]
    return next((c for c in clip_characters(clip) if c != me), None)


def blocking(scene: dict, clip: dict) -> dict[str, str]:
    """Which side of the line of action each character stands on in this clip's location ("left"/"right"), the same in
    every clip there, so the cuts keep screen direction: someone on the left looks toward the right of frame. The
    episode may fix it (`blocking: {loc: {char: side}}`, or on the clip); otherwise the people who talk to each
    other most are put on opposite sides (allies who mostly address the same person end up together)."""
    fixed = clip.get("blocking") or scene.get("blocking", {}).get(clip["location"])
    if fixed:
        return dict(fixed)
    talk: dict[str, dict[str, int]] = {}
    for cl in scene.get("clips", [clip]):
        if cl["location"] != clip["location"]:
            continue
        for i, cut in enumerate(cl["cuts"]):
            me = cut.get("speaker") if cut.get("line") else (cut.get("subjects") or [None])[0]
            other = partner(cl, i)
            if me and other and me != other:
                talk.setdefault(me, {}).setdefault(other, 0)
                talk.setdefault(other, {}).setdefault(me, 0)
                talk[me][other] += 1
                talk[other][me] += 1
    side: dict[str, str] = {}
    for c in sorted(talk, key=lambda c: (-sum(talk[c].values()), c)):   # greedy max-cut: most talk across the line
        pull = {s: sum(n for o, n in talk[c].items() if side.get(o) == s) for s in ("left", "right")}
        side[c] = "left" if not side else ("right" if pull["left"] >= pull["right"] else "left")
    return side


def gaze(who: str, other: str | None, sides: dict[str, str], label) -> str:
    """The eyeline of a single: off-axis toward the partner, on the side screen direction says; never into the lens."""
    if not other:
        return f"{label(who)} looks just off camera, never into the lens."
    s = sides.get(who)
    if s and sides.get(other) and sides[other] != s:
        look = "right" if s == "left" else "left"
        return (f"{label(who)} is framed slightly {s} of centre and looks toward the {look} of frame at {label(other)}, "
                f"who is just off camera; {label(who)} never looks into the lens.")
    return f"{label(who)} looks at {label(other)}, who is just off camera beside the lens; {label(who)} never looks into the lens."


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
    sides = blocking(scene, clip)

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
        defs.append(f"{S(c)} is {chars[c]['name']}, the {chars[c]['role']} {src}: {chars[c]['look']}"
                    + (f" {chars[c]['name']}'s manner: {chars[c]['demeanor'].rstrip('.')}." if chars[c].get("demeanor") else ""))
        if c in phone_only(clip):
            defs.append(f"{S(c)} never appears in the picture of this clip; only {S(c)}'s voice is heard, through a phone.")
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
                    if c in cut.get("subjects", []) or c == cut.get("over")
                    or (c == cut.get("speaker") and cut["type"] != "VO" and cut.get("via") != "phone")]
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
    body = [f"{scene['style']} {LOOKS.get(scene.get('look_preset', 'cinematic'), CINEMA)}{light} {NO_TEXT}"]
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
            edge = f"at the {sides[cut['over']]} edge of frame" if cut["over"] in sides else "in the foreground"
            frame = (f"{phrase} {SHOT_LENS[typ]} from behind {S(cut['over'])}'s shoulder, which is soft and out of focus "
                     f"{edge}, onto {S(cut['subjects'][0])}")
        else:
            frame = f"{phrase} of {' and '.join(S(c) for c in cut['subjects'])}" if cut.get("subjects") else phrase
            frame += f" {SHOT_LENS[typ]}"
        if ANGLE_PHRASE.get(cut.get("angle", "front")):
            frame += f", {ANGLE_PHRASE[cut['angle']]}"
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
        if typ in ("CU", "MCU", "ECU", "VO", "OTS", "RX"):
            faces = [cut["speaker"]] if typ == "OTS" and cut.get("speaker") else cut.get("subjects", [])
            for c in faces:
                parts.append(f"Fine facial detail on {S(c)}: {chars[c]['face_detail']}")
                if chars[c].get("demeanor"):   # who they are, so H3 fills unwritten moments in character (an eye-roll once)
                    parts.append(f"{S(c)}'s manner, unless this shot's action says otherwise: {chars[c]['demeanor'].rstrip('.')}.")
        who = cut.get("subjects", [])
        if cut.get("via") == "phone" and who:
            parts.append(f"{S(who[0])} holds a phone to the ear, eyes unfocused, listening; no one else is on the call in the room.")
        elif typ in ("CU", "MCU", "RX") and len(who) == 1:
            parts.append(gaze(who[0], partner(clip, i - 1), sides, lambda c: labels[c]))
        elif typ == "OTS" and who:
            parts.append(gaze(who[0], cut["over"], sides, lambda c: labels[c]))
        elif typ in ("MS", "WS") and len(who) >= 2 and len({sides.get(c) for c in who[:2]} - {None}) == 2:
            a, b = sorted(who[:2], key=lambda c: sides[c] != "left")
            parts.append(f"{S(a)} is on the left of frame and {S(b)} on the right, facing each other.")
        parts.append(f(cut["action"]))
        parts.append(look_up_note(cut))
        # who else is in frame: written extras, the passers-by of a public place, or no one (an invitation to
        # "background people" in every prompt put strangers into 21% of private-scene panels)
        if cut.get("extras"):
            parts.append(f"In the background: {f(cut['extras']).rstrip('.')}. They are extras who never speak, never "
                         "look at the camera and stay softer than the main characters; no one else is in the shot.")
        elif loc.get("crowd"):
            parts.append("Background people are soft and out of focus, busy with their own business, and never look into the lens.")
        elif typ != "ECU":
            seen = [S(c) for c in cut.get("subjects", [])] + ([S(cut["over"])] if cut.get("over") else [])
            parts.append(f"No one else is in the shot: only {' and '.join(dict.fromkeys(seen))}." if seen
                         else "The shot is empty of people.")
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
            if cut.get("via") == "phone":
                held = " and ".join(S(x) for x in cut.get("subjects", [])) or "someone"
                parts.append(f"{S(c)} (S{sid[c]}) is on the other end of a phone call and is not in the picture; {S(c)}'s "
                             f"voice comes through the phone held by {held}, thin and slightly compressed like a phone "
                             f"speaker{voice}, {cut['delivery']}, <d>[{lang}] {said}</d> {held} listens and does not speak, "
                             "lips closed.")
            elif typ == "VO" and c not in cut.get("subjects", []):   # the speaker's thoughts over someone else's shot
                others = " and ".join(S(x) for x in cut.get("subjects", []))
                parts.append(f"Over this shot {S(c)}'s (S{sid[c]}) inner voice-over is heard{voice}, {cut['delivery']}, "
                             f"<d>[{lang}] {said}</d> {others} does not speak.")
            elif typ == "VO":
                # never "off-screen" here: H3 then leaves the speaker out of the picture (E02 C3 rendered an empty set)
                parts.append(f"{S(c)} stays in frame, silent, lips closed, while {S(c)}'s (S{sid[c]}) inner voice-over is heard"
                             f"{voice}, {cut['delivery']}, <d>[{lang}] {said}</d> {S(c)}'s lips stay closed throughout the line.")
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
