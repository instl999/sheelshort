"""Quality check of a rendered clip, so a bad H3 take is re-rendered instead of reaching the episode.

Two checks, both cheap next to a render:
- **Dialogue** (local): every scripted line must be heard. The Whisper pass that times the subtitles also gives the
  words heard in each line's span; a line not found, or heard with a character error rate above `max_line_cer`, fails.
- **Picture** (the servers' vision model, one call per clip): one frame from the middle of every cut, side by side in a
  strip, described blind. Visible on-screen text or subtitles, a day/night look that contradicts the location's
  `light`, a character shot with nobody in it, or a single-character close-up of the wrong sex fail.

The result (problems, per-line CER, per-panel description) is saved to edit/<EP>/clipqc/<take>.json and shown in the
review page; render.py uses it to decide on re-takes and picks the take with the fewest problems.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import assemble, h3prompt, media, qc
from .project import Project, load_json, log, save_json

STRIP_PROMPT = """This image is a strip of {n} video frames side by side, panel 1 on the left to panel {n} on the right.
For each panel, answer in order. Answer with ONE JSON object and nothing else:
{{"panels": [{{"panel": <number>, "people": <number of people clearly visible>, "main_person_sex": "woman" | "man" | "none",
  "text_or_subtitles": <true only if text is OVERLAID on the picture: subtitles, captions, titles, watermarks or logos; text printed on objects inside the scene (documents, contracts, business cards, signs, phone screens) does not count>,
  "light": "daylight" if the scene is lit by the sun or a bright sky (outdoors by day, or daylight coming through windows) | "night" if it is dark outside or moonlit | "lamp" if it is indoors, lit by lamps or bulbs, with no daylight visible,
  "glitch": <true if a face or body is clearly malformed, melted or duplicated>}}]}}"""

RULES = 5   # bump when the rules change: cached results are re-judged (the vision answers stay cached in qc.ask)
NIGHT = re.compile(r"\bnight|moonlight|dark\b|midnight", re.I)
DAY = re.compile(r"\bday(light)?\b|sunlight|morning|afternoon|noon", re.I)


def cut_times(clip: dict) -> list[float]:
    t, out = 0.0, []
    for cut in clip["cuts"]:
        out.append(t + h3prompt.cut_len(cut) / 2)
        t += h3prompt.cut_len(cut)
    return out


def cut_at(clip: dict, t: float) -> int:
    """0-based index of the planned cut playing at `t` seconds (the last one past the clip's planned end)."""
    end = 0.0
    for i, cut in enumerate(clip["cuts"]):
        end += h3prompt.cut_len(cut)
        if t < end:
            return i
    return len(clip["cuts"]) - 1


def sample_times(clip: dict, dur: float, shot_cuts: list[float]) -> list[float]:
    """The middle of every planned cut, plus the middle of every shot H3 actually cut that contains none of them:
    an inserted shot (an empty establishing view during a voice-over, say) is judged against the cut it plays in."""
    planned = [min(t, dur - 0.05) for t in cut_times(clip)]
    bounds = [0.0] + [c for c in shot_cuts if 0 < c < dur] + [dur]
    extra = [(a + b) / 2 for a, b in zip(bounds, bounds[1:])
             if b - a >= 0.8 and not any(a <= t < b for t in planned)]
    return sorted(planned + extra)


def strip(p: Project, ep: str, video: Path, times: list[float]) -> Path:
    tmp = p.d("edit", ep, "clipqc", "frames", video.stem)   # per episode: E03 and E04 both have a C3_h3_t1
    frames = []
    for i, t in enumerate(times):
        f = tmp / f"p{i + 1}.jpg"
        media._run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1",
                    "-q:v", "3", str(f)])
        frames.append(f)
    return media.tile(frames, tmp / "strip.jpg", cols=len(frames), cell=(288, 512))


PRINTED = re.compile(r"\b(print(ed)?|characters|text|writing|screen|paragraphs?)\b", re.I)
# A shot written to show something that carries writing (a phone screen, a contract, a sign): the vision model reads
# that writing as a caption, so such a shot is not judged on text (E03 C10 cut 3, the phone-screen insert: two takes)
SHOWS_WRITING = re.compile(r"\b(screens?|phone|contracts?|documents?|pages?|sign(board)?s?|posters?|labels?|newspapers?|"
                           r"letters?|cards?|menus?|notes?|receipts?|banners?)\b", re.I)


def judge_panel(chars: dict, i: int, cut: dict, pan: dict, want_night: bool, want_day: bool,
                props: dict | None = None) -> list[str]:
    """Problems with cut `i`, from the vision model's blind description `pan` of its middle frame. A cut that holds a
    prop with printed text (a business card, a contract, a phone screen) is not judged on text: the model reads the
    prop's text as a caption however the question is put (E03's business card, three takes)."""
    out = []
    printed = any(PRINTED.search((props or {}).get(pr, {}).get("look", "")) for pr in cut.get("props", [])) \
        or (cut.get("type") == "ECU" and bool(SHOWS_WRITING.search(cut.get("composition", ""))))
    if pan.get("text_or_subtitles") is True and not printed:
        out.append(f"cut {i}: on-screen text")
    if pan.get("glitch") is True:
        out.append(f"cut {i}: malformed face or body")
    # a lamp-lit room is a night interior, not "daylight": asking "is it dark?" flagged every warm-lit night scene
    light = str(pan.get("light", "")).lower() or {True: "night", False: "daylight"}.get(pan.get("night"), "")
    if want_night and light == "daylight":
        out.append(f"cut {i}: daylight in a night scene")
    if want_day and light == "night":
        out.append(f"cut {i}: night look in a day scene")
    subj = cut.get("subjects", [])
    if cut["type"] != "ECU" and subj and pan.get("people") == 0:   # an ECU may be hands or a prop
        out.append(f"cut {i}: nobody in the shot, should show {', '.join(chars[c]['name'] for c in subj)}")
    if cut["type"] in ("CU", "ECU", "VO") and len(subj) == 1:
        sex = qc.written_sex(chars[subj[0]])
        seen = str(pan.get("main_person_sex", "")).lower()
        if sex and seen in ("woman", "man") and seen != sex:
            out.append(f"cut {i}: shows a {seen}, should be {chars[subj[0]]['name']}")
    return out


def check(p: Project, ep: str, clip: dict, video: Path) -> dict:
    """Problems with one rendered take (an empty list passes). Cached per take in edit/<EP>/clipqc/<take>.json."""
    out_path = p.d("edit", ep, "clipqc") / f"{video.stem}.json"
    if out_path.exists():
        old = load_json(out_path)
        if old.get("mtime") == video.stat().st_mtime_ns and old.get("rules") == RULES:
            return old
    c = p.cfg.get("qc", {})
    problems = []
    lines = assemble.heard_lines(p, ep, clip, video)
    for ln in lines:
        if not ln.get("aligned"):
            problems.append(f"line not heard: 「{ln['text']}」")
        elif ln.get("cer", 0) > float(c.get("max_line_cer", 0.35)):
            problems.append(f"line garbled (CER {ln['cer']}): 「{ln['text']}」 heard 「{ln.get('heard', '')}」")
    panels = []
    if c.get("clip_vision", True):
        try:
            times = sample_times(clip, media.probe(video)["duration"], media.shot_cuts(video))
            img = strip(p, ep, video, times)
            n = len(times)
            ans = qc.ask(p, f"clipqc/{ep}_{video.stem}", img, STRIP_PROMPT.format(n=n),
                         model=c.get("clip_vlm_model"), mmproj=c.get("clip_vlm_mmproj"), handler=c.get("clip_vlm_chat_handler"))
            panels = ans.get("panels") or []
            light = re.sub(r"\b(no|without|never)\s+(\w+\s+){0,2}?(daylight|day|sunlight|night|moonlight)\b", "",
                           p.bible["locations"][clip["location"]].get("light", ""), flags=re.I)   # "no daylight anywhere"
            want_night = bool(NIGHT.search(light)) and not DAY.search(light)
            want_day = bool(DAY.search(light)) and not NIGHT.search(light)
            for t, pan in zip(times, panels):
                k = cut_at(clip, t)
                for prob in judge_panel(p.chars, k + 1, clip["cuts"][k], pan, want_night, want_day,
                                        p.bible.get("props", {})):
                    if prob not in problems:
                        problems.append(prob)
        except Exception as e:  # noqa: BLE001 - a failed vision call must not fail the render
            log(f"clipqc {video.stem}: vision check skipped ({e})")
    res = {"rules": RULES, "mtime": video.stat().st_mtime_ns, "take": video.stem, "problems": problems, "lines": lines, "panels": panels}
    save_json(out_path, res)
    log(f"clipqc {ep}/{video.stem}: " + ("pass" if not problems else "; ".join(problems)))
    return res


def best_take(results: list[tuple[int, dict]]) -> int:
    """The take with the fewest problems; the earlier take wins a tie."""
    return min(results, key=lambda r: (len(r[1]["problems"]), r[0]))[0]
