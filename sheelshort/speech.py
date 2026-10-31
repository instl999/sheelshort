"""Measure spoken takes: where the speech is, pitch level, expressiveness, and the words (ASR).

Expressiveness ("pitch motion") is how far the pitch moves between the voiced runs (roughly syllables and words) of
one line, in semitones. drama-studio found this, not overall pitch spread, to be the measure that matches the ear for
flat vs. acted delivery (its `word_pitch`; live-action reference dramas sit around 3.3+). It is measured per line,
one speaker each, so two voices are never mixed into one number.
"""
from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from pathlib import Path

import numpy as np

SR = 16000
HOP = 160   # 10 ms


def load(path: Path, sr: int = SR) -> np.ndarray:
    import librosa
    y, _ = librosa.load(str(path), sr=sr, mono=True)
    return y


def speech_bounds(y: np.ndarray, sr: int = SR, top_db: float = 38) -> tuple[float, float]:
    """Start and end of the speech in seconds (leading and trailing silence ignored)."""
    import librosa
    if not len(y) or float(np.max(np.abs(y))) < 1e-4:
        return 0.0, 0.0
    _, (a, b) = librosa.effects.trim(y, top_db=top_db, frame_length=512, hop_length=HOP)
    return a / sr, b / sr


def pitch(y: np.ndarray, sr: int = SR, ref_hz: float | None = None) -> np.ndarray:
    """f0 in Hz per 10 ms frame, NaN where unvoiced. With the speaker's own pitch (`ref_hz`) the search is limited to
    about an octave around it, which removes the octave errors that breathy or noisy speech otherwise causes."""
    import librosa
    lo, hi = (max(50.0, ref_hz / 2.0), min(1000.0, ref_hz * 2.3)) if ref_hz else (65.0, 750.0)
    f0, voiced, prob = librosa.pyin(y, fmin=lo, fmax=hi, sr=sr, frame_length=1024, hop_length=HOP, center=True)
    f0[~voiced] = np.nan
    return f0


def runs(f0: np.ndarray, gap: int = 3, min_len: int = 6) -> list[np.ndarray]:
    """Voiced runs of frames, split at >= `gap` unvoiced frames; runs shorter than `min_len` frames are dropped."""
    out, cur, silent = [], [], 0
    for v in f0:
        if np.isnan(v):
            silent += 1
            if silent >= gap and cur:
                out.append(np.array(cur))
                cur = []
        else:
            silent = 0
            cur.append(v)
    if cur:
        out.append(np.array(cur))
    return [r for r in out if len(r) >= min_len]


def syllables(y: np.ndarray, f0: np.ndarray, min_voiced: int = 4) -> list[np.ndarray]:
    """Voiced pitch per syllable: the line is cut at energy onsets (Mandarin syllables often join without an
    unvoiced gap) and at unvoiced gaps; segments with fewer than `min_voiced` voiced frames are dropped."""
    import librosa
    onsets = librosa.onset.onset_detect(y=y, sr=SR, hop_length=HOP, backtrack=True, units="frames") if len(y) > 2048 else []
    bounds = sorted(set([0, len(f0)] + [int(o) for o in onsets if 0 < o < len(f0)]))
    out = []
    for a, b in zip(bounds, bounds[1:]):
        for r in runs(f0[a:b], gap=3, min_len=1):
            if len(r) >= min_voiced:
                out.append(r)
    return out


def semitones(hz: np.ndarray | float) -> np.ndarray | float:
    return 12 * np.log2(np.asarray(hz) / 55.0)


MAX_STEP_ST = 8.0   # a larger jump between neighbouring runs is a tracking error, not intonation


def profile(path: Path, ref_hz: float | None = None) -> dict:
    """Timing, pitch level and expressiveness of one spoken line (`ref_hz`: the speaker's cast-voice median)."""
    y = load(path)
    start, end = speech_bounds(y)
    seg = y[int(start * SR):int(end * SR)] if end > start else y
    f0 = pitch(seg, ref_hz=ref_hz) if len(seg) > SR // 10 else np.array([np.nan])
    rs = syllables(seg, f0)
    meds = [float(np.median(semitones(r))) for r in rs]
    voiced = f0[~np.isnan(f0)]
    rms = np.sqrt(np.convolve(seg ** 2, np.ones(400) / 400, mode="valid"))[::HOP] if len(seg) > 400 else np.array([0.0])
    db = 20 * np.log10(rms[rms > 1e-4]) if np.any(rms > 1e-4) else np.array([0.0])
    return {
        "duration": round(len(y) / SR, 3),
        "speech_start": round(start, 3),
        "speech_end": round(end, 3),
        "speech_s": round(end - start, 3),
        "median_hz": round(float(np.median(voiced)), 1) if len(voiced) else None,
        "motion": round(float(np.mean(np.minimum(np.abs(np.diff(meds)), MAX_STEP_ST))), 2) if len(meds) > 1 else 0.0,
        "range_st": round(float(np.percentile(semitones(voiced), 95) - np.percentile(semitones(voiced), 5)), 2) if len(voiced) > 10 else 0.0,
        "loud_std_db": round(float(np.std(db)), 2),
        "voiced_runs": len(rs),
    }


# ---------- words ----------

def _keep(ch: str) -> bool:
    return not unicodedata.category(ch).startswith(("P", "Z", "S", "C"))


def chars(text: str) -> str:
    text = re.sub(r"\[[^\]]*\]|（[^）]*）|\([^)]*\)", "", text)   # acting tags and stage directions
    return "".join(ch for ch in text if _keep(ch)).lower()


def error_rate(ref: str, hyp: str) -> float:
    """Character error rate (Levenshtein) of hyp against ref, punctuation ignored."""
    r, h = chars(ref), chars(hyp)
    if not r:
        return 0.0 if not h else 1.0
    prev = list(range(len(h) + 1))
    for i, rc in enumerate(r, 1):
        cur = [i]
        for j, hc in enumerate(h, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (rc != hc)))
        prev = cur
    return prev[-1] / len(r)


@lru_cache(maxsize=1)
def _whisper(size: str):
    from faster_whisper import WhisperModel
    return WhisperModel(size, device="cpu", compute_type="int8")


def transcribe(path: Path, lang: str = "zh", size: str = "small") -> str:
    prompt = "以下是普通话的句子。" if lang.startswith("zh") else None
    segs, _ = _whisper(size).transcribe(load(path).astype(np.float32), language=lang.split("-")[0], beam_size=5, initial_prompt=prompt,
                                        vad_filter=False, condition_on_previous_text=False)
    text = "".join(s.text for s in segs)
    if lang.startswith("zh"):
        text = to_simplified(text)
    return text


def to_simplified(text: str) -> str:
    try:
        import zhconv
        return zhconv.convert(text, "zh-cn")
    except ImportError:
        return text


def words(path: Path, lang: str = "zh", size: str = "small") -> list[tuple[float, float, str]]:
    """(start, end, text) per recognised word/character run, for locating lines inside a clip."""
    prompt = "以下是普通话的句子。" if lang.startswith("zh") else None
    segs, _ = _whisper(size).transcribe(load(path).astype(np.float32), language=lang.split("-")[0], beam_size=5,
                                        initial_prompt=prompt, word_timestamps=True, condition_on_previous_text=False)
    return [(w.start, w.end, to_simplified(w.word) if lang.startswith("zh") else w.word) for s in segs for w in (s.words or [])]


def locate(lines: list[str], ws: list[tuple[float, float, str]]) -> list[tuple[float, float] | None]:
    """Split the recognised words among the script lines in order, by character count; returns each line's span."""
    out, i = [], 0
    for ln in lines:
        need = len(chars(ln))
        got, start, end = 0, None, None
        while i < len(ws) and got < need * 0.85:
            c = len(chars(ws[i][2]))
            if c:
                start = ws[i][0] if start is None else start
                end = ws[i][1]
                got += c
            i += 1
        out.append((start, end) if start is not None else None)
    return out
