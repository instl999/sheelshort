"""Disk space for long series: an episode's unused takes are deleted only after its final cut is approved, and only
when the disk is getting full (owner decision 2026-11-01).

`python -m sheelshort approve <project> EP` records the approval (edit/<EP>/approved.json). An approved episode keeps
everything a later re-cut needs: its chosen takes and their upscaled versions, every cast file, voice and location, and
its master. What goes: the takes that were not chosen, takes left from an older version of a clip, and the frames the
checks were judged on. No GPU work is ever repeated because of it; only switching to a take that was not chosen would
need that clip rendered again.
"""
from __future__ import annotations

import shutil
import time
from pathlib import Path

from .project import Project, load_json, log, save_json


def approved(p: Project, ep: str) -> bool:
    return (p.root / "edit" / ep / "approved.json").exists()


def approve(p: Project, ep: str) -> None:
    master = p.root / "edit" / ep / f"{p.master_name()}.mp4"
    if not master.exists():
        raise SystemExit(f"{ep}: no finished master to approve; run make or assemble first")
    save_json(p.d("edit", ep) / "approved.json", {"at": time.time(), "master": master.name,
                                                  "master_mtime": master.stat().st_mtime})
    log(f"{ep}: final cut approved")


def free_gb(p: Project) -> float:
    return shutil.disk_usage(p.root).free / 1e9


def tight(p: Project) -> bool:
    return free_gb(p) < float(p.cfg["project"].get("cleanup_below_gb", 40))


def unused_files(p: Project, ep: str) -> list[Path]:
    """Files of an approved episode that a re-cut does not need."""
    picks = load_json(p.root / "edit" / ep / "picks.json") if (p.root / "edit" / ep / "picks.json").exists() else {}
    keep = {f"{cid}_" for cid in picks}
    out = []
    clips = p.root / "clips" / ep
    for f in sorted(clips.glob("*.mp4")) if clips.exists() else []:
        stem = f.stem.removesuffix("_up").removesuffix("_foley")
        cid, _, rest = stem.partition("_")
        take = rest.rsplit("_t", 1)[-1].split("_")[0] if "_t" in rest else ""
        chosen = cid in picks and take == str(picks[cid]) and "_stale" not in f.stem
        if not chosen:
            out.append(f)
            out += [g for g in (f.with_suffix(".json"), f.with_name(f.stem + "_foley.flac")) if g.exists()]
    frames = p.root / "edit" / ep / "clipqc" / "frames"
    if frames.exists():
        out += sorted(x for x in frames.rglob("*") if x.is_file())
    return out


def clean(p: Project, ep: str) -> float:
    """Delete an approved episode's unused files; returns the GB freed (0 when not approved)."""
    if not approved(p, ep):
        return 0.0
    files = unused_files(p, ep)
    size = sum(f.stat().st_size for f in files if f.exists())
    for f in files:
        f.unlink(missing_ok=True)
    if files:
        log(f"{ep}: {len(files)} unused file(s) deleted, {size / 1e9:.2f} GB freed (final cut approved)")
    return size / 1e9


def clean_if_tight(p: Project) -> float:
    """When the disk is getting full, clean every approved episode; otherwise do nothing."""
    if not tight(p):
        return 0.0
    eps = sorted(x.parent.name for x in (p.root / "edit").glob("*/approved.json"))
    if not eps:
        return 0.0
    log(f"disk: {free_gb(p):.0f} GB free; cleaning approved episodes ({', '.join(eps)})")
    return sum(clean(p, ep) for ep in eps)
