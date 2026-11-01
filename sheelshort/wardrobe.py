"""Costume changes: a character's other outfits.

A character may list `outfits` (`{"suit": {"look": ..., "keep": ..., "costume": ...}}`); an episode or a clip says who
wears what (`"outfits": {"chenmo": "suit"}`, a clip's entry overriding the episode's). For the engine each outfit is a
character of its own, `chenmo__suit`: the same name, voice and face, with the outfit's look, keep and costume. Its
close-up is the base close-up re-dressed by the image editor (so the face is the same person's), and its sheet is
made from that. The episode files keep the writer's ids; clips are dressed when they are read.
"""
from __future__ import annotations

import copy

SEP = "__"
FIELDS = ("look", "keep", "costume", "hair", "face_detail")
NEEDED = ("look", "keep", "costume")


def variants(bible: dict) -> dict:
    out = {}
    for c, ch in bible.get("characters", {}).items():
        for o, v in (ch.get("outfits") or {}).items():
            base = {k: x for k, x in ch.items() if k != "outfits"}
            out[f"{c}{SEP}{o}"] = {**base, **{k: v[k] for k in FIELDS if v.get(k)}, "base": c, "outfit": o}
    return out


def wearing(episode: dict, clip: dict) -> dict[str, str]:
    """Character id -> the id of the outfit it wears in this clip (only characters out of their default costume)."""
    w = {**(episode.get("outfits") or {}), **(clip.get("outfits") or {})}
    return {c: f"{c}{SEP}{o}" for c, o in w.items() if o and o != "default"}


def _swap(obj, wear: dict):
    if isinstance(obj, str):
        for c, v in wear.items():
            obj = obj.replace("{" + c + "}", "{" + v + "}")
        return obj
    if isinstance(obj, list):
        return [_swap(x, wear) for x in obj]
    if isinstance(obj, dict):
        return {k: _swap(x, wear) for k, x in obj.items()}
    return obj


def dress(episode: dict, clip: dict) -> dict:
    """The clip with every outfit-wearing character replaced by its outfit's id (subjects, speaker, over, to, blocking
    and the {charid} references in the text)."""
    wear = wearing(episode, clip)
    if not wear:
        return clip
    out = _swap(copy.deepcopy(clip), wear)
    for cut in out["cuts"]:
        cut["subjects"] = [wear.get(c, c) for c in cut.get("subjects", [])]
        for k in ("speaker", "over", "to"):
            if cut.get(k) in wear:
                cut[k] = wear[cut[k]]
    fixed = clip.get("blocking") or (episode.get("blocking") or {}).get(clip["location"])
    if fixed:
        out["blocking"] = {wear.get(c, c): side for c, side in fixed.items()}
    return out


def check(bible: dict, episode: dict) -> list[str]:
    errs, chars = [], bible.get("characters", {})
    for c, ch in chars.items():
        for o, v in (ch.get("outfits") or {}).items():
            miss = [k for k in NEEDED if not v.get(k)]
            if miss:
                errs.append(f"character {c} outfit {o}: missing {', '.join(miss)} (the outfit's whole look, its keep "
                            "list and its costume)")
            if SEP in o or SEP in c:
                errs.append(f"character {c} outfit {o}: ids may not contain '{SEP}'")
    for where, w in [("episode", episode.get("outfits"))] + [(cl["id"], cl.get("outfits")) for cl in episode.get("clips", [])]:
        for c, o in (w or {}).items():
            if c not in chars:
                errs.append(f"{where}: outfits names unknown character {c!r}")
            elif o and o != "default" and o not in (chars[c].get("outfits") or {}):
                errs.append(f"{where}: {c} has no outfit {o!r} (bible outfits: {sorted(chars[c].get('outfits') or {})})")
    return errs
