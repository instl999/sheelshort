"""A/B the H3 render graph on real clips: the same prompt, references, voices and seed under each variant.

`python -m sheelshort ab <project> <EP> --only K1,K2 [--variants current,no_mystic,tuned]` renders every clip under
every variant into `ab/<EP>/<variant>/`, then writes for each clip a frame sheet (one row per variant, one frame per
cut) and a side-by-side video with sound, and `ab/<EP>/report.md` with GPU time, size and frame rate. Judging is
the owner's: the sheets and videos are what they look at. A variant overrides config keys; the production default
changes only after that review.
"""
from __future__ import annotations

import copy
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import flows, h3prompt, media, render
from .project import PKG, Project, log, save_json

# config overrides per variant (section -> keys); "current" is production as configured
VARIANTS: dict[str, dict] = {
    "current": {},
    "no_mystic": {"render": {"h3_loras": {"MysticXXX": 0}}},
    # drama-studio's tuned U22 (YJ01): DMAD distill 0.8 + realism 0.2, MysticXXX off, SelfLift 10 steps from a 0.6
    # low-res start, RIFE 24 -> 30 in the render; 1.25 MP is the size the current graph delivers (832x1504)
    "tuned": {"workflows": {"h3_ref": "u22_tuned.api.json"},
              "render": {"h3_loras": {"MysticXXX": 0}, "h3_steps": 10, "h3_lowres_scale": 0.6, "h3_megapixels": 1.25,
                         "interp": "rife"}},
}


def variant_cfg(p: Project, name: str) -> dict:
    cfg = copy.deepcopy(p.cfg)
    for section, values in {**VARIANTS.get(name, {}), **p.cfg.get("ab", {}).get(name, {})}.items():
        cfg.setdefault(section, {}).update(copy.deepcopy(values))
    return cfg


def run(p: Project, ep: str, only: set[str] | None, variants: list[str]) -> Path:
    unknown = [v for v in variants if v not in VARIANTS and v not in p.cfg.get("ab", {})]
    if unknown:
        raise SystemExit(f"unknown variant(s) {unknown}; known: {sorted(VARIANTS)} and [ab.<name>] in the config")
    clips = [c for c in p.clips(ep) if not only or c["id"] in only]
    specs = []
    for clip in clips:
        text, refs, files, seconds = render.prompt_for(p, ep, clip, acted=False)
        specs.append((clip, text, refs, files, seconds))
    out = p.d("ab", ep)
    p.wake()
    jobs = [(v, sp) for sp in specs for v in variants]

    def one(job):
        v, (clip, text, refs, files, seconds) = job
        cfg = variant_cfg(p, v)
        seed = p.seed(f"render/{ep}/{render.stem(p, clip['id'], 1)}")   # production take 1's seed: "current" = production
        tpl = flows.load(PKG / "workflows" / cfg["workflows"]["h3_ref"])

        def build(cl):
            imgs = [p.put(cl, files[n]) for n in refs.images()]
            auds = [p.put(cl, files[n]) for n in refs.audios()]
            api = flows.h3_reference(tpl, text, seconds, imgs, auds, seed, f"sheelshort/ab_{ep}_{v}_{clip['id']}",
                                     megapixels=cfg["render"]["h3_megapixels"])
            return render.tune(p, api, cfg)

        dest = p.d("ab", ep, v) / f"{clip['id']}.mp4"
        if not dest.exists():
            t0 = time.time()
            files_out = p.run_job(f"ab/{ep}/{v}/{clip['id']}", build, p.d("ab", ep, v), clip["id"], ("gifs", "videos"),
                                  cfg["render"]["timeout_s"], units=seconds) or []
            mp4 = sorted((f for f in files_out if f.suffix == ".mp4"), key=lambda f: media.probe(f)["audio_codec"] is None)
            if not mp4:
                log(f"ab {v} {clip['id']}: failed")
                return
            mp4[0].replace(dest)
            for f in files_out:
                if f.exists() and f != dest:
                    f.unlink()
            save_json(dest.with_suffix(".json"), {"variant": v, "seed": seed, "wall_s": round(time.time() - t0),
                                                  "graph": {"workflow": cfg["workflows"]["h3_ref"],
                                                            "loras": flows.loras(render.tune(p, copy.deepcopy(tpl), cfg)),
                                                            **{k: cfg["render"].get(k) for k in
                                                               ("h3_megapixels", "h3_steps", "h3_lowres_scale", "interp")}}})
            log(f"ab {v} {clip['id']}: -> {dest.relative_to(p.root)}")

    workers = max(1, len(p.cfg["comfy"]["servers"])) * int(p.cfg["comfy"].get("in_flight", 2))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(one, jobs))
    for clip, *_ in specs:
        sheet(p, ep, clip, variants)
        side_by_side(p, ep, clip, variants)
    return report(p, ep, [c for c, *_ in specs], variants)


def cut_times(clip: dict) -> list[float]:
    """The middle of every cut, in seconds from the clip's start."""
    t, out = 0.0, []
    for cut in clip["cuts"]:
        d = float(cut.get("dur") or h3prompt.clip_len(clip) / len(clip["cuts"]))
        out.append(t + d / 2)
        t += d
    return out


def sheet(p: Project, ep: str, clip: dict, variants: list[str]) -> Path | None:
    """One row per variant, one frame per cut, labelled; 360 px wide frames."""
    rows = [(v, p.root / "ab" / ep / v / f"{clip['id']}.mp4") for v in variants]
    rows = [(v, f) for v, f in rows if f.exists()]
    if not rows:
        return None
    tmp = p.d("ab", ep, "frames")
    lines = []
    for v, f in rows:
        cells = []
        for i, t in enumerate(cut_times(clip)):
            img = tmp / f"{clip['id']}_{v}_{i}.png"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(f), "-frames:v", "1",
                            "-vf", f"scale=360:-2,drawtext=text='{v} {i + 1}':x=8:y=8:fontsize=22:fontcolor=white:"
                                   "box=1:boxcolor=black@0.6", str(img)], check=True)
            cells.append(img)
        row = tmp / f"{clip['id']}_{v}.png"
        _stack(cells, row, "hstack")
        lines.append(row)
    dest = p.root / "ab" / ep / f"{clip['id']}_sheet.jpg"
    _stack(lines, dest, "vstack")
    return dest


def _stack(images: list[Path], dest: Path, how: str) -> None:
    if len(images) == 1:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(images[0]), str(dest)], check=True)
        return
    args = ["ffmpeg", "-y", "-v", "error"]
    for im in images:
        args += ["-i", str(im)]
    filt = "".join(f"[{i}]" for i in range(len(images))) + f"{how}=inputs={len(images)}"   # rows of one clip match
    subprocess.run(args + ["-filter_complex", filt, str(dest)], check=True)


def side_by_side(p: Project, ep: str, clip: dict, variants: list[str]) -> Path | None:
    """The variants next to each other, 540 px wide each and labelled, with the first variant's sound (each variant's
    own sound is in its clip)."""
    vids = [(v, p.root / "ab" / ep / v / f"{clip['id']}.mp4") for v in variants]
    vids = [(v, f) for v, f in vids if f.exists()]
    if len(vids) < 2:
        return None
    args = ["ffmpeg", "-y", "-v", "error"]
    for _, f in vids:
        args += ["-i", str(f)]
    filt = ";".join(f"[{i}:v]fps=30,scale=540:960:force_original_aspect_ratio=decrease,pad=540:960:-1:-1,"
                    f"drawtext=text='{v}':x=12:y=12:fontsize=30:fontcolor=white:box=1:boxcolor=black@0.6[v{i}]"
                    for i, (v, _) in enumerate(vids))
    filt += ";" + "".join(f"[v{i}]" for i in range(len(vids))) + f"hstack=inputs={len(vids)}[v]"
    dest = p.root / "ab" / ep / f"{clip['id']}_side_by_side.mp4"
    subprocess.run(args + ["-filter_complex", filt, "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-crf", "20",
                           "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(dest)], check=True)
    return dest


def report(p: Project, ep: str, clips: list[dict], variants: list[str]) -> Path:
    rows = ["| clip | variant | size | fps | wall s | LoRAs on |", "|---|---|---|---|---|---|"]
    for clip in clips:
        for v in variants:
            f = p.root / "ab" / ep / v / f"{clip['id']}.mp4"
            if not f.exists():
                rows.append(f"| {clip['id']} | {v} | failed | | | |")
                continue
            m, info = media.probe(f), _load(f.with_suffix(".json"))
            on = ", ".join(f"{Path(k).stem.split('_')[0]} {s:g}" for k, s in info.get("graph", {}).get("loras", {}).items() if s)
            rows.append(f"| {clip['id']} | {v} | {m['width']}x{m['height']} | {m['fps']:g} | {info.get('wall_s', '')} | {on} |")
    dest = p.root / "ab" / ep / "report.md"
    dest.write_text(f"# H3 graph A/B, {p.root.name} {ep}\n\nSame prompt, references, voices and seed per clip. "
                    "Look at `<clip>_sheet.jpg` (rows = variants, one frame per cut) and `<clip>_side_by_side.mp4`.\n\n"
                    + "\n".join(rows) + "\n", encoding="utf-8")
    return dest


def _load(path: Path) -> dict:
    import json
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


# ---------- smoke test: one fixed clip, fixed seed, against the last accepted render ----------

SMOKE_DIR = PKG.parent / ".cache" / "smoke"
SAME, CLOSE = 4.0, 12.0   # mean absolute difference of small grey frames (0-255): same / slightly changed / different


def _grey(video: Path, t: float):
    import cv2
    import numpy as np
    out = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1", "-vf",
                          "scale=72:128,format=gray", "-f", "rawvideo", "-"], capture_output=True).stdout
    return np.frombuffer(out, np.uint8).astype(float) if len(out) == 72 * 128 else None


def smoke(p: Project, ep: str, clip_id: str, accept: bool = False) -> str:
    """Render one clip with the production settings and its production seed, and compare it with the accepted
    reference render (`.cache/smoke/<project>_<EP>_<clip>.mp4`). Run it after a server image or model change: the
    same inputs should give the same picture. `--accept` makes this render the reference."""
    import numpy as np
    SMOKE_DIR.mkdir(parents=True, exist_ok=True)
    ref = SMOKE_DIR / f"{p.root.name}_{ep}_{clip_id}.mp4"
    stamp = time.strftime("%Y%m%d_%H%M")
    key = f"ab/{ep}/smoke_{stamp}/{clip_id}"
    VARIANTS.setdefault(f"smoke_{stamp}", {})
    run(p, ep, {clip_id}, [f"smoke_{stamp}"])
    new = p.root / "ab" / ep / f"smoke_{stamp}" / f"{clip_id}.mp4"
    if not new.exists():
        return f"smoke {clip_id}: the render failed ({p.state['jobs'].get(key, {}).get('error', '')[:300]})"
    if accept or not ref.exists():
        new.replace(ref) if not ref.exists() else ref.write_bytes(new.read_bytes())
        return f"smoke {clip_id}: accepted as the reference ({ref})"
    clip = next(c for c in p.clips(ep) if c["id"] == clip_id)
    diffs = []
    for t in cut_times(clip):
        a, b = _grey(ref, t), _grey(new, t)
        if a is not None and b is not None:
            diffs.append(float(np.mean(np.abs(a - b))))
    worst = max(diffs) if diffs else 255.0
    verdict = "the same" if worst <= SAME else "slightly changed" if worst <= CLOSE else "DIFFERENT"
    msg = (f"smoke {clip_id}: {verdict} (frame difference per cut {', '.join(f'{d:.1f}' for d in diffs)}; same <= {SAME}, "
           f"slight <= {CLOSE}); new render {new}, reference {ref}")
    save_json(SMOKE_DIR / "last.json", {"at": stamp, "project": p.root.name, "ep": ep, "clip": clip_id,
                                        "diffs": diffs, "verdict": verdict})
    return msg
