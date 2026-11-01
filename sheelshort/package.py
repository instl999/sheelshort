"""The delivery package (DESIGN §S8): everything a platform or distributor needs, in projects/<name>/delivery/.

  delivery/
    cover.jpg                 9:16 series poster: the leads composed from their locked references, title typeset locally
    episodes/E01.mp4          burned-in subtitles
    episodes/E01_clean.mp4    no subtitles (dubbing, re-subtitling)
    episodes/E01.srt
    thumbs/E01.jpg            the episode's hook frame with the episode title
    metadata.json             series and episode titles, loglines, durations, cast
    AI_DISCLOSURE.txt

Image models garble Chinese lettering, so the poster art is generated without text and every title is drawn with
ffmpeg in the subtitle font.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

from . import flows, media
from .project import Project, log, save_json


def leads(p: Project, k: int = 2) -> list[str]:
    """The characters who speak the most lines across the episodes."""
    count: dict[str, int] = {}
    for ep in p.episodes():
        for clip in p.episode(ep)["clips"]:
            for cut in clip["cuts"]:
                if cut.get("line"):
                    count[cut["speaker"]] = count.get(cut["speaker"], 0) + 1
    return [c for c, _ in sorted(count.items(), key=lambda kv: -kv[1])][:k]


def poster_art(p: Project) -> Path | None:
    cast = leads(p)
    loc = next(iter(p.bible["locations"]))
    srcs = [p.asset(f"char_{c}") for c in cast] + [p.asset(f"loc_{loc}")]
    srcs = [s for s in srcs if s.exists()][:3]
    if not srcs:
        return None
    who = " and ".join(f"the {p.chars[c]['role']} from image {i}" for i, c in enumerate(cast, 1))
    prompt = (f"A dramatic vertical 9:16 movie poster for a Chinese short drama: {who} in the foreground, facing the camera "
              f"with intense expressions, the setting from image {len(srcs)} behind them, cinematic lighting, high contrast, "
              "rich colour grade, photorealistic. Keep every face identical to the reference images. Leave the top quarter of "
              "the image as calm dark sky or background for a title. No text, no letters, no logos, no watermark.")
    key = "package/poster"
    files = p.run_job(key, lambda cl: flows.edit(p.wf("edit"), prompt, [p.put(cl, s) for s in srcs], 1664, p.seed(key),
                                                 "sheelshort/poster", aspect="9:16", background="#101010"),
                      p.d("delivery", "_work"), "poster_art", ("images",), 900)
    return files[0] if files else None


def title_card(image: Path, dest: Path, title: str, subtitle: str = "", font: str = "Heiti SC",
               fonts_dir: str = "/System/Library/Fonts") -> Path:
    """Draw a title (and a smaller subtitle) over an image at 1080x1920 in the subtitle font."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    ass = dest.with_suffix(".ass")
    ass.write_text(f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Title,{font},{96 if len(title) <= 10 else 72},&H0000D7FF,&H0000D7FF,&H00101010,&H96000000,-1,0,0,0,100,100,4,0,1,6,3,8,60,60,170,1
Style: Sub,{font},58,&H00FFFFFF,&H00FFFFFF,&H00101010,&H96000000,-1,0,0,0,100,100,2,0,1,4,2,8,60,60,330,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.00,0:00:05.00,Title,,0,0,0,,{_wrap_title(title)}
""" + (f"Dialogue: 0,0:00:00.00,0:00:05.00,Sub,,0,0,0,,{subtitle}\n" if subtitle else ""), encoding="utf-8")
    media._run(["ffmpeg", "-y", "-v", "error", "-i", str(image), "-vf",
                f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,subtitles={_esc(ass)}:fontsdir={_esc(fonts_dir)}",
                "-frames:v", "1", "-q:v", "2", str(dest)])
    ass.unlink(missing_ok=True)
    return dest


def _wrap_title(t: str) -> str:
    """A long series title breaks at its comma (全村抢20万拆迁款，我拒签后靠玉米赚麻了 -> two lines)."""
    for sep in ("，", ",", "：", ":"):
        if sep in t:
            a, b = t.split(sep, 1)
            return a + r"\N" + b
    return t


def _esc(path) -> str:
    return "'" + str(path).replace("\\", "/").replace(":", r"\:").replace("'", r"\'") + "'"


def run(p: Project) -> Path:
    out = p.d("delivery")
    e = p.cfg["edit"]
    title = p.bible.get("title", p.root.name).split(" (")[0]
    eps_meta = []
    for ep in [e for e in p.episodes() if e.startswith("E")]:   # T-numbered test episodes are not delivered
        src = p.root / "edit" / ep
        master, clean, srt = src / "master.mp4", src / "master_clean.mp4", src / "subtitles.srt"
        if not master.exists():
            log(f"package: {ep} has no master yet; skipped")
            continue
        shutil.copy2(master, p.d("delivery", "episodes") / f"{ep}.mp4")
        if clean.exists():
            shutil.copy2(clean, p.d("delivery", "episodes") / f"{ep}_clean.mp4")
        if srt.exists():
            shutil.copy2(srt, p.d("delivery", "episodes") / f"{ep}.srt")
        e_json = p.episode(ep)
        frame = p.d("delivery", "_work") / f"{ep}_hook.jpg"
        media._run(["ffmpeg", "-y", "-v", "error", "-ss", "1.2", "-i", str(clean if clean.exists() else master),
                    "-frames:v", "1", "-q:v", "2", str(frame)])
        n = int("".join(ch for ch in ep if ch.isdigit()) or 0)
        title_card(frame, p.d("delivery", "thumbs") / f"{ep}.jpg", e_json.get("title", ep), f"第{n}集", e["font"],
                   e.get("fonts_dir", "/System/Library/Fonts"))
        eps_meta.append({"episode": ep, "number": n, "title": e_json.get("title", ""), "logline": e_json.get("logline", ""),
                         "duration_s": media.probe(master)["duration"], "file": f"episodes/{ep}.mp4"})
    art = poster_art(p)
    if art:
        title_card(art, out / "cover.jpg", title, "", e["font"], e.get("fonts_dir", "/System/Library/Fonts"))
    save_json(out / "metadata.json", {
        "title": title, "language": p.bible.get("language", "zh-CN"), "aspect": "9:16", "resolution": [e["width"], e["height"]],
        "episodes": eps_meta, "total_duration_s": round(sum(x["duration_s"] for x in eps_meta), 1),
        "cast": {c: {"name": v["name"], "role": v["role"]} for c, v in p.chars.items()},
        "ai_generated": True})
    (out / "AI_DISCLOSURE.txt").write_text(
        "本剧集画面、人物与配音均由人工智能生成。剧中人物均为虚构，与现实人物无关。\n"
        "The video, characters and voices of this series were generated with AI. All characters are fictional and do not "
        "depict real people.\n", encoding="utf-8")
    from . import sfx
    lines = sfx.credits(p)   # Attribution-licensed sound effects must be credited where the episodes are published
    (out / "CREDITS.txt").unlink(missing_ok=True)
    if lines:
        (out / "CREDITS.txt").write_text("音效 Sound effects (freesound.org):\n" + "\n".join(lines) + "\n", encoding="utf-8")
        log(f"package: {len(lines)} sound effect credit(s) in CREDITS.txt; publish them with the series")
    shutil.rmtree(out / "_work", ignore_errors=True)
    log(f"package: {len(eps_meta)} episode(s) -> {out}")
    return out
