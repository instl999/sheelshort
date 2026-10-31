"""Cut an episode: rendered clips -> 1080x1920 master with the acted dialogue, burned-in subtitles, music bed and
platform loudness. Deterministic: every decision is in edit/<EP>/edl.json.

Subtitle text always comes from the script, never from ASR, so it is word-correct. Its timing comes from where the lines
were placed (acted dialogue) or from where each line is heard in the clip (H3 dialogue, located by word timestamps). Style follows vertical micro-drama convention: plain white with a dark
outline, at most two short lines, above the bottom UI zone, no word-by-word pop.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import h3prompt, media
from .project import Project, load_json, log, save_json

CJK_PER_LINE = 13


def chosen_take(p: Project, ep: str, clip_id: str) -> Path:
    picks = p.root / "edit" / ep / "picks.json"
    t = load_json(picks).get(clip_id, 1) if picks.exists() else 1
    from .render import clip_path
    path = clip_path(p, ep, clip_id, t)
    if not path.exists():
        raise FileNotFoundError(f"{path} missing; run `sheelshort render {ep}`")
    return path


def edl(p: Project, ep: str) -> dict:
    acted = p.cfg["render"]["dialogue"] == "acted"
    timing = load_json(p.root / "perform" / ep / "timing.json") if acted else None
    t, items, subs = 0.0, [], []
    for clip in p.clips(ep):
        video = chosen_take(p, ep, clip["id"])
        if acted:
            tm = timing[clip["id"]]
            seconds, lines = tm["seconds"], tm["lines"]
            audio = tm["track"] if p.cfg["edit"].get("audio", "clean") == "clean" and tm["track"] else None
        else:
            seconds = media.probe(video)["duration"]
            lines, audio = heard_lines(p, ep, clip, video), None
        fol = sorted(video.parent.glob(video.stem + "_foley.*"))
        items.append({"clip": clip["id"], "video": str(video), "audio": audio, "foley": str(fol[0]) if fol and audio else None,
                      "start": round(t, 3), "seconds": seconds})
        subs += [{"text": ln["text"], "start": round(t + ln["start"], 3), "end": round(t + ln["end"] + 0.25, 3)} for ln in lines]
        t += seconds
    out = {"episode": ep, "duration": round(t, 3), "clips": items, "subtitles": subs}
    save_json(p.d("edit", ep) / "edl.json", out)
    return out


def heard_lines(p: Project, ep: str, clip: dict, video: Path) -> list[dict]:
    """Subtitle times for a clip H3 voiced itself: each script line is found in the clip's own audio by its
    recognised words (H3 places speech freely inside the clip); a line that cannot be found keeps its cut window.
    Cached per take in edit/<EP>/align/<take>.json."""
    from . import speech
    cache = p.d("edit", ep, "align") / f"{video.stem}.json"
    if cache.exists() and load_json(cache).get("mtime") == video.stat().st_mtime_ns:
        return load_json(cache)["lines"]
    windows, at = [], 0.0
    for cut in clip["cuts"]:
        if cut.get("line"):
            windows.append({"text": cut["line"], "say": cut.get("say") or cut["line"], "start": at + 0.3,
                            "end": at + h3prompt.cut_len(cut) - 0.2})
        at += h3prompt.cut_len(cut)
    if windows:
        wav = cache.with_suffix(".wav")
        media.extract_audio(video, wav)
        ws = speech.words(wav, p.cfg["project"]["language"], p.cfg["perform"]["whisper_model"])
        wav.unlink(missing_ok=True)
        for w, span in zip(windows, speech.locate([w["say"] for w in windows], ws)):
            if span:
                w["start"], w["end"], w["aligned"] = round(span[0], 3), round(span[1], 3), True
    out = [{k: w[k] for k in ("text", "start", "end")} | {"aligned": w.get("aligned", False)} for w in windows]
    save_json(cache, {"mtime": video.stat().st_mtime_ns, "lines": out})
    return out


def wrap(text: str) -> str:
    """Micro-drama subtitle style: ，。、；： become spaces (？！ stay), at most two lines of ~13 CJK characters,
    broken at a space when possible."""
    text = re.sub(r"[，。、；：,.;:]+", " ", text).replace("……", " ").replace("——", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= CJK_PER_LINE:
        return text
    cut = max((m.start() for m in re.finditer(r" ", text) if 4 <= m.start() <= len(text) - 3),
              key=lambda i: -abs(i - len(text) / 2), default=len(text) // 2)
    return text[:cut].rstrip() + r"\N" + text[cut:].lstrip()


def ass_time(s: float) -> str:
    return f"{int(s // 3600)}:{int(s % 3600 // 60):02d}:{s % 60:05.2f}"


def write_subtitles(p: Project, ep: str, plan: dict) -> tuple[Path, Path]:
    e = p.cfg["edit"]
    w, h = e["width"], e["height"]
    ass = p.d("edit", ep) / "subtitles.ass"
    srt = p.d("edit", ep) / "subtitles.srt"
    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Line,{e['font']},{round(w * 0.062)},&H00FFFFFF,&H00FFFFFF,&H00101010,&H64000000,-1,0,0,0,100,100,1,0,1,{round(w * 0.0045, 1)},1.2,2,{round(w * 0.09)},{round(w * 0.09)},{round(h * 0.21)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    rows, srows = [], []
    subs = plan["subtitles"]
    for i, s in enumerate(subs):
        end = min(s["end"], subs[i + 1]["start"] - 0.05) if i + 1 < len(subs) else s["end"]
        rows.append(f"Dialogue: 0,{ass_time(s['start'])},{ass_time(end)},Line,,0,0,0,,{wrap(s['text'])}")
        srows.append(f"{i + 1}\n{srt_time(s['start'])} --> {srt_time(end)}\n{wrap(s['text']).replace(chr(92) + 'N', chr(10))}\n")
    ass.write_text(head + "\n".join(rows) + "\n", encoding="utf-8")
    srt.write_text("\n".join(srows), encoding="utf-8")
    return ass, srt


def srt_time(s: float) -> str:
    return f"{int(s // 3600):02d}:{int(s % 3600 // 60):02d}:{int(s % 60):02d},{int(round(s % 1 * 1000)) % 1000:03d}"


def run(p: Project, ep: str) -> Path:
    e = p.cfg["edit"]
    plan = edl(p, ep)
    ass, _ = write_subtitles(p, ep, plan)
    out_dir = p.d("edit", ep)
    w, h, fps = e["width"], e["height"], e["fps"]
    ins, filt, n = [], [], len(plan["clips"])
    for i, it in enumerate(plan["clips"]):
        ins += ["-i", it["video"]]
        filt.append(f"[{i}:v]trim=0:{it['seconds']},setpts=PTS-STARTPTS,scale={w}:{h}:force_original_aspect_ratio=increase:"
                    f"flags=lanczos,crop={w}:{h},fps={fps},setsar=1[v{i}]")
    k = n
    for i, it in enumerate(plan["clips"]):
        fit = f"aresample=48000,aformat=channel_layouts=stereo,atrim=0:{it['seconds']},asetpts=PTS-STARTPTS,apad=whole_dur={it['seconds']}"
        if it["audio"]:
            ins += ["-i", it["audio"]]
            filt.append(f"[{k}:a]{fit}[d{i}]")
            k += 1
            if it["foley"]:
                # foley under the clean dialogue: about -20 dB below it, ducked a little more while a line plays
                ins += ["-i", it["foley"]]
                filt.append(f"[{k}:a]{fit},loudnorm=I=-38:TP=-9[f{i}]")
                filt.append(f"[d{i}]asplit=2[dd{i}][key{i}]")
                filt.append(f"[f{i}][key{i}]sidechaincompress=threshold=0.02:ratio=3:attack=30:release=500[fd{i}]")
                filt.append(f"[dd{i}][fd{i}]amix=inputs=2:normalize=0[a{i}]")
                k += 1
            else:
                filt.append(f"[d{i}]anull[a{i}]")
        else:
            filt.append(f"[{i}:a]{fit}[a{i}]")
    filt.append("".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[vc][dia]")
    fonts = e.get("fonts_dir", "/System/Library/Fonts")
    filt.append(f"[vc]subtitles={_esc(ass)}:fontsdir={_esc(fonts)}[vout]")
    music = e.get("music")
    if music == "auto":
        music = score(p, ep)
    if music:
        mi = ins.count("-i")
        ins += ["-stream_loop", "-1", "-i", str(Path(music).expanduser())]
        filt.append("[dia]asplit=2[dia1][key]")
        filt.append(f"[{mi}:a]aresample=48000,aformat=channel_layouts=stereo,volume=-16dB,atrim=0:{plan['duration']},"
                    f"afade=t=out:st={max(0, plan['duration'] - 1.5)}:d=1.5[bed]")
        filt.append("[bed][key]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[duck]")
        filt.append("[dia1][duck]amix=inputs=2:normalize=0[mix]")
        last = "[mix]"
    else:
        last = "[dia]"
    filt.append(f"{last}loudnorm=I={e['loudness']}:TP=-1.0:LRA=11,aresample=48000[aout]")
    master = out_dir / f"{p.master_name()}.mp4"
    cmd = ["ffmpeg", "-y", "-v", "error", *ins, "-filter_complex", ";".join(filt), "-map", "[vout]", "-map", "[aout]",
           "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-profile:v", "high", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(master)]
    subprocess.run(cmd, check=True)
    log(f"{ep}: master {plan['duration']:.1f}s -> {master}")
    return master


DEFAULT_MUSIC = ("cinematic tense underscore, Chinese short drama, instrumental, slow, low strings, soft piano, deep drone, "
                 "subtle percussion pulse, emotional, no vocals")


def music_seconds(p: Project, ep: str) -> int:
    """The bed's length from the clips in scope (written lengths, plus slack for H3's frame grid and the fade)."""
    if p.cfg["render"]["dialogue"] == "acted" and (p.root / "perform" / ep / "timing.json").exists():
        tm = load_json(p.root / "perform" / ep / "timing.json")
        return int(sum(tm[c["id"]]["seconds"] for c in p.clips(ep) if c["id"] in tm)) + 4
    return int(sum(h3prompt.clip_len(c) for c in p.clips(ep))) + 4


def score(p: Project, ep: str) -> Path | None:
    """An instrumental bed for the episode from ACE-Step; mood tags from the episode's `music` field."""
    from . import flows
    tags = p.episode(ep).get("music") or DEFAULT_MUSIC
    seconds = music_seconds(p, ep)
    key = f"music/{ep}_{seconds}s"
    out = p.run_job(key, lambda cl: flows.music(tags, seconds, p.seed(key), f"sheelshort/{ep}_music"),
                    p.d("edit", ep), f"music_{seconds}s", ("audio",), 900)
    return out[0] if out else None


def _esc(path) -> str:
    return "'" + str(path).replace("\\", "/").replace(":", r"\:").replace("'", r"\'") + "'"
