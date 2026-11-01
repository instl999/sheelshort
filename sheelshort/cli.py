"""sheelshort: script -> finished vertical short-drama episodes on your ComfyUI servers.

The agent (skills/sheelshort/SKILL.md) writes bible.json and episodes/<EP>.json from the user's script; this CLI does
everything deterministic. Every command is resumable: run it again after a crash and finished work is reused.

  python -m sheelshort new <project> [--script FILE]   create projects/<project>/ (and copy the script into it)
  python -m sheelshort doctor <project>                servers, schemas, episode rules
  python -m sheelshort check <project> [EP]            validate bible + episodes (no GPU)
  python -m sheelshort cast <project>                  reference pack + cast voices
  python -m sheelshort perform <project> EP [--only K1]   act every line, choose takes, lay out dialogue tracks
  python -m sheelshort render <project> EP [--only K1] [--takes N] [--dialogue acted|h3] [--dry-run]
  python -m sheelshort assemble <project> EP           master.mp4 with subtitles, music bed, loudness
  python -m sheelshort review <project> EP             edit/<EP>/review.html (contact sheet, takes, line scores)
  python -m sheelshort make <project> EP               cast + perform + render + assemble + review
  python -m sheelshort make <project> EP --seconds 20  fast test: only the opening ~20 s, finished (edit/<EP>/master_20s.mp4)
  python -m sheelshort package <project>               delivery/: episodes, clean cuts, SRTs, cover, thumbnails, metadata
  python -m sheelshort fleet <project> [off|idle]      AutoDL servers: list them (off: power off every one no run uses;
                                                       idle: watch and power off idle ones, run in the background)
  python -m sheelshort evaluate <project> EP           A/B per line: acted vs. H3-voiced renders (edit/<EP>/dialogue_ab.md)
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

from . import assemble, cast, evaluate, h3prompt, perform, render, review
from .project import REPO, Project, log, save_json


TIME_WORDS = re.compile(r"sunlight|morning|afternoon|noon|night|dusk|dawn|sunset|moonlight|tungsten|lamp", re.I)


def project_dir(name: str) -> Path:
    p = Path(name)
    return p if p.is_dir() and (p / "bible.json").exists() else REPO / "projects" / name


def check(p: Project, eps: list[str]) -> list[str]:
    errs = []
    b = p.bible
    for c, ch in b["characters"].items():
        for k in ("name", "role", "look", "keep", "face_detail", "hair", "costume", "voice_en"):
            if not ch.get(k):
                errs.append(f"character {c}: missing {k}")
    if TIME_WORDS.search(b.get("style", "")):
        errs.append("style names a time of day or a light source; put lighting in each location's `light` "
                    "(the style opens every prompt and would override a night scene)")
    for loc, v in b["locations"].items():
        if not v.get("light"):
            errs.append(f"location {loc}: missing `light` (time of day and light sources for every shot there)")
    for ep in eps:
        scene = p.scene(ep)
        errs += [f"{ep} {e}" for e in h3prompt.check_rules(scene)]
        for clip in scene["clips"]:
            if len(h3prompt.clip_characters(clip)) > 3:
                errs.append(f"{ep} {clip['id']}: more than 3 characters; split the clip")
            for n, cut in enumerate(clip["cuts"], 1):
                if cut.get("line") and cut.get("speaker") in b["characters"] and not b["characters"][cut["speaker"]].get("voice_design") \
                        and not p.supplied(f"voice_{cut['speaker']}", (".wav", ".mp3", ".flac", ".m4a")):
                    errs.append(f"{ep} {clip['id']} cut {n}: {cut['speaker']} speaks but has no voice_design and no inputs/voice_ file")
                if cut.get("line") and not cut.get("act"):
                    errs.append(f"{ep} {clip['id']} cut {n}: line without an `act` direction (the voice will be read, not acted)")
    return errs


def doctor(p: Project) -> bool:
    ok = True
    p.wake()
    for url in p.cfg["comfy"]["servers"]:
        try:
            c = p.client(url)
            st = c.system_stats()
            dev = st["devices"][0]
            info = p.object_info(c)
            need = {"MiniMaxH3ReferenceToVideo", "MiniMaxH3AudioConditioningT8", "Drama3Generate", "TDQwen3TTSVoiceDesign",
                    "ZealmanH3PickReferenceMedia", "VHS_VideoCombine"}
            miss = need - set(info)
            log(f"{'OK  ' if not miss else 'FAIL'} {url}: ComfyUI {st['system']['comfyui_version']}, {dev['name'].split(':')[1].strip()}, "
                f"{dev['vram_free'] / 2**30:.0f}/{dev['vram_total'] / 2**30:.0f} GB free" + (f", missing nodes {sorted(miss)}" if miss else ""))
            ok &= not miss
        except Exception as e:  # noqa: BLE001
            log(f"FAIL {url}: {e}")
            ok = False
    errs = check(p, p.episodes())
    for e in errs:
        log(f"RULE {e}")
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            log(f"FAIL {tool} not on PATH")
            ok = False
    return ok and not errs


def _exit_on_signal(signum, frame):   # SIGTERM/SIGHUP end the run normally, so cleanup (the fleet's power-off) runs
    raise SystemExit(128 + signum)


def main(argv: list[str] | None = None) -> None:
    import signal
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _exit_on_signal)
    ap = argparse.ArgumentParser(prog="sheelshort", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["new", "doctor", "check", "cast", "perform", "render", "assemble", "review", "make", "free", "evaluate", "fleet", "package"])
    ap.add_argument("project")
    ap.add_argument("episode", nargs="?", help="episode id, or several separated by commas (E03,E04)")
    ap.add_argument("--script")
    ap.add_argument("--only", help="comma-separated clip ids (or line keys for perform)")
    ap.add_argument("--takes", type=int)
    ap.add_argument("--dialogue", choices=["acted", "h3"])
    ap.add_argument("--voice-ref", choices=["cast", "acted"], help="dialogue h3: voice references from the cast clip or acted takes")
    ap.add_argument("--seconds", type=float, help="test mode: only the opening clips of the episode, about this long (e.g. 20)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--fit", action="store_true", help="check: lengthen dialogue cuts that are too short for their lines")
    a = ap.parse_args(argv)
    root = project_dir(a.project)

    if a.command == "new":
        root = REPO / "projects" / a.project
        (root / "episodes").mkdir(parents=True, exist_ok=True)
        (root / "inputs").mkdir(exist_ok=True)
        if a.script:
            shutil.copy2(a.script, root / ("script" + Path(a.script).suffix))
        log(f"created {root}; the agent now writes bible.json and episodes/<EP>.json (skills/sheelshort/SKILL.md)")
        return

    p = Project(root, dry_run=a.dry_run)
    fc = p.cfg.get("fleet", {})
    if fc.get("enabled") and fc.get("core") and not fc.get("dry_run") and a.command not in ("check", "fleet", "free"):
        from . import fleet   # every server is powered on on demand: the first GPU job wakes them, the end lets them go
        p.waker = fleet.Session(p)
    try:
        _dispatch(a, p)
    finally:
        if p.waker:
            p.waker.release()


def _dispatch(a, p: Project) -> None:
    if a.dialogue:
        p.cfg["render"]["dialogue"] = a.dialogue
    if a.voice_ref:
        p.cfg["render"]["voice_ref"] = a.voice_ref
    p.limit_s = a.seconds
    only = set(a.only.split(",")) if a.only else None
    eps = a.episode.split(",") if a.episode else p.episodes()
    p.only_eps = eps if a.episode else None

    if a.command == "doctor":
        sys.exit(0 if doctor(p) else 1)
    if a.command == "check":
        for ep in eps if a.fit else []:
            e = p.episode(ep)
            changes = h3prompt.fix_jump_cuts({**p.bible, **e}) + h3prompt.fit_durations({**p.bible, **e})
            if changes:
                save_json(p.root / "episodes" / f"{ep}.json", e)
                print(f"{ep}: fitted " + "; ".join(changes))
        for ep in eps:
            pc = h3prompt.pacing(p.scene(ep))
            slow = " (slower than the reference: add reaction cuts or split long shots)" if pc["mean_shot_s"] > 1.5 * h3prompt.REFERENCE_MEAN_SHOT_S else ""
            print(f"{ep}: {pc['clips']} clips, {pc['shots']} shots, {pc['duration_s']} s, mean shot {pc['mean_shot_s']} s, "
                  f"{pc['over_5s']} over 5 s; reference mean {h3prompt.REFERENCE_MEAN_SHOT_S} s{slow}")
            for note in h3prompt.craft_notes(p.scene(ep)):
                print(f"{ep} note: {note}")
        errs = check(p, eps)
        for e in errs:
            print(e)
        print("ok" if not errs else f"{len(errs)} problem(s)")
        sys.exit(1 if errs else 0)
    if a.command == "fleet":   # python -m sheelshort fleet <project> [off|idle]
        from . import fleet
        api, fc = fleet.AutoDL(), p.cfg["fleet"]
        if a.episode == "idle":
            fleet.watch_idle(api, fc, log)
            return
        fleet.reap_idle(api, fc, log)
        for inst in [i for i in api.instances() if fleet.ours(fc, i)]:
            used = fleet.holders(inst["uuid"])
            if a.episode == "off" and inst["status"] not in ("shutdown",) and not used:
                api.power_off(inst["uuid"])
                fleet.forget(inst["uuid"])
                log(f"powered off {inst['uuid']}")
            else:
                kind = "core" if inst["uuid"] in (fc.get("core") or []) else inst.get("name")
                log(f"{inst['uuid']} {kind} {inst['status']} {inst.get('region_name')} {inst.get('gpu_spec_uuid')}"
                    + (f", in use by pid {', '.join(map(str, used))}" if used else ""))
        return
    if a.command == "package":
        from . import package
        package.run(p)
        return
    if a.command == "free":
        for url in p.cfg["comfy"]["servers"]:
            p.client(url).http.post("/free", json={"unload_models": True, "free_memory": True})
        return
    if a.command in ("cast", "make"):
        cast.images(p)
        cast.voices(p)
    eps = eps if a.command != "cast" else []
    for ep in eps:
        if a.command == "perform" or (a.command == "make" and (p.cfg["render"]["dialogue"] == "acted"
                                                             or p.cfg["render"].get("voice_ref") == "acted")):
            perform.run(p, ep, only)
    if eps and a.command in ("render", "make"):
        render.run(p, eps, only, a.takes)   # all episodes in one queue: no idle servers between episodes
    for ep in eps:
        if a.command in ("assemble", "make"):
            assemble.run(p, ep)
        if a.command in ("review", "make"):
            review.run(p, ep)
        if a.command == "evaluate":
            evaluate.run(p, ep, a.takes or 1)


if __name__ == "__main__":
    main()
