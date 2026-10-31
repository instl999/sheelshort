"""Vision checks on the casting pack before anything is rendered from it.

The servers' own vision model (Qwen3.8 through the llama.cpp nodes) looks at each close-up and three-view sheet and
answers in JSON; the engine compares the answer with the bible. A close-up must show one person of the right sex and
roughly the written age, with every identity anchor (`keep`) visible as written; a sheet must show the same person three
times (front, side, back) in the written costume. Identity anchors that exist only in the text fail silently
downstream (M0: a beauty mark never appeared, a scar landed on the nose), so they are checked here, once, cheaply.
"""
from __future__ import annotations

import hashlib
import json
import re

from . import flows
from .project import Project, log, save_json

# Blind first: the model describes what it sees without being shown the description, so it cannot just agree with it
# (a primed model confirmed a scar "across the left eyebrow" that was drawn on the nose bridge). The engine compares.
PORTRAIT_PROMPT = """Describe the person in this photo precisely. Answer with ONE JSON object and nothing else:
{"people": <number>, "sex": "woman" | "man", "apparent_age": <years>,
 "hair": "<colour, length, style; mention any grey>",
 "face_marks": [{"kind": "scar | mole | beauty mark | other", "on": "<facial feature it is on or crosses, without left/right: eyebrow, eye, cheekbone, cheek, nose bridge, upper lip, lip corner, chin, forehead, jaw>", "image_side": "<which half of the IMAGE it appears in, as you look at the picture: left | right | center>"}],
 "eyewear": "<none or describe>", "facial_hair": "<none or describe>", "clothing": "<describe, with colours>",
 "text_or_watermark": <true/false>}
List every scar, mole and beauty mark you can see; an empty list if none."""

SHEET_PROMPT = """This should be a character reference sheet. Answer with ONE JSON object and nothing else:
{"figures": <number of full-body figures>, "same_person": <true if all figures have the same face, hair and clothes>,
 "views": "<e.g. front, side, back>", "hair": "<colour and style>", "clothing": "<describe, with colours>",
 "text_or_watermark": <true if any letters, labels or watermarks are visible>}"""

MARKS = {"scar": "scar", "mole": "mole", "beauty mark": "mole", "freckle": "mole", "birthmark": "mole"}
REGIONS = {"eyebrow": {"eyebrow", "brow", "forehead"}, "brow": {"eyebrow", "brow", "forehead"},
           "eye": {"eye", "cheekbone", "cheek"}, "cheekbone": {"cheekbone", "cheek", "eye"}, "cheek": {"cheek", "cheekbone", "eye"},
           "lip": {"lip", "mouth"}, "lips": {"lip", "mouth"}, "mouth": {"lip", "mouth"}, "nose": {"nose"},
           "chin": {"chin", "jaw"}, "jaw": {"jaw", "chin"}, "forehead": {"forehead", "eyebrow", "brow"}}
COLOURS = ("black", "white", "grey", "gray", "red", "pink", "blue", "green", "olive", "brown", "yellow", "gold", "silver",
           "purple", "orange", "beige", "khaki", "navy", "charcoal")
MARK_RE = re.compile(r"\b(scar|mole|beauty mark|freckle|birthmark)\b([^,;.]*)")

AGE_TOLERANCE = 8


def parse(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"no JSON in the vision model's reply: {text[:200]!r}")
    return json.loads(m.group(0))


def ask(p: Project, key: str, image, prompt: str, model: str | None = None, mmproj: str | None = None,
        handler: str | None = None) -> dict:
    """One blind question about one image to the servers' vision model. The model is unloaded afterwards so the GPU
    memory goes back to the video model."""
    v = p.cfg["qc"]
    key = f"{key}_{hashlib.sha1(image.read_bytes() + prompt.encode()).hexdigest()[:8]}"   # a new image is asked afresh
    out = p.run_job(key, lambda cl: flows.vlm_describe(p.put(cl, image), prompt, model or v["vlm_model"], mmproj or v["vlm_mmproj"],
                                                       handler or v["vlm_chat_handler"], p.seed(key), max_tokens=900,
                                                       max_size=1536, offload=True),
                    p.d("assets", "qc"), key.replace("/", "_"), ("text",), 900)
    if not out:
        raise RuntimeError(f"{key}: the vision check did not run")
    return parse(out[0].read_text(encoding="utf-8"))


def anchors(ch: dict) -> list[str]:
    return [a.strip() for a in re.split(r",| and ", ch["keep"]) if a.strip()]


def written_age(ch: dict) -> int | None:
    m = re.search(r"(\d{2})-year-old", ch["look"])
    return int(m.group(1)) if m else None


def written_sex(ch: dict) -> str | None:
    words = (ch.get("role", "") + " " + ch["look"]).lower()
    if re.search(r"\b(woman|girl|female|lady|mother|wife)\b", words):
        return "woman"
    if re.search(r"\b(man|boy|male|father|husband|businessman|farmer)\b", words):
        return "man"
    return None


def expected_marks(ch: dict) -> list[dict]:
    """Identity marks in the character's `keep` list: kind, facial region and the person's own side."""
    out = []
    for m in MARK_RE.finditer(ch["keep"].lower()):
        rest = m.group(2)
        side = re.search(r"\b(left|right)\b", rest)
        feat = next((f for f in REGIONS if re.search(rf"\b{f}\b", rest)), None)
        out.append({"text": m.group(0).strip(), "kind": MARKS[m.group(1)], "side": side and side.group(1), "feature": feat})
    return out


def check_portrait(p: Project, c: str, image, attempt: int = 0) -> tuple[list[str], dict]:
    """Problems with a close-up; problems starting with "side:" mean the mark is there but on the other side."""
    ch = p.chars[c]
    r = ask(p, f"qc/char_{c}_a{attempt}", image, PORTRAIT_PROMPT)
    problems = []
    if r.get("people") != 1:
        problems.append(f"{r.get('people')} people in a close-up")
    sex = written_sex(ch)
    if sex and str(r.get("sex", "")).lower() != sex:
        problems.append(f"looks like a {r.get('sex')}, written as a {sex}")
    age = written_age(ch)
    try:
        seen_age = int(r.get("apparent_age"))
    except (TypeError, ValueError):
        seen_age = None
    if age and seen_age and abs(seen_age - age) > AGE_TOLERANCE:
        problems.append(f"looks about {seen_age}, written as {age}")
    if r.get("text_or_watermark") is True:
        problems.append("text or watermark visible")
    seen = [{"kind": MARKS.get(str(m.get("kind", "")).lower(), str(m.get("kind", "")).lower()),
             "on": str(m.get("on", "")).lower(), "side": str(m.get("image_side", "")).lower()} for m in r.get("face_marks") or []]
    for want in expected_marks(ch):
        region = REGIONS.get(want["feature"], set())
        hits = [m for m in seen if m["kind"] == want["kind"] and (not region or any(w in m["on"] for w in region))]
        image_side = {"left": "right", "right": "left"}.get(want["side"])   # the person's left is the image's right
        if not hits:
            where = ", ".join(f"{m['kind']} on {m['on']} ({m['side']} of image)" for m in seen) or "no marks"
            problems.append(f"missing or misplaced: {want['text']} (seen: {where})")
        elif image_side and not any(m["side"] in (image_side, "center") for m in hits):
            problems.append(f"side: {want['text']} is on the person's {'right' if want['side'] == 'left' else 'left'}")
    keep = ch["keep"].lower()
    if "glasses" in keep and str(r.get("eyewear", "none")).lower().startswith("none"):
        problems.append("glasses missing")
    if re.search(r"gr[ae]y[^,;]*\b(hair|temples)\b|\b(hair|temples)\b[^,;]*gr[ae]y", keep) and not re.search(r"gr[ae]y", str(r.get("hair", "")).lower()):
        problems.append("grey hair missing")
    problems += _colour_problems(ch["costume"], str(r.get("clothing", "")), "clothing")
    return problems, r


def check_sheet(p: Project, c: str, image, attempt: int = 0) -> tuple[list[str], dict]:
    ch = p.chars[c]
    r = ask(p, f"qc/sheet_{c}_a{attempt}", image, SHEET_PROMPT)
    problems = []
    if r.get("figures") != 3:
        problems.append(f"{r.get('figures')} figures, expected 3 views")
    if r.get("same_person") is not True:
        problems.append("the views are not the same person")
    if r.get("text_or_watermark") is True:
        problems.append("text or labels visible")
    problems += _colour_problems(ch["costume"], str(r.get("clothing", "")), "costume")
    return problems, r


def _colour_problems(written: str, seen: str, what: str) -> list[str]:
    """The first garment's colour words must be seen (a lenient check: models name shades differently)."""
    first = written.lower().split(",")[0]
    want = {c for c in COLOURS if re.search(rf"\b{c}\b", first)} - {"gray"}
    seen = seen.lower().replace("gray", "grey")
    if want and not any(c in seen for c in want | ({"grey"} if "charcoal" in want else set())):
        return [f"{what}: written '{first.strip()}', seen '{seen[:80]}'"]
    return []


def align_text(p: Project, c: str, problems: list[str], seen: dict) -> list[str]:
    """The approved image wins over the text (DESIGN §0.4): rewrite the character's look/keep/face_detail so they
    describe what the close-up actually shows, for the problems text can fix: a mark on the other side, a mark on another
    feature, a different apparent age. bible.json is rewritten; returns the problems that remain."""
    ch = p.chars[c]
    fields = ("look", "keep", "face_detail")
    left = []
    for pr in problems:
        if pr.startswith("side:"):
            mark = pr.split(": ", 1)[1].split(" is on")[0]
            kind = re.match(r"(scar|mole|beauty mark|freckle|birthmark)", mark).group(1)
            for f in fields:
                ch[f] = re.sub(rf"({kind}[^,;.]*?)\b(left|right)\b",
                               lambda m: m.group(1) + ("right" if m.group(2) == "left" else "left"), ch[f])
            log(f"qc char_{c}: text corrected to the image: {mark} -> other side")
        elif pr.startswith("missing or misplaced:"):
            mark = pr.split(": ", 1)[1].split(" (seen")[0]
            kind = MARKS[re.match(r"(scar|mole|beauty mark|freckle|birthmark)", mark).group(1)]
            found = [m for m in seen.get("face_marks") or [] if MARKS.get(str(m.get("kind", "")).lower()) == kind]
            if not found:
                left.append(pr)
                continue
            where = found[0]["on"]
            side = {"left": "right", "right": "left"}.get(str(found[0].get("image_side", "")).lower())
            new = f"{mark.split()[0] if mark.split()[0] in ('scar', 'mole') else kind} on the {side + ' ' if side else ''}{where}"
            pattern = re.compile(rf"\b{re.escape(mark.split()[0])}\b[^,;.]*", re.I)
            for f in fields:
                ch[f] = pattern.sub(new, ch[f], count=1)
            log(f"qc char_{c}: text corrected to the image: '{mark}' -> '{new}'")
        elif pr.startswith("looks about"):
            age = int(re.search(r"looks about (\d+)", pr).group(1))
            ch["look"] = re.sub(r"\b\d{2}-year-old", f"{age}-year-old", ch["look"], count=1)
            log(f"qc char_{c}: text corrected to the image: age {age}")
        else:
            left.append(pr)
    with p.lock:
        p.bible["characters"][c] = ch
        save_json(p.root / "bible.json", p.bible)
    return left


def locked(p: Project) -> bool:
    """A cast that has been rendered is canon: its images are never regenerated, only the text aligned to them."""
    return any((p.root / "clips").glob("*/*.mp4")) if (p.root / "clips").exists() else False


def report(p: Project, results: dict) -> None:
    with p.lock:   # characters are checked in parallel threads
        path = p.d("assets") / "qc.json"
        old = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        old.update(results)
        save_json(path, old)
    for name, r in results.items():
        log(f"qc {name}: " + ("pass" if not r["problems"] else "FAIL " + "; ".join(r["problems"])))
