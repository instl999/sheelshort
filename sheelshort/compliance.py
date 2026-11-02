"""Words on screen and in the dialogue against platform rules, before anything renders.

`[compliance] terms` (the project's or the repo's config) are words that must not appear in any line, episode title
or name card: `check` fails on them. Built in, as notes: what platforms reject as traffic diversion (导流), a phone
number, a web address, a WeChat or QQ handle, "scan the code"; the writer decides whether the scene needs it.
The visible AI label and the AIGC metadata are the edit's (assemble.ai_label_events, aigc_metadata).
"""
from __future__ import annotations

import re

from .project import Project

DIVERSION = [
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "a mobile phone number"),
    (re.compile(r"https?://|www\.|\.com\b|\.cn\b", re.I), "a web address"),
    (re.compile(r"微信号|加微信|加我微信|加V|VX|vx|QQ号|扫码|扫一扫|二维码|私信我"), "a contact handle or 'scan the code'"),
]


def texts(p: Project, eps: list[str]) -> list[tuple[str, str]]:
    """(where, text) for everything a viewer reads or hears."""
    out = [(f"character {c} card", f"{v.get('name', '')} {v.get('card', '') if isinstance(v.get('card'), str) else ''}")
           for c, v in p.bible.get("characters", {}).items()]
    out += [(f"location {k}", v.get("name_zh", "")) for k, v in p.bible.get("locations", {}).items()]
    for ep in eps:
        e = p.episode(ep)
        out.append((f"{ep} title", f"{e.get('title', '')} {e.get('logline', '')}"))
        for clip in e["clips"]:
            for n, cut in enumerate(clip["cuts"], 1):
                for k in ("line", "say"):
                    if cut.get(k):
                        out.append((f"{ep} {clip['id']} cut {n}", cut[k]))
    return out


def problems(p: Project, eps: list[str]) -> list[str]:
    terms = [t for t in p.cfg.get("compliance", {}).get("terms", []) if t]
    return [f"{where}: {t!r} is on the compliance list ([compliance] terms)"
            for where, text in texts(p, eps) for t in terms if t in text]


def notes(p: Project, eps: list[str]) -> list[str]:
    return [f"{where}: {why} ({m.group(0)!r}); platforms reject traffic diversion"
            for where, text in texts(p, eps) for rx, why in DIVERSION for m in [rx.search(text)] if m]
