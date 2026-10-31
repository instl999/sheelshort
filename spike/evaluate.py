"""Score spike clips: technical probe, ASR dialogue accuracy, voice and face consistency.

ASR (faster-whisper), speaker embeddings (resemblyzer) and face embeddings (insightface)
are optional; missing packages are reported instead of failing the run.
"""
from __future__ import annotations

import difflib
import html
import itertools
import json
import os
import unicodedata
from pathlib import Path

import media


# ---------- text ----------

def tokens(text: str, lang: str) -> list[str]:
    def keep(ch):
        return not (unicodedata.category(ch).startswith(("P", "Z", "S")) or ch.isspace())
    if lang.startswith(("zh", "ja")):
        return [ch for ch in text if keep(ch)]
    return ["".join(ch for ch in w.lower() if keep(ch)) for w in text.split() if any(keep(c) for c in w)]


def error_rate(ref: list[str], hyp: list[str]) -> float:
    """Levenshtein distance / reference length (CER for zh, WER otherwise)."""
    if not ref:
        return 0.0 if not hyp else 1.0
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i]
        for j, h in enumerate(hyp, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h)))
        prev = cur
    return round(prev[-1] / len(ref), 3)


# ---------- optional models ----------

class Models:
    def __init__(self, lang: str, whisper_size: str):
        self.lang = lang
        self.whisper_size = whisper_size
        self.notes: list[str] = []
        self._asr = self._voice = self._face = None
        self._tried: set[str] = set()

    def _load(self, name, fn):
        if name not in self._tried:
            self._tried.add(name)
            try:
                setattr(self, f"_{name}", fn())
            except Exception as e:  # noqa: BLE001 - any import/model failure just disables the metric
                self.notes.append(f"{name} disabled: {type(e).__name__}: {e}"[:300])
        return getattr(self, f"_{name}")

    def asr(self):
        def load():
            from faster_whisper import WhisperModel
            return WhisperModel(self.whisper_size, device="auto", compute_type="int8")
        return self._load("asr", load)

    def voice(self):
        def load():
            from resemblyzer import VoiceEncoder
            return VoiceEncoder(verbose=False)
        return self._load("voice", load)

    def face(self):
        def load():
            from insightface.app import FaceAnalysis
            app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
            app.prepare(ctx_id=-1, det_size=(640, 640))
            return app
        return self._load("face", load)


def transcribe(models: Models, wav: Path) -> dict | None:
    asr = models.asr()
    if not asr:
        return None
    # Pass samples, not a path: faster-whisper's own decoder breaks on newer PyAV releases.
    import wave
    import numpy as np
    with wave.open(str(wav)) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2, "expects 16 kHz mono s16 wav"
        samples = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    lang = models.lang.split("-")[0]
    # Whisper drifts into traditional characters on Mandarin unless primed with simplified text.
    prime = "以下是普通话的句子。" if lang == "zh" else None
    segments, _ = asr.transcribe(samples, language=lang, word_timestamps=True, initial_prompt=prime)
    words = [{"w": w.word, "start": w.start, "end": w.end} for s in segments for w in (s.words or [])]
    return {"text": "".join(w["w"] for w in words).strip(), "words": words}


def line_spans(lines: list[dict], words: list[dict], lang: str) -> list[tuple[float, float] | None]:
    """Find each scripted line's time span by aligning script tokens to ASR tokens."""
    ref, owner = [], []
    for i, ln in enumerate(lines):
        t = tokens(ln["line"], lang)
        ref += t
        owner += [i] * len(t)
    hyp, times = [], []
    for w in words:
        for t in tokens(w["w"], lang):
            hyp.append(t)
            times.append((w["start"], w["end"]))
    spans: list[list[float] | None] = [None] * len(lines)
    for block in difflib.SequenceMatcher(a=ref, b=hyp, autojunk=False).get_matching_blocks():
        for k in range(block.size):
            i = owner[block.a + k]
            s, e = times[block.b + k]
            spans[i] = [min(spans[i][0], s), max(spans[i][1], e)] if spans[i] else [s, e]
    return [tuple(s) if s else None for s in spans]


def cosine(a, b) -> float:
    import numpy as np
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    return round(float(a @ b / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-9)), 3)


def voice_embedding(models: Models, wav: Path):
    enc = models.voice()
    if not enc:
        return None
    from resemblyzer import preprocess_wav
    audio = preprocess_wav(wav)
    return enc.embed_utterance(audio) if len(audio) > 16000 * 0.8 else None


def face_embeddings(models: Models, image: Path) -> list:
    app = models.face()
    if not app:
        return []
    import cv2
    img = cv2.imread(str(image))
    return [f.normed_embedding for f in app.get(img)] if img is not None else []


# ---------- main ----------

def evaluate(scene: dict, out: Path, state: dict, canon: dict[str, Path], whisper_size: str = "small") -> dict:
    lang = scene.get("language", "zh-CN")
    models = Models(lang, whisper_size)
    shots = {s["id"]: s for s in scene["shots"]}
    canon_faces = {c: faces[0] for c, p in canon.items() if p.exists() and (faces := face_embeddings(models, p))}

    results: dict = {"language": lang, "variants": {}, "voice": {}, "notes": models.notes}
    voice_embs: dict[str, list[tuple[str, object]]] = {}

    for key, job in sorted(state["clips"].items()):
        if job.get("status") != "succeeded" or not job.get("file"):
            continue
        variant, shot_id = key.split("/")
        clip, shot = Path(job["file"]), shots[shot_id]
        work = out / "eval" / variant / shot_id
        r = {"file": str(clip), "probe": media.probe(clip), "elapsed_s": job.get("elapsed_s")}

        wav = media.extract_audio(clip, work / "audio.wav")
        asr = transcribe(models, wav)
        if asr:
            expected = "".join(l["line"] for l in shot["dialogue"]) if lang.startswith("zh") else " ".join(l["line"] for l in shot["dialogue"])
            r["asr_text"] = asr["text"]
            r["dialogue_error_rate"] = error_rate(tokens(expected, lang), tokens(asr["text"], lang))
            r["dialogue_metric"] = "CER" if lang.startswith(("zh", "ja")) else "WER"
            spans = line_spans(shot["dialogue"], asr["words"], lang)
            r["lines"] = []
            for i, (ln, span) in enumerate(zip(shot["dialogue"], spans)):
                entry = {"char": ln["char"], "line": ln["line"], "span": span}
                if span and span[1] - span[0] >= 0.8:
                    seg = media.extract_audio(clip, work / f"line{i}_{ln['char']}.wav", span[0], span[1])
                    entry["wav"] = str(seg)
                    emb = voice_embedding(models, seg)
                    if emb is not None:
                        voice_embs.setdefault(f"{variant}/{ln['char']}", []).append((shot_id, emb))
                r["lines"].append(entry)

        if canon_faces:
            frames = media.sample_frames(clip, work / "frames")
            scores = {}
            for c in shot["characters"]:
                ref = canon_faces.get(c)
                if ref is None:
                    continue
                best = [max((cosine(ref, f) for f in face_embeddings(models, fr)), default=None) for fr in frames]
                vals = [b for b in best if b is not None]
                scores[c] = {"mean": round(sum(vals) / len(vals), 3) if vals else None, "frames_with_face": len(vals), "frames": len(frames)}
            r["identity"] = scores

        results["variants"].setdefault(variant, {})[shot_id] = r

    # Voice drift: pairwise similarity of the same character across shots within a variant.
    for key, embs in voice_embs.items():
        pairs = {f"{a}~{b}": cosine(ea, eb) for (a, ea), (b, eb) in itertools.combinations(embs, 2)}
        results["voice"][key] = {"segments": [s for s, _ in embs], "pairwise": pairs,
                                 "min": min(pairs.values()) if pairs else None}
    results["notes"] = models.notes
    (out / "eval.json").write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(scene, out, results)
    return results


def write_report(scene: dict, out: Path, results: dict) -> Path:
    def rel(p):
        return html.escape(os.path.relpath(p, out).replace("\\", "/"))

    variants = sorted(results["variants"])
    rows = []
    for shot in scene["shots"]:
        cells = [f"<th><b>{shot['id']}</b> · {shot['duration']}s<br><small>{html.escape(shot['action'])}</small><br>"
                 + "<br>".join(f"<i>{html.escape(l['char'])}</i>: {html.escape(l['line'])}" for l in shot["dialogue"]) + "</th>"]
        for v in variants:
            r = results["variants"][v].get(shot["id"])
            if not r:
                cells.append("<td class=empty>—</td>")
                continue
            p = r["probe"]
            meta = [f"{p['width']}×{p['height']} · {p['fps']}fps · {p['duration']}s · audio {p['audio_codec'] or 'none'}"]
            if r.get("elapsed_s"):
                meta.append(f"gen time {r['elapsed_s']:.0f}s")
            if "dialogue_error_rate" in r:
                meta.append(f"{r['dialogue_metric']} {r['dialogue_error_rate']:.0%} · heard: “{html.escape(r['asr_text'])}”")
            for c, s in (r.get("identity") or {}).items():
                meta.append(f"face {c}: {s['mean']} ({s['frames_with_face']}/{s['frames']} frames)")
            lines = "".join(f"<div>{html.escape(l['char'])}: <audio controls src='{rel(l['wav'])}'></audio></div>"
                            for l in r.get("lines", []) if l.get("wav"))
            cells.append(f"<td><video controls preload=metadata src='{rel(r['file'])}'></video>"
                         f"<div class=meta>{'<br>'.join(meta)}</div>{lines}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")

    voice = "".join(f"<li><b>{html.escape(k)}</b>: min {v['min']} · {html.escape(json.dumps(v['pairwise']))}</li>"
                    for k, v in sorted(results["voice"].items()))
    notes = "".join(f"<li>{html.escape(n)}</li>" for n in results["notes"])
    page = f"""<!doctype html><meta charset=utf-8><title>M0 spike report</title>
<style>
:root{{--bg:#fff;--fg:#1d1d1f;--muted:#666;--line:#ddd}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151517;--fg:#eee;--muted:#999;--line:#333}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;margin:16px}}
table{{border-collapse:collapse}} th,td{{border:1px solid var(--line);padding:8px;vertical-align:top;text-align:left}}
th{{width:220px;font-weight:normal}} video{{width:240px;aspect-ratio:9/16;background:#000}} audio{{height:28px;width:240px}}
.meta{{color:var(--muted);font-size:12px;max-width:240px}} .empty{{color:var(--muted);text-align:center}}
</style>
<h1>{html.escape(scene['title'])} — M0 spike</h1>
<p>Variants: {', '.join(f'<b>{v}</b>' for v in variants)}. Voice similarity: resemblyzer cosine (≥0.80 usually same speaker, &lt;0.70 clearly different).</p>
<table><tr><th>Shot</th>{''.join(f'<th>Variant {v}</th>' for v in variants)}</tr>{''.join(rows)}</table>
<h2>Voice consistency</h2><ul>{voice or '<li>no speaker metrics (resemblyzer not installed or no aligned lines)</li>'}</ul>
<h2>Notes</h2><ul>{notes or '<li>all metrics enabled</li>'}</ul>
"""
    path = out / "report.html"
    path.write_text(page, encoding="utf-8")
    return path
