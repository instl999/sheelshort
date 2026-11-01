"""Render each clip with MiniMax H3 from the locked reference pack.

dialogue = "acted" (default): the picture is rendered around the clip's performed dialogue track (perform.py), locked
into the audio stream, so the lips follow the acted lines; the prompt's shot times come from the performance.
dialogue = "h3": H3 voices the lines itself from the cast voice references (the M0 variant C, kept for A/B).
"""
from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import flows, h3prompt, media, perform
from .project import Project, load_json, log, save_json

IMAGE_BUDGET = 9


def refs_for(p: Project, clip: dict, acted: bool, ep: str = "") -> tuple[h3prompt.Refs, dict[str, Path]]:
    """Reference files for one clip, in H3's budget order: cast (close-up + sheet), props, location, voices."""
    refs, files = h3prompt.Refs(dialogue_track=acted), {}
    cast = h3prompt.clip_characters(clip)
    for c in cast:
        pics = [p.asset(f"char_{c}"), p.asset(f"sheet_{c}")]
        refs.char_pictures[c] = [x.name for x in pics]
        files |= {x.name: x for x in pics}
    for pr in h3prompt.clip_props(clip):
        x = p.asset(f"prop_{pr}")
        refs.prop_pictures[pr] = [x.name]
        files[x.name] = x
    room = IMAGE_BUDGET - len(refs.images())
    locs = sorted((p.root / "inputs").glob(f"loc_{clip['location']}*")) if (p.root / "inputs").exists() else []
    locs = locs or [p.asset(f"loc_{clip['location']}")]
    for x in locs[:max(1, room)]:
        refs.location_pictures.append(x.name)
        files[x.name] = x
    if len(refs.images()) > IMAGE_BUDGET:
        raise ValueError(f"{clip['id']}: {len(refs.images())} reference images; H3 takes 9. Fewer characters or props per clip.")
    if not acted:
        for c in h3prompt.speaker_order(clip)[:3]:
            v = acted_ref(p, ep, clip, c) if p.cfg["render"].get("voice_ref", "cast") == "acted" else None
            v = v or p.voice(c).with_name(f"{c}_h3.wav")
            refs.voices[c] = v.name
            files[v.name] = v
    missing = [str(f) for f in files.values() if not f.exists()]
    if missing:
        raise FileNotFoundError(f"{clip['id']}: missing references {missing}; run `sheelshort cast`")
    return refs, files


def prompt_for(p: Project, ep: str, clip: dict, acted: bool) -> tuple[str, h3prompt.Refs, dict[str, Path], int]:
    scene = p.scene(ep)
    if acted:
        timing = load_json(p.root / "perform" / ep / "timing.json")
        clip = perform.retimed(clip, timing)
        seconds = timing[clip["id"]]["seconds"]
    else:
        seconds = round(h3prompt.clip_len(clip))
    refs, files = refs_for(p, clip, acted, ep)
    override = p.root / "prompts" / ep / f"{clip['id']}.txt"
    text = override.read_text(encoding="utf-8") if override.exists() else h3prompt.build(scene, clip, refs)
    out = p.d("prompts", ep, "built") / f"{clip['id']}.txt"
    out.write_text(text, encoding="utf-8")
    return text, refs, files, seconds


def stem(p: Project, clip_id: str, take: int) -> str:
    """K1_h3_t1 when H3 voices the lines from the cast voices (default); K1_h3a_t1 when its voice references are
    acted takes of the clip's lines; K1_t1 for the performed dialogue track. Kept apart for A/B."""
    rc = p.cfg["render"]
    if rc["dialogue"] == "acted":
        return f"{clip_id}_t{take}"
    return f"{clip_id}_h3a_t{take}" if rc.get("voice_ref", "cast") == "acted" else f"{clip_id}_h3_t{take}"


def acted_ref(p: Project, ep: str, clip: dict, speaker: str, min_s: float = 3.5, max_s: float = 7.0) -> Path | None:
    """The speaker's voice reference for this clip made of acted takes of their own lines in it (perform.choose):
    the chosen take of each line, then other passing takes of the same lines until it reaches `min_s` (a short
    reference holds the timbre poorly), at most `max_s` (what H3 keeps). The reference carries the delivery as well as
    the timbre; H3 still speaks the line itself."""
    cp = p.root / "perform" / ep / "choice.json"
    if not cp.exists():
        return None
    mine = [c for c in load_json(cp).values() if c["clip"] == clip["id"] and c["speaker"] == speaker]
    if not mine:
        return None
    parts = [Path(c["wav"]) for c in mine]
    spare = sorted((t for c in mine for t in c["takes"] if t["ok"] and t["file"] != c["chosen"]), key=lambda t: -t["score"])
    total = sum(c["speech_s"] for c in mine)
    tmp = p.d("perform", ep, "refs", "parts")
    for t in spare:
        if total >= min_s:
            break
        x = tmp / f"{Path(t['file']).stem}.wav"
        perform.trim(Path(t["file"]), x, t["speech_start"], t["speech_end"])
        parts.append(x)
        total += t["speech_end"] - t["speech_start"]
    dest = p.d("perform", ep, "refs") / f"{ep}_{clip['id']}_{speaker}.wav"
    ins, filt = [], []
    for i, f in enumerate(parts):
        ins += ["-i", str(f)]
        filt.append(f"[{i}:a]aresample=44100,aformat=channel_layouts=mono,apad=pad_dur=0.35[p{i}]")
    filt.append("".join(f"[p{i}]" for i in range(len(parts))) + f"concat=n={len(parts)}:v=0:a=1,atrim=0:{max_s},"
                "loudnorm=I=-18:TP=-2:LRA=11[out]")
    media._run(["ffmpeg", "-y", "-v", "error", *ins, "-filter_complex", ";".join(filt), "-map", "[out]", "-ar", "44100",
                str(dest)])
    return dest


def clip_path(p: Project, ep: str, clip_id: str, take: int) -> Path:
    return p.root / "clips" / ep / f"{stem(p, clip_id, take)}.mp4"


def run(p: Project, eps: str | list[str], only: set[str] | None = None, takes: int | None = None) -> None:
    """Render one or more episodes in three phases, so the vision model and H3 do not keep evicting each other from
    the GPUs:
    1. render every clip (`takes` each);
    2. check every take (clipqc: dialogue locally, one vision call per clip, all together);
    3. re-take the clips whose best take still has problems (up to `qc.max_takes`), check those, and record the best
       take of every clip in edit/<EP>/picks.json.
    Several episodes share one job queue, so the servers (and any the fleet adds) stay busy from the first clip of the
    first episode to the last clip of the last one."""
    from . import clipqc
    eps = [eps] if isinstance(eps, str) else list(eps)
    rc, qcfg = p.cfg["render"], p.cfg.get("qc", {})
    acted = rc["dialogue"] == "acted"
    n = takes or int(rc["takes"])
    specs = []
    for ep in eps:
        for clip in p.clips(ep):
            if only and clip["id"] not in only:
                continue
            text, refs, files, seconds = prompt_for(p, ep, clip, acted)
            track = None
            if acted:
                tm = load_json(p.root / "perform" / ep / "timing.json")[clip["id"]]
                track = Path(tm["track"]) if tm["track"] else None
            specs.append((ep, clip, text, refs, files, seconds, track))

    def take_job(spec, t):
        ep, clip, text, refs, files, seconds, track = spec
        job = lambda: _render(p, ep, f"render/{ep}/{stem(p, clip['id'], t)}", clip, t, text, refs, files, seconds, track)  # noqa: E731
        dest = clip_path(p, ep, clip["id"], t)
        meta = dest.with_suffix(".json")
        job.renders = not dest.exists() or (meta.exists() and load_json(meta).get("fingerprint")
                                             != fingerprint(text, files, track, seconds))   # a current take needs no GPU
        return job

    jobs = [take_job(sp, t) for sp in specs for t in range(1, n + 1)]
    if p.cfg["edit"].get("music") == "auto" and not only:
        from . import assemble
        for i, ep in enumerate(eps):   # each music bed renders alongside the clips, not after them
            jobs.insert(i, lambda ep=ep: assemble.score(p, ep))
    fl = None
    if p.cfg.get("fleet", {}).get("enabled") and not p.dry:
        from .fleet import Fleet
        fl = Fleet(p)
        fl.start(sum(1 for j in jobs if getattr(j, "renders", False)))   # only real renders are work worth a server
    workers = 2 * (len(p.cfg["comfy"]["servers"]) + (int(p.cfg["fleet"]["max_extra"]) if fl else 0))

    def run_one(job):
        try:
            job()
        finally:
            if fl and getattr(job, "renders", False):
                fl.job_done()

    def pool(fn, items):
        ex = ThreadPoolExecutor(max(1, workers))
        try:
            return list(ex.map(fn, items))
        except BaseException:   # SIGTERM/Ctrl-C: drop the queued jobs instead of rendering them all before exiting
            ex.shutdown(wait=False, cancel_futures=True)
            raise
        finally:
            ex.shutdown(wait=False)

    try:
        pool(run_one, jobs)                                              # 1. render
        if not (qcfg.get("clips", True) and not acted):
            return
        results: dict[tuple[str, str], list[tuple[int, dict]]] = {}

        def check(item):
            spec, t = item
            ep, clip = spec[0], spec[1]
            dest = clip_path(p, ep, clip["id"], t)
            if dest.exists():
                results.setdefault((ep, clip["id"]), []).append((t, clipqc.check(p, ep, clip, dest)))

        def existing(spec):
            """The takes on disk rendered from the clip as it is now. A take left from an earlier version of the
            episode is a different scene: it must never be checked, picked or counted (E03 C3 once used one)."""
            ep, clip, text, refs, files, seconds, track = spec
            t, out = 1, []
            while (dest := clip_path(p, ep, clip["id"], t)).exists():
                meta = dest.with_suffix(".json")
                if not meta.exists() or load_json(meta).get("fingerprint") == fingerprint(text, files, track, seconds):
                    out.append(t)
                t += 1
            return out

        pool(check, [(sp, t) for sp in specs for t in existing(sp)])  # 2. check every take
        for _round in range(int(qcfg.get("max_takes", 2))):
            retake = []
            for sp in specs:
                rs = sorted(results.get((sp[0], sp[1]["id"]), []))
                if rs and all(r["problems"] for _, r in rs) and len(rs) < int(qcfg.get("max_takes", 2)):
                    log(f"{sp[0]} {sp[1]['id']}: no clean take yet; rendering take {len(rs) + 1}")
                    retake.append(take_job(sp, len(rs) + 1))
                    retake[-1].spec, retake[-1].t = sp, len(rs) + 1
            if not retake:
                break
            if fl:
                fl.pending += sum(1 for j in retake if j.renders)
            pool(run_one, retake)                                         # 3. re-take
            pool(check, [(j.spec, j.t) for j in retake])
        with p.lock:
            for ep in eps:
                picks_path = p.d("edit", ep) / "picks.json"
                picks = load_json(picks_path) if picks_path.exists() else {}
                for (e, cid), rs in results.items():
                    if e != ep:
                        continue
                    best = clipqc.best_take(rs)
                    picks[cid] = best
                    left = dict(rs)[best]["problems"]
                    log(f"{ep} {cid}: using take {best}" + (f" (still: {'; '.join(left)})" if left else ""))
                save_json(picks_path, picks)
    finally:
        if fl:
            fl.stop()   # every server the fleet added is powered off, also after a crash


def _render(p, ep, key, clip, t, text, refs, files, seconds, track) -> None:
    rc = p.cfg["render"]
    seed = p.seed(key.replace("_h3a_", "_h3_"))   # same seed as the cast-voice take: the reference is the only change

    def build(cl):
        imgs = [p.put(cl, files[name]) for name in refs.images()]
        if track is not None:
            return flows.h3_drive(p.wf("h3_ref"), p.wf("h3_drive"), text, seconds, imgs, p.put(cl, track), seed,
                                  f"sheelshort/{ep}_{stem(p, clip['id'], t)}", megapixels=rc["megapixels"],
                                  upscale=rc["upscale"], lock_strength=rc["lock_strength"])
        auds = [p.put(cl, files[name]) for name in refs.audios()]
        return flows.h3_reference(p.wf("h3_ref"), text, seconds, imgs, auds, seed, f"sheelshort/{ep}_{stem(p, clip['id'], t)}",
                                  megapixels=rc["h3_megapixels"])

    dest = clip_path(p, ep, clip["id"], t)
    fp = fingerprint(text, files, track, seconds)
    meta = dest.with_suffix(".json")
    if dest.exists() and meta.exists() and load_json(meta).get("fingerprint") != fp:
        # the prompt, a reference or the acted track changed since this take was rendered: keep it aside, render again
        stale = dest.with_name(dest.stem + f"_stale{int(meta.stat().st_mtime)}.mp4")
        dest.replace(stale)
        meta.unlink()
        p.forget(key)
        p.forget(f"foley/{ep}/{dest.stem}")
        foley_path(dest).unlink(missing_ok=True)
        log(f"{key}: inputs changed since the last render; old take kept as {stale.name}")
    fresh = not dest.exists()
    files_out = [dest] if not fresh else p.run_job(key, build, p.d("clips", ep), stem(p, clip["id"], t), ("gifs", "videos"), rc["timeout_s"])
    if files_out and dest not in files_out:
        # VHS saves a silent .mp4 and, with audio connected, a "-audio" one: keep the one with sound
        mp4 = sorted((f for f in files_out if f.suffix == ".mp4"), key=lambda f: media.probe(f)["audio_codec"] is None)
        mp4[0].replace(dest)
        for f in files_out:
            if f.exists() and f != dest:
                f.unlink()
        p.state["jobs"][key]["files"] = [str(dest)]   # so a re-run finds the job done instead of rendering again
        p._touched.add(key)
        p.save()
        log(f"{key}: -> {dest.name}")
    if dest.exists() and (fresh or not meta.exists()):   # a take rendered now records the inputs it was rendered from
        save_json(meta, {"fingerprint": fp, "seconds": seconds, "seed": seed})
    if dest.exists() and track is None:
        from . import assemble
        assemble.heard_lines(p, ep, clip, dest)   # subtitle timing, computed while the other clips still render
    if dest.exists() and track is not None and rc.get("foley", True):
        foley(p, ep, clip, dest, seconds)


def fingerprint(text: str, files: dict[str, Path], track: Path | None, seconds) -> str:
    h = hashlib.sha1(f"{text}|{seconds}".encode())
    for name in sorted(files):
        h.update(name.encode() + files[name].read_bytes())
    if track is not None:
        h.update(track.read_bytes())
    return h.hexdigest()[:16]


def foley_path(video: Path) -> Path:
    return video.with_name(video.stem + "_foley.flac")


def foley(p: Project, ep: str, clip: dict, video: Path, seconds: float) -> Path | None:
    """Room tone and synchronised foley for an acted clip (its render carries only the dialogue track)."""
    sounds = " ".join(c.get("sound", "") for c in clip["cuts"] if c.get("sound"))
    prompt = f"{clip.get('soundscape', '')} {sounds}".replace("There is no background music.", "").strip()
    key = f"foley/{ep}/{video.stem}"
    out = p.run_job(key, lambda cl: flows.mmaudio(p.put(cl, video), seconds, prompt, p.seed(key), f"sheelshort/{ep}_{video.stem}_foley"),
                    p.d("clips", ep), video.stem + "_foley", ("audio",), 900)
    return out[0] if out else None
