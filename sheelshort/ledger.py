"""Season ledger and series arc: what is true at the end of every episode, and what each episode is for.

The ledger is computed, never hand-kept: it walks the episodes in story order from what they say.
- Outfits come from the episodes' `outfits` (wardrobe.py): who wears what when the episode ends.
- `changes` (an episode's list) are what the story changes, each after a clip (`"after": "K3"`) or at the end:
  `{"who": "linwan", "injury": {"left_hand": "a white gauze bandage wrapped round her left palm"}}`,
  `{"who": "linwan", "heals": ["left_hand"]}`, `{"who": "linwan", "learns": ["father"]}`,
  `{"prop": "bangle", "where": "in the jewellery tray on the dresser", "holder": "zhoumin"}`,
  `{"prop": "letter", "gone": "burned in the fireplace"}` (and `{"prop": "letter", "back": "a copy turns up"}`).
- `when` is the story day and time an episode starts ("第3天 傍晚"); `jump` says time has passed since the last one
  ("三天后"). Without a jump, an episode continues straight from the one before.
- Facts are the bible's `facts` (`{"father": "沈怀山 is 唐宁's father"}`); a cut whose line depends on one says so
  (`"knows": ["father"]`) and the speaker must have learned it by then.

What it refuses (in `check`): a prop used after it is gone; a line that uses a fact its speaker has not learned; a
change or `knows` naming an unknown character, prop or fact; an outfit change across two continuous episodes (no
`jump`, no outfit change written in the clip). An injury in force is written into every later prompt of that
character, so a bandage does not vanish between episodes.

The arc is `arc.json` in the project: one row per episode, `{"ep", "hook", "conflict", "shape", "turn",
"cliffhanger", "paywall"}`, written from the script breakdown before any episode. `check` notes the classic failures
of long runs: the same conflict shape or the same places three episodes running, and an episode that does not open on
the cliffhanger before it (`picks_up`, or the same people as the last clip before).
"""
from __future__ import annotations

import copy
from pathlib import Path

from . import h3prompt, wardrobe
from .project import Project, load_json, save_json

REPEAT = 3   # the same shape or places this many episodes in a row is a note


def _base(p: Project, c: str) -> str:
    return (p.chars.get(c) or {}).get("base", c)


def _cut_people(cut: dict) -> list[str]:
    return [x for x in cut.get("subjects", []) + [cut.get("speaker"), cut.get("over")] if x]


def build(p: Project) -> dict:
    """The ledger: per episode, the state at its start and end, the injuries in force per clip, and its problems."""
    chars, props, facts = p.bible.get("characters", {}), p.bible.get("props", {}), p.bible.get("facts", {})
    state = {"outfits": {}, "injuries": {}, "carries": {}, "knows": {}, "props": {}}
    out, prev_ep = [], None
    for ep in p.episodes():
        e = p.episode(ep)
        start = copy.deepcopy(state)
        probs, marks, seen = [], {}, set()
        changes = e.get("changes") or []
        clip_ids = [c["id"] for c in e["clips"]]
        for ch in changes:
            if ch.get("after") and ch["after"] not in clip_ids:
                probs.append(f"changes: after {ch['after']!r} is not a clip of {ep}")
            if ch.get("who") and ch["who"] not in chars:
                probs.append(f"changes: unknown character {ch['who']!r}")
            if ch.get("prop") and ch["prop"] not in props:
                probs.append(f"changes: unknown prop {ch['prop']!r} (props are the bible's)")
            for f in ch.get("learns", []):
                if f not in facts:
                    probs.append(f"changes: unknown fact {f!r} (facts are the bible's `facts`)")
        for clip in e["clips"]:
            marks[clip["id"]] = {c: dict(v) for c, v in state["injuries"].items() if v}
            wear = wardrobe.wearing(e, clip)
            for n, cut in enumerate(clip["cuts"], 1):
                where = f"{clip['id']} cut {n}"
                for pr in cut.get("props", []):
                    gone = state["props"].get(pr, {}).get("gone")
                    if gone:
                        probs.append(f"{where}: prop {pr} is used, but it is gone since {gone} "
                                     "(bring it back with a change `back`, or use another prop)")
                who = cut.get("speaker") or next(iter(cut.get("subjects", [])), None)
                for f in cut.get("knows", []):
                    if f not in facts:
                        probs.append(f"{where}: knows unknown fact {f!r}")
                    elif who and f not in state["knows"].get(who, []):
                        probs.append(f"{where}: {who}'s line uses {f!r} ({facts[f]}), which {who} has not learned yet "
                                     "(a change `learns` in this or an earlier episode)")
                for c in _cut_people(cut):
                    if c in seen or c not in chars:
                        continue
                    seen.add(c)
                    now = wear.get(c, c).split(wardrobe.SEP)[1] if wear.get(c) else "default"
                    before = state["outfits"].get(c)
                    written = c in (clip.get("outfits") or {})
                    if prev_ep and before and before != now and not e.get("jump") and not written:
                        probs.append(f"{where}: {c} wears {now!r} but {prev_ep} ended with {c} in {before!r}; "
                                     f"{ep} has no `jump` (time passing), so the outfit would change between two "
                                     "continuous episodes")
            for c in {x for cut in clip["cuts"] for x in _cut_people(cut)}:
                if c in chars:
                    w = wear.get(c)
                    state["outfits"][c] = w.split(wardrobe.SEP)[1] if w else "default"
            _apply(state, [ch for ch in changes if ch.get("after") == clip["id"]])
        _apply(state, [ch for ch in changes if not ch.get("after")])
        out.append({"ep": ep, "when": e.get("when", ""), "jump": e.get("jump", ""), "start": start,
                    "end": copy.deepcopy(state), "marks": marks, "problems": probs})
        prev_ep = ep
    return {"episodes": out}


def _apply(state: dict, changes: list[dict]) -> None:
    for ch in changes:
        c = ch.get("who")
        if c:
            inj = state["injuries"].setdefault(c, {})
            inj.update(ch.get("injury") or {})
            for part in ch.get("heals", []):
                inj.pop(part, None)
            knows = state["knows"].setdefault(c, [])
            knows += [f for f in ch.get("learns", []) if f not in knows]
            carries = state["carries"].setdefault(c, [])
            carries += [x for x in ch.get("carries", []) if x not in carries]
            state["carries"][c] = [x for x in carries if x not in ch.get("drops", [])]
        pr = ch.get("prop")
        if pr:
            st = state["props"].setdefault(pr, {})
            for k in ("where", "holder"):
                if k in ch:
                    st[k] = ch[k]
            if ch.get("gone"):
                st["gone"] = ch["gone"]
            if ch.get("back"):
                st.pop("gone", None)


_cache: dict[tuple, dict] = {}


def cached(p: Project) -> dict:
    """build(), reused while no episode file or the bible has changed (prompts ask for it clip by clip)."""
    files = [p.root / "bible.json"] + sorted((p.root / "episodes").glob("*.json"))
    key = (str(p.root),) + tuple((f.name, f.stat().st_mtime_ns) for f in files if f.exists())
    if key not in _cache:
        _cache.clear()
        _cache[key] = build(p)
    return _cache[key]


def marks(p: Project, ep: str, clip_id: str) -> dict[str, dict[str, str]]:
    """Injuries in force when a clip starts: base character id -> {part: what is seen}."""
    for row in cached(p)["episodes"]:
        if row["ep"] == ep:
            return row["marks"].get(clip_id, {})
    return {}


def problems(p: Project, eps: list[str] | None = None) -> list[str]:
    led = cached(p)
    return [f"{row['ep']} ledger: {x}" for row in led["episodes"] if not eps or row["ep"] in eps for x in row["problems"]]


def write(p: Project) -> Path:
    """ledger.json and ledger.md (one table row per episode) in the project."""
    led = build(p)
    save_json(p.root / "ledger.json", led)
    rows = ["| episode | when | outfits at the end | injuries | carrying | props moved or gone | learned |",
            "|---|---|---|---|---|---|---|"]
    for row in led["episodes"]:
        s, e = row["start"], row["end"]
        fits = ", ".join(f"{c} {o}" for c, o in sorted(e["outfits"].items()) if o != "default") or "-"
        inj = "; ".join(f"{c}: {', '.join(v.values())}" for c, v in sorted(e["injuries"].items()) if v) or "-"
        car = "; ".join(f"{c}: {', '.join(v)}" for c, v in sorted(e["carries"].items()) if v) or "-"
        prs = "; ".join(f"{k}: {v.get('gone') and 'gone (' + v['gone'] + ')' or v.get('where') or ''}"
                        + (f" ({v['holder']})" if v.get("holder") and not v.get("gone") else "")
                        for k, v in sorted(e["props"].items()) if v != s["props"].get(k)) or "-"
        new = "; ".join(f"{c}: {', '.join(f for f in v if f not in s['knows'].get(c, []))}"
                        for c, v in sorted(e["knows"].items()) if set(v) - set(s["knows"].get(c, []))) or "-"
        when = " / ".join(x for x in (row["jump"], row["when"]) if x) or "continues"
        rows.append(f"| {row['ep']} | {when} | {fits} | {inj} | {car} | {prs} | {new} |")
    md = p.root / "ledger.md"
    md.write_text(f"# Season ledger, {p.root.name}\n\nComputed by `python -m sheelshort ledger` from the episodes "
                  "(outfits, `changes`, `when`/`jump`, `knows`). Each row is the state when the episode ends.\n\n"
                  + "\n".join(rows) + "\n", encoding="utf-8")
    return md


# ---------- the arc ----------

def arc(p: Project) -> list[dict]:
    f = p.root / "arc.json"
    if not f.exists():
        return []
    data = load_json(f)
    return data["episodes"] if isinstance(data, dict) else data


def arc_problems(p: Project) -> list[str]:
    rows = {r.get("ep"): r for r in arc(p)}
    if not rows:
        return []
    errs = []
    eps = p.episodes()
    for ep in eps:
        r = rows.get(ep)
        if not r:
            errs.append(f"arc: {ep} has an episode file but no row in arc.json (its hook, conflict and cliffhanger)")
            continue
        miss = [k for k in ("hook", "conflict", "shape", "cliffhanger") if not r.get(k)]
        if ep == eps[-1] and len(rows) <= len(eps):
            miss = [k for k in miss if k != "cliffhanger"]   # the series' last episode may close instead
        if miss:
            errs.append(f"arc: {ep} is missing {', '.join(miss)}")
    return errs


def arc_notes(p: Project) -> list[str]:
    """Repetition across the run and cliffhanger-to-opener links (notes, not errors: the writer judges)."""
    rows = arc(p)
    if not rows:
        return []
    notes = []
    have = set(p.episodes())
    by_ep = [r for r in rows if r.get("ep")]
    for i in range(len(by_ep) - REPEAT + 1):
        run = by_ep[i:i + REPEAT]
        shapes = {(r.get("shape") or "").strip().lower() for r in run}
        if len(shapes) == 1 and "" not in shapes:
            notes.append(f"arc: {run[0]['ep']}-{run[-1]['ep']} all have the conflict shape {shapes.pop()!r}; "
                         "vary the beat (long runs die of sameness)")
    places = {ep: frozenset(base_locations(p, ep)) for ep in have}
    order = [r["ep"] for r in by_ep if r["ep"] in have]
    for i in range(len(order) - REPEAT + 1):
        run = order[i:i + REPEAT]
        if len({places[e] for e in run}) == 1 and places[run[0]]:
            notes.append(f"arc: {run[0]}-{run[-1]} all play in the same places ({', '.join(sorted(places[run[0]]))})")
    for a, b in zip(order, order[1:]):
        ra = next(r for r in by_ep if r["ep"] == a)
        eb = p.episode(b)
        if not ra.get("cliffhanger") or eb.get("picks_up"):
            continue
        last = {_base(p, c) for cut in p.episode(a)["clips"][-1]["cuts"] for c in _cut_people(cut)}
        first = {_base(p, c) for cut in eb["clips"][0]["cuts"] for c in _cut_people(cut)}
        if not last & first:
            notes.append(f"arc: {b} does not open on {a}'s cliffhanger ({ra['cliffhanger']}): its first clip shares "
                         f"nobody with {a}'s last; open on it, or say how it picks up (`picks_up`)")
    return notes


def base_locations(p: Project, ep: str) -> set[str]:
    return {c["location"].split(wardrobe.SEP)[0] for c in p.episode(ep)["clips"]}


def apply_marks(p: Project, ep: str, clip: dict) -> dict:
    """The clip with the injuries in force written into it (`marks`, read by h3prompt.build)."""
    m = marks(p, ep, clip["id"])
    if not m:
        return clip
    people = set(h3prompt.clip_characters(clip))
    hit = {c: v for c, v in m.items() if any(_base(p, x) == c for x in people)}
    return {**clip, "marks": hit} if hit else clip
