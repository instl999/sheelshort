"""Vision checks on generated stills before they become references or keyframes.

The servers' vision model (the same one that tags location photos) lists the people it sees, with the
horizontal position of each head, plus any leftover mannequins or visible text. That is compared with what
the blockout camera should show: who is in frame, as a woman or a man, at which side of the frame.
"""
from __future__ import annotations

import json
import re

PEOPLE_PROMPT = """Look at this image carefully. Answer with ONE JSON object and nothing else:
{"people": [{"x": <horizontal position of the person's head, 0.0 = left edge, 1.0 = right edge>, "sex": "woman" | "man", "clothing": "<a few words>"}],
 "mannequins": <true if any grey mannequin, dummy or statue-like figure is visible, else false>,
 "visible_text": <true if any legible letters, signs or logos are visible, else false>}
List every real person you can see, including people only partly visible at the edges. An empty room has "people": []."""

POSITION_TOLERANCE = 0.3


def parse(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"no JSON object in model reply: {text[:200]!r}")
    raw = json.loads(m.group(0))
    people = []
    for p in raw.get("people") or []:
        try:
            x = float(p.get("x", 0.5))
        except (TypeError, ValueError):
            x = 0.5
        sex = str(p.get("sex", "")).lower()
        people.append({"x": min(1.0, max(0.0, x)), "sex": "woman" if sex.startswith(("w", "f")) else "man" if sex.startswith("m") else "?",
                       "clothing": str(p.get("clothing", ""))})
    flag = lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("true", "yes", "1")  # noqa: E731
    return {"people": people, "mannequins": flag(raw.get("mannequins", False)), "visible_text": flag(raw.get("visible_text", False))}


def expected_people(view: dict, roles: dict[str, str]) -> tuple[list[tuple[str, float]], int]:
    """From a measured camera view: the people that must be seen (sex, screen x) and how many may be seen at most
    (people just outside the frame edge or seen from behind as a foreground shoulder may or may not be counted)."""
    must, may = [], 0
    for cid, s in view.get("subjects", {}).items():
        if s["depth_m"] <= 0:
            continue
        if s["in_frame"]:
            must.append((roles.get(cid, "?"), min(1.0, max(0.0, s["x"]))))
        if -0.4 <= s["x"] <= 1.4:
            may += 1
    return sorted(must, key=lambda t: t[1]), max(may, len(must))


def check(result: dict, must: list[tuple[str, float]], at_most: int) -> list[str]:
    """Problems with a checked image; an empty list means it passes."""
    problems = []
    people = sorted(result["people"], key=lambda p: p["x"])
    if not len(must) <= len(people) <= at_most:
        problems.append(f"{len(people)} people seen, expected {len(must)}" + (f" to {at_most}" if at_most > len(must) else ""))
    if result["mannequins"]:
        problems.append("a mannequin is still visible")
    if result["visible_text"]:
        problems.append("visible text")
    if len(people) == len(must):
        for (sex, x), p in zip(must, people):
            if sex in ("woman", "man") and p["sex"] in ("woman", "man") and p["sex"] != sex:
                problems.append(f"a {p['sex']} at x={p['x']:.2f} where a {sex} should be")
            elif abs(p["x"] - x) > POSITION_TOLERANCE:
                problems.append(f"the {sex} is at x={p['x']:.2f}, expected about {x:.2f}")
    return problems
