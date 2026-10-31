"""Location libraries: any number of photos per story location, tagged once, and the best few picked per clip.

Layout: inputs/locations/<loc>/** (any images, any sub-folders), plus the older inputs/loc_<loc>*.png form.
The index lives next to the photos as inputs/locations/<loc>/library.json, keyed by file path. Each entry holds:
  sha1, dhash, width, height   computed; a changed file is re-tagged
  tags                         from the vision model (or written by the agent / a person):
                               framing, facing, features, landmarks, time_of_day, people, visible_text, caption
  tagged_by                    "vlm:<model>", "agent" or "manual"
  pin / veto                   a person's override: always use / never use
  synthetic                    a generated view, not a real photo (ranked below real ones)
  allow_people / allow_text    accept a photo the default filters would drop
Hand edits (pin, veto, allow_*, tags with tagged_by != "vlm:*") survive re-indexing.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import media

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp")

TAG_PROMPT = """You are cataloguing reference photos of a film location. Look at the photo and answer with ONE JSON object and nothing else:
{"framing": "wide" | "medium" | "detail",
 "facing": "<what the camera looks toward, 3-8 words, e.g. floor-to-ceiling windows and city skyline>",
 "features": ["<every clearly visible fixed feature of the place, 1-3 lowercase words each, e.g. window, skyline, sofa, doorway, table lamp, marble floor>"],
 "landmarks": ["<recognisable landmarks or named buildings, if any>"],
 "time_of_day": "day" | "dusk" | "night" | "unknown",
 "people": true | false,
 "visible_text": true | false,
 "caption": "<one plain sentence describing the photo>"}
"wide" shows most of the space, "medium" one area or one wall, "detail" a single object or surface.
"people" is true if any person or part of a person is visible. "visible_text" is true for any legible letters, signs, logos or watermarks."""

STOP = set("""a an the and or of in on at to from with without into onto over under behind before after his her their its
him she he they them it this that these those is are be being been as by for while who whom which then than so very
one two both each other another frame shot camera third left right centre center background foreground half
soft slowly slow toward towards across between stands stand standing holds holding looks look looking
face faces facing eyes edge just only still""".split())
# framing preference by shot type: an MS wants to see the space, a close-up only a slice of background
FRAMING_FIT = {
    "MS": {"wide": 1.0, "medium": 0.6, "detail": 0.0},
    "CU": {"medium": 1.0, "detail": 0.6, "wide": 0.4},
    "ECU": {"detail": 1.0, "medium": 0.8, "wide": 0.3},
    "OTS": {"medium": 1.0, "wide": 0.6, "detail": 0.4},
    "VO": {"medium": 1.0, "detail": 0.6, "wide": 0.4},
}
NEAR_DUPLICATE_BITS = 8
SYNONYMS = {"doorway": "door", "entrance": "door", "glass": "window", "skyscraper": "tower", "building": "tower",
            "lamplight": "lamp", "couch": "sofa", "settee": "sofa", "city": "skyline", "cityscape": "skyline"}


def words(text: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z]+", text.lower()):
        if w in STOP or len(w) < 3:
            continue
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.add(SYNONYMS.get(w, w))
    return out


def folder(inputs: Path, loc: str) -> Path:
    return inputs / "locations" / loc


def files_for(inputs: Path, loc: str) -> list[Path]:
    found = []
    if folder(inputs, loc).exists():
        found += [p for p in folder(inputs, loc).rglob("*") if p.suffix.lower() in IMAGE_EXTS]
    if inputs.exists():
        found += [p for p in inputs.iterdir() if p.suffix.lower() in IMAGE_EXTS
                  and (p.stem == f"loc_{loc}" or p.stem.startswith(f"loc_{loc}_"))]
    return sorted(found)


def index_path(inputs: Path, loc: str) -> Path:
    return folder(inputs, loc) / "library.json"


def load(inputs: Path, loc: str) -> dict:
    p = index_path(inputs, loc)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"location": loc, "entries": {}}


def save(inputs: Path, loc: str, lib: dict) -> None:
    p = index_path(inputs, loc)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(lib, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(p)


def refresh(inputs: Path, loc: str) -> dict:
    """Add new or changed photos, drop missing ones; keeps tags and hand edits of unchanged files."""
    lib = load(inputs, loc)
    old = lib["entries"]
    entries = {}
    for path in files_for(inputs, loc):
        key = path.relative_to(inputs).as_posix()
        sha1 = hashlib.sha1(path.read_bytes()).hexdigest()
        e = old.get(key, {})
        if e.get("sha1") != sha1:
            info = media.probe(path)
            keep = {k: e[k] for k in ("pin", "veto", "synthetic", "allow_people", "allow_text", "note") if k in e}
            e = {"sha1": sha1, "dhash": f"{media.dhash(path):016x}", "width": info["width"], "height": info["height"],
                 "tags": None, **keep}
        entries[key] = e
    lib["entries"] = entries
    return lib


def parse_tags(text: str) -> dict:
    """The vision model's reply -> tags; tolerant of code fences and prose around the JSON."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"no JSON object in model reply: {text[:200]!r}")
    raw = json.loads(m.group(0))
    as_list = lambda v: [str(x).strip().lower() for x in (v if isinstance(v, list) else [v]) if str(x).strip()]  # noqa: E731
    as_bool = lambda v: v if isinstance(v, bool) else str(v).strip().lower() in ("true", "yes", "1")  # noqa: E731
    framing = str(raw.get("framing", "")).lower()
    tod = str(raw.get("time_of_day", "unknown")).lower()
    return {
        "framing": framing if framing in ("wide", "medium", "detail") else "medium",
        "facing": str(raw.get("facing", "")).strip(),
        "features": as_list(raw.get("features", [])),
        "landmarks": as_list(raw.get("landmarks", [])),
        "time_of_day": tod if tod in ("day", "dusk", "night") else "unknown",
        "people": as_bool(raw.get("people", False)),
        "visible_text": as_bool(raw.get("visible_text", False)),
        "caption": str(raw.get("caption", "")).strip(),
    }


def cut_wants(cut: dict) -> set[str]:
    """What the background of a cut should show: its `bg` hint, else its composition text."""
    text = cut.get("bg") or cut.get("composition", "")
    return words(re.sub(r"\{\w+\}", " ", text))


def keywords(tags: dict) -> set[str]:
    return words(" ".join([tags.get("facing", ""), *tags.get("features", []), *tags.get("landmarks", [])]))


def usable(e: dict) -> tuple[bool, str]:
    if e.get("veto"):
        return False, "vetoed"
    t = e.get("tags") or {}
    if t.get("visible_text") and not e.get("allow_text"):
        return False, "has visible text (could leak into the video)"
    if t.get("people") and not e.get("allow_people"):
        return False, "has people in it (could be read as extra characters)"
    return True, ""


def fit(e: dict, cut_type: str, want: set[str], avoid: set[str] = frozenset()) -> float:
    """How well a photo fits one cut's background: wanted features shown, unwanted ones (out of frame in the
    cut's camera view) penalised, and framing that suits the shot type."""
    t = e.get("tags") or {}
    kw = keywords(t) if t else set()
    return (len(kw & want) - 2.0 * len(kw & avoid)
            + 0.8 * FRAMING_FIT.get(cut_type, FRAMING_FIT["CU"]).get(t.get("framing", ""), 0.3))


def default_wants(clip: dict) -> list[tuple[str, set[str], set[str]]]:
    return [(cut["type"], cut_wants(cut), set()) for cut in clip["cuts"]]


def select(lib: dict, clip: dict, time_of_day: str | None, k: int,
           wants: list[tuple[str, set[str], set[str]]] | None = None) -> list[tuple[str, str]]:
    """Up to k photos for a clip: pinned first, then greedy coverage of what each cut's background needs,
    preferring the right framing per shot type, the right time of day, real photos, and no near-duplicates.
    `wants` per cut is (shot type, wanted words, words to avoid); by default read from the composition text."""
    cand = {}
    for key, e in lib["entries"].items():
        ok, _ = usable(e)
        if ok:
            cand[key] = e
    if not cand or k <= 0:
        return []
    wants = wants or default_wants(clip)

    def base(e: dict) -> float:
        t = e.get("tags") or {}
        b = 3.0 if e.get("pin") else 0.0
        b += 0.5 if t.get("framing") == "wide" else 0.0
        if time_of_day and t.get("time_of_day") not in (None, "unknown", time_of_day):
            b -= 5.0
        return b - (0.5 if e.get("synthetic") else 0.0)

    chosen: list[tuple[str, str]] = []
    covered = [0.0] * len(wants)
    while len(chosen) < k:
        best = None
        for key, e in cand.items():
            if key in dict(chosen):
                continue
            if any(bin(int(e["dhash"], 16) ^ int(cand[c]["dhash"], 16)).count("1") <= NEAR_DUPLICATE_BITS for c, _ in chosen):
                continue
            fits = [fit(e, ct, w, a) for ct, w, a in wants]
            gain = base(e) + sum(max(0.0, f - c) for f, c in zip(fits, covered))
            if best is None or gain > best[0]:
                best = (gain, key, fits)
        if best is None or (chosen and best[0] <= 0.25):
            break
        gain, key, fits = best
        shots = [i + 1 for i, (f, c) in enumerate(zip(fits, covered)) if f > c]
        e = cand[key]
        why = ("pinned; " if e.get("pin") else "") + (f"best background for shots {shots}" if shots else "establishing view")
        t = e.get("tags") or {}
        if t:
            why += f" ({t.get('framing')}, {t.get('facing') or ', '.join(t.get('features', [])[:4])})"
        chosen.append((key, why))
        covered = [max(f, c) for f, c in zip(fits, covered)]
    return chosen


def assign(lib: dict, keys: list[str], wants: list[tuple[str, set[str], set[str]]]) -> dict[int, str]:
    """For each cut (1-based), the chosen photo that best matches its background, if any matches well:
    at least one wanted feature and none of the avoided ones."""
    out = {}
    for i, (ct, want, avoid) in enumerate(wants, 1):
        scored = [(fit(lib["entries"][k], ct, want, avoid), k) for k in keys
                  if not keywords(lib["entries"][k].get("tags") or {}) & avoid
                  and keywords(lib["entries"][k].get("tags") or {}) & want]
        if scored:
            out[i] = max(scored)[1]
    return out


def covers(lib: dict, want: set[str], avoid: set[str]) -> bool:
    """Whether any usable photo shows something wanted and nothing avoided."""
    for e in lib["entries"].values():
        kw = keywords(e.get("tags") or {})
        if usable(e)[0] and kw & want and not kw & avoid:
            return True
    return False
