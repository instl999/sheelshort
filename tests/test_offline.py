"""Offline tests (no GPU, no servers): python -m pytest tests/ or python tests/test_offline.py"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sheelshort import assemble, h3prompt, perform, speech  # noqa: E402
from sheelshort.project import Project  # noqa: E402


def test_error_rate_ignores_punctuation_and_tags():
    assert speech.error_rate("整整三年……我以为你——死了。", "整整三年,我以为你死了") == 0.0
    assert speech.error_rate("[哽咽] 求……求您。", "求求您") == 0.0
    assert speech.error_rate("你嫁的那个人回不来了", "你家的那个人回不来") == 0.2


def test_wrap_two_lines_at_punctuation():
    assert assemble.wrap("我签！") == "我签！"
    w = assemble.wrap("钱是死的，股份是活的，长远看股票更划算！")
    assert w.count(r"\N") == 1 and all(len(x) <= 14 for x in w.split(r"\N")) and "，" not in w


def test_acted_prompt_uses_dialogue_track_and_say():
    scene = json.loads((ROOT / "projects/guilai/bible.json").read_text()) | json.loads((ROOT / "projects/guilai/episodes/E01.json").read_text())
    clip = next(c for c in scene["clips"] if c["id"] == "K1b")
    refs = h3prompt.Refs(dialogue_track=True, char_pictures={"luchen": ["a.png", "b.png"], "linxia": ["c.png", "d.png"]},
                         location_pictures=["loc.png"])
    text = h3prompt.build(scene, clip, refs)
    assert "<Audio 1> is the recorded dialogue of this clip" in text
    assert "partially_copy" in text and "audio reuse" in text
    assert "<d>[Chinese] 我确实……死过一次。</d>" in text      # `say`, punctuation verbatim
    assert "voice-timbre reference" not in text


def test_layout_retimes_cuts_to_acted_lines():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "p"
        (root / "episodes").mkdir(parents=True)
        (root / "perform" / "E01").mkdir(parents=True)
        (root / "bible.json").write_text(json.dumps({"characters": {"a": {}}, "locations": {"l": {}}}))
        (root / "episodes" / "E01.json").write_text(json.dumps({"clips": [{"id": "K1", "location": "l", "cuts": [
            {"type": "MS", "subjects": ["a"]},
            {"type": "CU", "subjects": ["a"], "speaker": "a", "line": "好"}]}]}))
        wav = root / "perform" / "E01" / "K1_2.wav"
        import numpy as np
        import soundfile as sf
        sf.write(wav, np.zeros(int(48000 * 4.0)), 48000)
        (root / "perform" / "E01" / "choice.json").write_text(json.dumps({"K1_2": {
            "clip": "K1", "cut": 2, "speaker": "a", "line": "好", "say": "好", "act": "", "emotion": "", "wav": str(wav),
            "speech_s": 4.0, "takes": []}}))
        p = Project(root)
        t = perform.layout(p, "E01")["K1"]
        assert t["cuts"][1]["dur"] >= 4.0 + p.cfg["perform"]["lead_s"]       # the cut grew to hold the acted line
        assert t["seconds"] == 8 and abs(sum(c["dur"] for c in t["cuts"]) - 8) < 1e-6
        assert t["lines"][0]["start"] == round(3 + p.cfg["perform"]["lead_s"], 3)


def test_test_mode_takes_opening_clips():
    p = Project(ROOT / "projects/quancun")
    p.limit_s = 20
    assert [c["id"] for c in p.clips("E01")] == ["C1", "C2"]          # 9 s + 6 s; C3 would overshoot to 26 s
    assert p.in_use()[0] == {"dahua", "xiaokang", "cunmin"} and p.master_name() == "master_20s"
    p.limit_s = None
    assert len(p.clips("E01")) == 7


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
