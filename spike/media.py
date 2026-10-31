"""ffmpeg/ffprobe helpers."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


def _run(args: list[str]) -> str:
    p = subprocess.run(args, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"{args[0]} failed: {p.stderr.strip()[-800:]}")
    return p.stdout


def probe(path: Path) -> dict:
    data = json.loads(_run(["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]))
    v = next((s for s in data["streams"] if s["codec_type"] == "video"), {})
    a = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    num, _, den = v.get("avg_frame_rate", "0/1").partition("/")
    return {
        "duration": round(float(data["format"].get("duration", 0)), 2),
        "width": v.get("width"),
        "height": v.get("height"),
        "fps": round(float(num) / float(den or 1), 3) if num else None,
        "video_codec": v.get("codec_name"),
        "audio_codec": a and a.get("codec_name"),
        "audio_channels": a and a.get("channels"),
    }


def extract_audio(video: Path, dest: Path, start: float | None = None, end: float | None = None, rate: int = 16000) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = ["ffmpeg", "-y", "-v", "error"]
    if start is not None:
        args += ["-ss", f"{start:.3f}"]
    args += ["-i", str(video)]
    if end is not None:
        args += ["-t", f"{end - (start or 0):.3f}"]
    _run(args + ["-vn", "-ac", "1", "-ar", str(rate), str(dest)])
    return dest


def voice_reference(src: Path, dest: Path, max_s: float = 7.0, min_s: float = 2.0) -> Path:
    """Turn a recording into an H3 reference-audio clip: leading silence cut, loudness-normalized, mono WAV."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(["ffmpeg", "-y", "-v", "error", "-i", str(src),
          "-af", "silenceremove=start_periods=1:start_threshold=-45dB,loudnorm=I=-18:TP=-2:LRA=11",
          "-t", f"{max_s:.2f}", "-ac", "1", "-ar", "44100", str(dest)])
    if probe(dest)["duration"] < min_s:
        raise ValueError(f"{src.name}: only {probe(dest)['duration']}s of speech; H3 reference audio needs at least {min_s}s")
    return dest


def dhash(path: Path) -> int:
    """64-bit difference hash of an image (9x8 greyscale), for spotting near-duplicate photos."""
    p = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", "scale=9:8:flags=area,format=gray",
                        "-frames:v", "1", "-f", "rawvideo", "-"], capture_output=True)
    if p.returncode != 0 or len(p.stdout) < 72:
        raise RuntimeError(f"ffmpeg could not read {path}: {p.stderr.decode(errors='replace')[-300:]}")
    px = p.stdout[:72]
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return bits


def last_frame(video: Path, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(["ffmpeg", "-y", "-v", "error", "-sseof", "-0.25", "-i", str(video), "-update", "1", "-q:v", "1", str(dest)])
    return dest


def sample_frames(video: Path, dest_dir: Path, n: int = 6) -> list[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    duration = probe(video)["duration"]
    out = []
    for i in range(n):
        t = duration * (i + 0.5) / n
        dest = dest_dir / f"f{i:02d}.jpg"
        _run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(video), "-frames:v", "1", "-q:v", "2", str(dest)])
        out.append(dest)
    return out


def concat(videos: list[Path], dest: Path, width: int = 1080, height: int = 1920, fps: int = 30) -> Path:
    """Re-encode and join clips so a variant can be watched as one sequence."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = ["ffmpeg", "-y", "-v", "error"]
    for v in videos:
        args += ["-i", str(v)]
    chains, labels = [], []
    for i in range(len(videos)):
        chains.append(
            f"[{i}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps={fps},setsar=1[v{i}];"
            f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]"
        )
        labels.append(f"[v{i}][a{i}]")
    graph = ";".join(chains) + f";{''.join(labels)}concat=n={len(videos)}:v=1:a=1[v][a]"
    _run(args + ["-filter_complex", graph, "-map", "[v]", "-map", "[a]",
                 "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", str(dest)])
    return dest


def image_sequence(pattern: Path, dest: Path, fps: float, out_fps: int | None = None) -> Path:
    """Numbered PNG frames (e.g. seq/%04d.png) at `fps` -> silent H.264 MP4 at `out_fps` (frames repeated)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(["ffmpeg", "-y", "-v", "error", "-framerate", f"{fps:g}", "-start_number", "1", "-i", str(pattern),
          "-r", str(out_fps or fps), "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", "-an", str(dest)])
    return dest


def side_by_side(videos: list[Path], dest: Path, height: int = 768) -> Path:
    """Stack clips horizontally at the same height (e.g. animatic | generated clip) for review; audio from the last."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    args = ["ffmpeg", "-y", "-v", "error"]
    for v in videos:
        args += ["-i", str(v)]
    scaled = ";".join(f"[{i}:v]scale=-2:{height},fps=24,setsar=1[v{i}]" for i in range(len(videos)))
    graph = f"{scaled};{''.join(f'[v{i}]' for i in range(len(videos)))}hstack=inputs={len(videos)}:shortest=1[v]"
    last = len(videos) - 1
    _run(args + ["-filter_complex", graph, "-map", "[v]", "-map", f"{last}:a?", "-c:v", "libx264", "-crf", "20",
                 "-pix_fmt", "yuv420p", "-c:a", "aac", str(dest)])
    return dest


def tile(images: list[Path], dest: Path, cols: int, cell: tuple[int, int] = (270, 480)) -> Path:
    """Contact sheet: images scaled to `cell` and laid out left to right, top to bottom."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    w, h = cell
    args = ["ffmpeg", "-y", "-v", "error"]
    for im in images:
        args += ["-i", str(im)]
    scaled = ";".join(f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2[s{i}]"
                      for i in range(len(images)))
    layout = "|".join(f"{(i % cols) * w}_{(i // cols) * h}" for i in range(len(images)))
    graph = f"{scaled};{''.join(f'[s{i}]' for i in range(len(images)))}xstack=inputs={len(images)}:layout={layout}:fill=black[v]"
    _run(args + ["-filter_complex", graph, "-map", "[v]", "-frames:v", "1", "-q:v", "3", str(dest)])
    return dest
