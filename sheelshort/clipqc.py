"""Quality check of a rendered clip, so a bad H3 take is re-rendered instead of reaching the episode.

Two checks, both cheap next to a render:
- **Dialogue** (local): every scripted line must be heard. The Whisper pass that times the subtitles also gives the
  words heard in each line's span; a line not found, or heard with a character error rate above `max_line_cer`, fails.
- **Picture** (the servers' vision model, one call per clip): one frame from the middle of every cut, side by side in a
  strip, described blind. Visible on-screen text or subtitles, a day/night look that contradicts the location's
  `light`, or a single-character close-up of the wrong sex fail.

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
  "text_or_subtitles": <true if any legible letters, captions, subtitles, logos or watermarks appear>,
  "night": <true if it is clearly night or dark, false if daylight or a brightly lit day scene>,
  "glitch": <true if a face or body is clearly malformed, melted or duplicated>}}]}}"""

NIGHT = re.compile(r"\bnight|moonlight|dark\b|midnight", re.I)
DAY = re.compile(r"\bday(light)?\b|sunlight|morning|afternoon|noon", re.I)


def cut_times(clip: dict) -> list[float]:
    t, out = 0.0, []
    for cut in clip["cuts"]:
        out.append(t + h3prompt.cut_len(cut) / 2)
        t += h3prompt.cut_len(cut)
    return out


def strip(p: Project, video: Path, clip: dict) -> Path:
    dur = media.probe(video)["duration"]
    tmp = p.d("edit", "_qc_frames", video.stem)
    frames = []
    for i, t in enumerate(cut_times(clip)):
        f = tmp / f"p{i + 1}.jpg"
        media._run(["ffmpeg", "-y", "-v", "error", "-ss", f"{min(t, dur - 0.05):.3f}", "-i", str(video), "-frames:v", "1",
                    "-q:v", "3", str(f)])
        frames.append(f)
    return media.tile(frames, tmp / "strip.jpg", cols=len(frames), cell=(288, 512))


def check(p: Project, ep: str, clip: dict, video: Path) -> dict:
    """Problems with one rendered take (an empty list passes). Cached per take in edit/<EP>/clipqc/<take>.json."""
    out_path = p.d("edit", ep, "clipqc") / f"{video.stem}.json"
    if out_path.exists() and load_json(out_path).get("mtime") == video.stat().st_mtime_ns:
        return load_json(out_path)
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
            img = strip(p, video, clip)
            n = len(clip["cuts"])
            ans = qc.ask(p, f"clipqc/{ep}_{video.stem}", img, STRIP_PROMPT.format(n=n),
                         model=c.get("clip_vlm_model"), mmproj=c.get("clip_vlm_mmproj"), handler=c.get("clip_vlm_chat_handler"))
            panels = ans.get("panels") or []
            light = re.sub(r"\b(no|without|never)\s+(\w+\s+){0,2}?(daylight|day|sunlight|night|moonlight)\b", "",
                           p.bible["locations"][clip["location"]].get("light", ""), flags=re.I)   # "no daylight anywhere"
            want_night = bool(NIGHT.search(light)) and not DAY.search(light)
            want_day = bool(DAY.search(light)) and not NIGHT.search(light)
            for i, (cut, pan) in enumerate(zip(clip["cuts"], panels), 1):
                if pan.get("text_or_subtitles") is True:
                    problems.append(f"cut {i}: on-screen text")
                if pan.get("glitch") is True:
                    problems.append(f"cut {i}: malformed face or body")
                if want_night and pan.get("night") is False:
                    problems.append(f"cut {i}: daylight in a night scene")
                if want_day and pan.get("night") is True:
                    problems.append(f"cut {i}: night look in a day scene")
                subj = cut.get("subjects", [])
                if cut["type"] in ("CU", "ECU", "VO") and len(subj) == 1:
                    sex = qc.written_sex(p.chars[subj[0]])
                    seen = str(pan.get("main_person_sex", "")).lower()
                    if sex and seen in ("woman", "man") and seen != sex:
                        problems.append(f"cut {i}: shows a {seen}, should be {p.chars[subj[0]]['name']}")
        except Exception as e:  # noqa: BLE001 - a failed vision call must not fail the render
            log(f"clipqc {video.stem}: vision check skipped ({e})")
    res = {"mtime": video.stat().st_mtime_ns, "take": video.stem, "problems": problems, "lines": lines, "panels": panels}
    save_json(out_path, res)
    log(f"clipqc {ep}/{video.stem}: " + ("pass" if not problems else "; ".join(problems)))
    return res


def best_take(results: list[tuple[int, dict]]) -> int:
    """The take with the fewest problems; the earlier take wins a tie."""
    return min(results, key=lambda r: (len(r[1]["problems"]), r[0]))[0]
