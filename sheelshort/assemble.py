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
from .project import REPO, Project, load_json, log, save_json

CJK_PER_LINE = 13
# Finishing grade over the whole cut (`edit.grade`): generated video is over-saturated and clean as glass; a touch less
# saturation, a soft vignette and fine luma grain read as film (owner review 2026-10-31: "lacks realism")
GRADES = {
    "film": "eq=saturation=0.9:contrast=1.03,vignette=angle=PI/7,noise=c0s=5:c0f=t",
    "bright": "eq=saturation=1.03:contrast=1.02,noise=c0s=3:c0f=t",                      # high-key comedy: clean, no vignette
    "glossy": "eq=saturation=0.97:contrast=0.97:gamma=1.03,vignette=angle=PI/9,noise=c0s=3:c0f=t",   # soft, lifted, warm
    "none": "",
}
GRADE_FOR_LOOK = {"cinematic": "film", "bright": "bright", "glossy": "glossy"}   # `edit.grade = "auto"`


def grade(p: Project) -> str:
    g = p.cfg["edit"].get("grade", "auto")
    return GRADES.get(GRADE_FOR_LOOK.get(p.bible.get("look_preset", "cinematic"), "film") if g == "auto" else g, "")


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
    t, items, subs, cards = 0.0, [], [], []
    introduced = introduced_before(p, ep)
    places, prev_loc = [], None
    for clip in p.clips(ep):
        video = chosen_take(p, ep, clip["id"])
        if acted:
            tm = timing[clip["id"]]
            seconds, lines = tm["seconds"], tm["lines"]
            audio = tm["track"] if p.cfg["edit"].get("audio", "clean") == "clean" and tm["track"] else None
        else:
            seconds = media.probe(video)["duration"]
            lines, audio = heard_lines(p, ep, clip, video), None
        head, tail = edge_trims(video, seconds, lines) if p.cfg["edit"].get("trim_flashes", True) else (0.0, seconds)
        fol = sorted(video.parent.glob(video.stem + "_foley.*"))
        items.append({"clip": clip["id"], "video": str(video), "audio": audio, "foley": str(fol[0]) if fol and audio else None,
                      "start": round(t, 3), "in": head, "seconds": round(tail - head, 3)})
        subs += [{"text": ln["text"], "start": round(t + ln["start"] - head, 3), "end": round(t + ln["end"] - head + 0.25, 3)}
                 for ln in lines]
        cards += _cards_in(p, clip, t - head, video, introduced)
        loc = p.bible["locations"].get(clip["location"], {})
        if clip["location"] != prev_loc and loc.get("name_zh") and p.cfg["edit"].get("location_cards", True) \
                and clip["location"] not in {pl["loc"] for pl in places}:
            places.append({"kind": "place", "loc": clip["location"], "name": loc["name_zh"], "start": round(t + 0.1, 3),
                           "end": round(t + min(2.6, tail - head - 0.2), 3)})
        prev_loc = clip["location"]
        t += tail - head
    for pl in places:   # a name card waits until the location card is gone (both stand at the top of the frame)
        for c in cards:
            if c["start"] < pl["end"] and c["end"] > pl["start"]:
                c["start"] = round(pl["end"] + 0.1, 3)
    cards = [c for c in cards if c["end"] - c["start"] >= 0.8] + places
    out = {"episode": ep, "duration": round(t, 3), "clips": items, "subtitles": subs, "cards": cards}
    save_json(p.d("edit", ep) / "edl.json", out)
    return out


FLASH_S = 0.6   # a shot this short at a clip's head or tail is H3 starting the next shot early or ending late


def edge_trims(video: Path, seconds: float, lines: list[dict]) -> tuple[float, float]:
    """In and out points that drop a flash shot at either end of a take (E02 had a 0.3 s shot at a clip boundary), never
    cutting into a line."""
    cuts = media.shot_cuts(video)
    head = max((c for c in cuts if c < FLASH_S), default=0.0)
    tail = min((c for c in cuts if c > seconds - FLASH_S), default=seconds)
    if any(ln["start"] < head for ln in lines):
        head = 0.0
    if any(ln["end"] > tail + 0.05 for ln in lines):
        tail = seconds
    return round(head, 3), round(tail, 3)


def _intro_cut(cut: dict, c: str) -> bool:
    return cut.get("intro") is True and c in cut.get("subjects", []) or (
        cut["type"] in ("CU", "MCU", "VO") and cut.get("subjects") == [c])


def introduced_before(p: Project, ep: str) -> set[str]:
    """Characters whose name card was already shown in an earlier episode (their first solo close-up)."""
    seen: set[str] = set()
    for e in p.episodes():
        if e >= ep:
            break
        for clip in p.episode(e)["clips"]:
            for cut in clip["cuts"]:
                seen |= {c for c in cut.get("subjects", []) if p.chars.get(c, {}).get("card") and _intro_cut(cut, c)}
    return seen


def _cards_in(p: Project, clip: dict, t0: float, video: Path, introduced: set[str]) -> list[dict]:
    """Name cards (REFERENCE_STUDY §4): a main character's first solo close-up in the series carries their name and role,
    vertical, beside the head. `intro: true` on a cut picks the shot explicitly. The side comes from where the face is."""
    if not p.cfg["edit"].get("name_cards", True):
        return []
    out, at = [], 0.0
    for cut in clip["cuts"]:
        dur = h3prompt.cut_len(cut)
        cands = [c for c in cut.get("subjects", []) if p.chars.get(c, {}).get("card") and c not in introduced]
        if cut.get("intro") is True:
            cands = [c for c in cut.get("subjects", []) if c not in introduced and p.chars.get(c, {}).get("card")][:1]
        for c in cands:
            if _intro_cut(cut, c):
                ch = p.chars[c]
                face = media.face_box(video, at + dur / 2)
                out.append({"name": ch.get("name_zh") or ch["name"], "role": ch["card"], "start": round(t0 + at + 0.15, 3),
                            "end": round(t0 + at + min(dur - 0.1, 2.6), 3), "face": face})
                introduced.add(c)
                break
        at += dur
    return out


def heard_lines(p: Project, ep: str, clip: dict, video: Path) -> list[dict]:
    """Subtitle times for a clip H3 voiced itself: each script line is found in the clip's own audio by its
    recognised words (H3 places speech freely inside the clip); a line that cannot be found keeps its cut window.
    Cached per take in edit/<EP>/align/<take>.json."""
    from . import speech
    cache = p.d("edit", ep, "align") / f"{video.stem}.json"
    if cache.exists() and load_json(cache).get("mtime") == video.stat().st_mtime_ns and load_json(cache).get("v") == 3:
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
                w["heard"] = "".join(t for a0, a1, t in ws if span[0] - 0.01 <= (a0 + a1) / 2 <= span[1] + 0.01)
                w["cer"] = round(speech.error_rate(w["say"], w["heard"]), 3)
    out = [{k: w[k] for k in ("text", "start", "end")} | {"aligned": w.get("aligned", False), "heard": w.get("heard", ""),
                                                          "cer": w.get("cer", 1.0)} for w in windows]
    save_json(cache, {"mtime": video.stat().st_mtime_ns, "v": 3, "lines": out})
    return out


def wrap(text: str) -> str:
    """Micro-drama subtitle style: ，。、；： become spaces (？！ stay), at most two lines of ~13 CJK characters,
    broken at a space when possible."""
    text = re.sub(r"[，。、；：,.;:]+", " ", text).replace("……", " ").replace("——", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= CJK_PER_LINE:
        return text
    mid = len(text) / 2
    spaces = [m.start() for m in re.finditer(r" ", text) if 3 <= m.start() <= len(text) - 3 and abs(m.start() - mid) <= 4]
    if spaces:
        cut = min(spaces, key=lambda i: abs(i - mid))
    else:   # no pause near the middle: break between words, never inside one (「我们」 must not split)
        try:
            import jieba
            jieba.setLogLevel(60)
            bounds, i = [], 0
            for w in jieba.cut(text):
                i += len(w)
                if 3 <= i <= len(text) - 3:
                    bounds.append(i)
            cut = min(bounds, key=lambda b: abs(b - mid)) if bounds else len(text) // 2
        except ImportError:
            cut = len(text) // 2
    while cut < len(text) and text[cut] in "！？!?」”）)":   # closing punctuation ends the first row, never opens the second
        cut += 1
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
    subs = []
    for sb in plan["subtitles"]:   # a line too long for two subtitle rows is shown in two parts, timed by length
        text = re.sub(r"[，。、；：,.;:]+", " ", sb["text"]).replace("……", " ").replace("——", " ").strip()
        if len(text) > 2 * CJK_PER_LINE:
            mid = len(text) / 2
            cuts = [m.start() for m in re.finditer(r" ", text)]
            k = min(cuts, key=lambda i: abs(i - mid)) if cuts else int(mid)
            split_t = sb["start"] + (sb["end"] - sb["start"]) * k / len(text)
            subs += [{**sb, "text": text[:k].strip(), "end": round(split_t, 3)}, {**sb, "text": text[k:].strip(), "start": round(split_t, 3)}]
        else:
            subs.append(sb)
    for i, s in enumerate(subs):
        end = min(s["end"], subs[i + 1]["start"] - 0.05) if i + 1 < len(subs) else s["end"]
        rows.append(f"Dialogue: 0,{ass_time(s['start'])},{ass_time(end)},Line,,0,0,0,,{wrap(s['text'])}")
        srows.append(f"{i + 1}\n{srt_time(s['start'])} --> {srt_time(end)}\n{wrap(s['text']).replace(chr(92) + 'N', chr(10))}\n")
    for card in plan.get("cards", []):
        rows += card_events(card, w, h, e.get("card_font", "Songti SC"))
    ass.write_text(head + "\n".join(rows) + "\n", encoding="utf-8")
    srt.write_text("\n".join(srows), encoding="utf-8")
    return ass, srt


def card_events(card: dict, w: int, h: int, font: str) -> list[str]:
    """A vertical name (large, heavy serif) and a thin role column beside it, next to the head at eye level, on the side
    of the frame with more room; no box, a soft shadow. A location card (`kind: place`) is one vertical column of the
    place's name at the top left as a scene opens, the way 持械入宋 and 和离后我带女儿横扫魔域 title their scenes."""
    vert = lambda t: r"\N".join(t)   # noqa: E731 - one character per line = vertical type
    if card.get("kind") == "place":
        size = round(w * 0.075)
        tag = rf"{{\an7\pos({w * 0.07:.0f},{h * 0.075:.0f})\fn{font}\fs{size}\b1\bord0\shad3\4a&H70&\c&HFFFFFF&\fad(200,300)}}"
        return [f"Dialogue: 1,{ass_time(card['start'])},{ass_time(card['end'])},Line,,0,0,0,,{tag}{vert(card['name'])}"]
    face = card.get("face")   # (x0, y0, x1, y1) as fractions of the frame, or None
    fx = (face[0] + face[2]) / 2 if face else 0.5
    left = fx >= 0.5   # face right of centre: card on the left
    size, role_size = round(w * 0.1), round(w * 0.036)
    if face:
        x = (face[0] * w - size * 0.9) if left else (face[2] * w + size * 0.9)
        y = face[1] * h + 0.05 * h
    else:
        x, y = (w * 0.16 if left else w * 0.84), h * 0.24
    x = min(max(x, size * 0.75), w - size * 0.75)
    rx = x + (size * 0.95 if left else -size * 0.95)
    ts = f"{ass_time(card['start'])},{ass_time(card['end'])}"
    name_tag = rf"{{\an8\pos({x:.0f},{y:.0f})\fn{font}\fs{size}\b1\bord0\shad3\4a&H80&\c&HFFFFFF&\fad(150,200)}}"
    role_tag = rf"{{\an8\pos({rx:.0f},{y + size * 0.7:.0f})\fn{font}\fs{role_size}\b1\bord0\shad2\4a&H80&\c&HFFFFFF&\fad(150,200)}}"
    return [f"Dialogue: 1,{ts},Line,,0,0,0,,{name_tag}{vert(card['name'])}",
            f"Dialogue: 1,{ts},Line,,0,0,0,,{role_tag}{vert(card['role'])}"]


def srt_time(s: float) -> str:
    return f"{int(s // 3600):02d}:{int(s % 3600 // 60):02d}:{int(s % 60):02d},{int(round(s % 1 * 1000)) % 1000:03d}"


def upscaled(p: Project, ep: str, video: Path) -> Path:
    """The take upscaled on the servers to cover 1080x1920 (`edit.upscale = "rtx"`), else the take itself."""
    e = p.cfg["edit"]
    if e.get("upscale", "rtx") != "rtx":
        return video
    info = media.probe(video)
    scale = max(e["width"] / info["width"], e["height"] / info["height"])
    if scale <= 1.01:
        return video
    dest = video.with_name(video.stem + "_up.mp4")
    if dest.exists() and dest.stat().st_mtime >= video.stat().st_mtime:
        return dest
    from . import flows
    key = f"upscale/{ep}/{video.stem}_{int(video.stat().st_mtime)}"
    caps_path = REPO / ".cache" / "server_caps.json"
    out = None
    for _ in range(max(1, len(p.cfg["comfy"]["servers"]))):
        caps = load_json(caps_path) if caps_path.exists() else {}
        bad = set(caps.get("no_rtx_vsr", []))
        out = p.run_job(key, lambda cl: flows.upscale_rtx(p.put(cl, video), scale + 0.005, f"sheelshort/{ep}_{video.stem}_up"),
                        p.d("clips", ep, "up"), video.stem, ("gifs", "videos"), 1800, exclude=bad)
        job = p.state["jobs"].get(key, {})
        if out or "NvVFX" not in job.get("error", ""):
            break
        # this server's NVIDIA video-effects runtime cannot load RTX VSR: remember it and try another server
        with p.lock:
            caps = load_json(caps_path) if caps_path.exists() else {}
            caps["no_rtx_vsr"] = sorted(set(caps.get("no_rtx_vsr", [])) | {job["server"]})
            save_json(caps_path, caps)
        log(f"{video.stem}: RTX VSR unavailable on {job['server']}; retrying elsewhere")
        p.forget(key)
    mp4 = [f for f in out or [] if f.suffix == ".mp4"]
    if not mp4:
        log(f"{video.stem}: upscale failed; using the plain resize")
        return video
    mp4 = sorted(mp4, key=lambda f: media.probe(f)["audio_codec"] is None)
    mp4[0].replace(dest)
    return dest


def run(p: Project, ep: str) -> Path:
    e = p.cfg["edit"]
    plan = edl(p, ep)
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max(2, 2 * len(p.cfg["comfy"]["servers"]))) as ex:   # upscale the chosen takes in parallel
        ups = list(ex.map(lambda it: str(upscaled(p, ep, Path(it["video"]))), plan["clips"]))
    for it, up in zip(plan["clips"], ups):
        it["video"] = up
    ass, _ = write_subtitles(p, ep, plan)
    out_dir = p.d("edit", ep)
    w, h, fps = e["width"], e["height"], e["fps"]
    ins, filt, n = [], [], len(plan["clips"])
    for i, it in enumerate(plan["clips"]):
        ins += ["-i", it["video"]]
        filt.append(f"[{i}:v]trim={it.get('in', 0)}:{it.get('in', 0) + it['seconds']},setpts=PTS-STARTPTS,scale={w}:{h}:force_original_aspect_ratio=increase:"
                    f"flags=lanczos,crop={w}:{h},fps={fps},setsar=1[v{i}]")
    k = n
    for i, it in enumerate(plan["clips"]):
        fit = f"aresample=48000,aformat=channel_layouts=stereo,atrim={it.get('in', 0)}:{it.get('in', 0) + it['seconds']},asetpts=PTS-STARTPTS,apad=whole_dur={it['seconds']}"
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
    if grade(p):
        filt.append(f"[vc]{grade(p)}[vg]")
        vc = "[vg]"
    else:
        vc = "[vc]"
    filt.append(f"{vc}split=2[vsub][vclean];[vsub]subtitles={_esc(ass)}:fontsdir={_esc(fonts)}[vout]")
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
    filt.append(f"{last}loudnorm=I={e['loudness']}:TP=-1.0:LRA=11,aresample=48000,asplit=2[aout][aclean]")
    master = out_dir / f"{p.master_name()}.mp4"
    clean = out_dir / f"{p.master_name()}_clean.mp4"   # the same cut without burned-in subtitles (dubbing, re-subtitling)
    enc = ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-profile:v", "high", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
    cmd = ["ffmpeg", "-y", "-v", "error", *ins, "-filter_complex", ";".join(filt),
           "-map", "[vout]", "-map", "[aout]", *enc, str(master), "-map", "[vclean]", "-map", "[aclean]", *enc, str(clean)]
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
