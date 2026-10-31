"""Check the ASR + voice-similarity metrics on real speech, using Windows SAPI voices.

Builds 3 fake clips (David, Zira, then David + Zira) for an English scene and runs evaluate().
Expect: low WER, and higher same-speaker voice similarity than cross-speaker similarity.
Run: .venv/Scripts/python spike/tests/run_metrics_sapi.py
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import evaluate  # noqa: E402

VOICES = {"m": "Microsoft David Desktop", "f": "Microsoft Zira Desktop"}
SCENE = {
    "title": "SAPI metrics check", "language": "en-US",
    "characters": {"m": {}, "f": {}},
    "shots": [
        {"id": "S1", "duration": 6, "characters": ["m"], "action": "-", "dialogue": [{"char": "m", "line": "Three years. Three years I believed you were dead."}]},
        {"id": "S2", "duration": 6, "characters": ["f"], "action": "-", "dialogue": [{"char": "f", "line": "I was. The man you married does not exist anymore."}]},
        {"id": "S3", "duration": 8, "characters": ["m", "f"], "action": "-", "dialogue": [
            {"char": "m", "line": "Then tell me who you really are."},
            {"char": "f", "line": "The only one who can keep you alive tonight."}]},
    ],
}


def say(voice: str, text: str, dest: Path) -> None:
    ps = (f"Add-Type -AssemblyName System.Speech; $s=New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          f"$s.SelectVoice('{voice}'); $s.SetOutputToWaveFile('{dest}'); $s.Speak('{text}'); $s.Dispose()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=True)


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="sheelshort_sapi_"))
    state = {"clips": {}}
    for shot in SCENE["shots"]:
        parts = []
        for i, ln in enumerate(shot["dialogue"]):
            wav = work / f"{shot['id']}_{i}.wav"
            say(VOICES[ln["char"]], ln["line"], wav)
            parts.append(wav)
        clip = work / f"{shot['id']}.mp4"
        inputs = sum((["-i", str(p)] for p in parts), [])
        # input 0 is the video; audio parts start at index 1
        graph = "".join(f"[{i + 1}:a]" for i in range(len(parts))) + f"concat=n={len(parts)}:v=0:a=1,apad=whole_dur={shot['duration']}[a]"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c=black:s=432x768:r=24:d={shot['duration']}",
                        *inputs, "-filter_complex", graph,
                        "-map", "0:v", "-map", "[a]", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(clip)],
                       check=True)
        state["clips"][f"T/{shot['id']}"] = {"status": "succeeded", "file": str(clip)}

    res = evaluate.evaluate(SCENE, work, state, canon={}, whisper_size="tiny.en")
    print(json.dumps({k: {s: {m: r.get(m) for m in ("asr_text", "dialogue_error_rate")} for s, r in v.items()}
                      for k, v in res["variants"].items()}, indent=2))
    print(json.dumps(res["voice"], indent=2))
    print("notes:", res["notes"])

    shots = res["variants"]["T"]
    assert all(r["dialogue_error_rate"] <= 0.2 for r in shots.values()), "ASR error too high"
    assert all(l["span"] for r in shots.values() for l in r["lines"]), "a line was not aligned"
    same = min(res["voice"]["T/m"]["pairwise"].values()), min(res["voice"]["T/f"]["pairwise"].values())
    # cross-speaker similarity, computed from the same per-line segments
    m_seg = next(l["wav"] for l in shots["S1"]["lines"])
    f_seg = next(l["wav"] for l in shots["S2"]["lines"])
    models = evaluate.Models("en-US", "tiny.en")
    cross = evaluate.cosine(evaluate.voice_embedding(models, Path(m_seg)), evaluate.voice_embedding(models, Path(f_seg)))
    print(f"same-speaker min: {same}, cross-speaker: {cross}")
    assert min(same) > cross, "voice metric cannot separate speakers"
    print(f"PASS - report: {work / 'report.html'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
