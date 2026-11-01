"""edit/<EP>/review.html: one page to audit an episode - the master, every clip take, and every line's chosen take
with its scores and the rejected alternatives."""
from __future__ import annotations

import html
from pathlib import Path

import re

from . import media
from .project import Project, load_json, log


def contact_sheet(p: Project, ep: str, clip_id: str, video: Path) -> Path:
    """One frame from the middle of every cut (performed timing when there is one), side by side."""
    tpath = p.root / "perform" / ep / "timing.json"
    tm = load_json(tpath).get(clip_id) if tpath.exists() and "_h3" not in video.stem else None
    dur = media.probe(video)["duration"]
    times = [c["start"] + c["dur"] / 2 for c in tm["cuts"]] if tm else [dur * (i + 0.5) / 6 for i in range(6)]
    tmp = p.d("edit", ep, "frames", video.stem)
    frames = []
    for i, t in enumerate(times):
        f = tmp / f"c{i + 1}.jpg"
        media._run(["ffmpeg", "-y", "-v", "error", "-ss", f"{min(t, dur - 0.05):.3f}", "-i", str(video), "-frames:v", "1", "-q:v", "2", str(f)])
        frames.append(f)
    return media.tile(frames, p.d("edit", ep, "sheets") / f"{video.stem}.jpg", cols=len(frames))


def run(p: Project, ep: str) -> Path:
    out = p.d("edit", ep)
    rel = lambda f: Path(f).resolve().relative_to(p.root).as_posix() if Path(f).is_absolute() else f  # noqa: E731
    up = "../../"
    parts = [f"<h1>{html.escape(p.bible.get('title', ''))} · {ep}</h1>"]
    if (out / "master.mp4").exists():
        pc = media.pacing(out / "master.mp4")
        parts.append(f"<p><b>Pacing</b>: {pc['duration_s']} s, {pc['shots']} shots, mean {pc['mean_shot_s']} s, median "
                     f"{pc['median_shot_s']} s, {pc['over_5s']} over 5 s &nbsp;·&nbsp; reference 全村抢 001: 151 s, 73 shots, "
                     "mean 2.07 s, median 1.84 s, 2 over 5 s</p>")
    for m in ("master.mp4", "master_acted.mp4"):
        if (out / m).exists():
            parts.append(f'<figure style="display:inline-block"><video src="{m}" controls width="300"></video><figcaption>{m}</figcaption></figure>')
    choice_path = p.root / "perform" / ep / "choice.json"
    choice = load_json(choice_path) if choice_path.exists() else {}
    for clip in p.clips(ep):
        parts.append(f"<h2>{clip['id']}</h2><p>{html.escape(clip.get('summary', ''))}</p><div class=row>")
        takes = [f for f in sorted((p.root / "clips" / ep).glob(f"{clip['id']}_*.mp4"))
                 if re.fullmatch(rf"{re.escape(clip['id'])}_(h3a?_)?t\d+", f.stem)]
        for f in takes:
            sheet = contact_sheet(p, ep, clip["id"], f)
            qcj = p.root / "edit" / ep / "clipqc" / f"{f.stem}.json"
            verdict = ("QC: " + ("pass" if not load_json(qcj)["problems"] else "; ".join(load_json(qcj)["problems"]))) if qcj.exists() else ""
            parts.append(f'<figure><video src="{up}{rel(f)}" controls width="240"></video><figcaption>{f.stem}<br><small>{html.escape(verdict)}</small></figcaption></figure>'
                         f'<img src="sheets/{sheet.name}" height="427">')
        parts.append("</div><table><tr><th>line</th><th>chosen</th><th>motion</th><th>pitch×</th><th>CER</th><th>takes</th></tr>")
        for key, c in choice.items():
            if c["clip"] != clip["id"]:
                continue
            best = next(t for t in c["takes"] if t["file"] == c["chosen"])
            alts = " ".join(f'<audio src="{up}{rel(t["file"])}" controls title="{html.escape("; ".join(t["problems"]) or "ok")}"></audio>'
                            f'<small>{t["score"]}{"" if t["ok"] else " ✗"}</small>' for t in c["takes"])
            parts.append(f"<tr><td>{html.escape(c['speaker'])}: {html.escape(c['say'])}<br><small>{html.escape(c['act'])}</small></td>"
                         f'<td><audio src="{up}{rel(c["wav"])}" controls></audio></td><td>{best["motion"]}</td>'
                         f"<td>{best['pitch_ratio']}</td><td>{best['cer']}</td><td>{alts}</td></tr>")
        parts.append("</table>")
    page = out / "review.html"
    page.write_text("<!doctype html><meta charset=utf-8><style>body{font:14px system-ui;margin:24px}"
                    ".row{display:flex;gap:12px;flex-wrap:wrap}td{vertical-align:top;padding:4px 8px;border-top:1px solid #ddd}"
                    "audio{height:28px;width:180px}</style>" + "\n".join(parts), encoding="utf-8")
    log(f"{ep}: review -> {page}")
    return page
