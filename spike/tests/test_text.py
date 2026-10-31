"""Unit checks for the text metrics. Run: .venv/Scripts/python spike/tests/test_text.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evaluate import error_rate, line_spans, tokens  # noqa: E402


def test_zh():
    assert tokens("三年了。整整三年！", "zh-CN") == list("三年了整整三年")
    assert error_rate(list("那你到底是谁"), list("那你到底是谁")) == 0
    assert error_rate(list("那你到底是谁"), list("那你倒底是谁")) == round(1 / 6, 3)
    lines = [{"char": "linxia", "line": "那你到底是谁？"}, {"char": "luchen", "line": "今晚唯一能让你活下来的人。"}]
    # whisper returns multi-character "words" for Chinese; one has an ASR error (倒)
    words = [{"w": "那你", "start": 0.5, "end": 0.9}, {"w": "倒底是谁", "start": 0.9, "end": 1.8},
             {"w": "今晚", "start": 2.6, "end": 3.0}, {"w": "唯一能让你", "start": 3.0, "end": 3.9},
             {"w": "活下来的人", "start": 3.9, "end": 4.8}]
    assert line_spans(lines, words, "zh-CN") == [(0.5, 1.8), (2.6, 4.8)]


def test_en():
    assert tokens("You weren't supposed to be here.", "en-US") == ["you", "werent", "supposed", "to", "be", "here"]
    assert error_rate(["a", "b", "c", "d"], ["a", "x", "c"]) == 0.5
    lines = [{"char": "a", "line": "Hello there"}, {"char": "b", "line": "Go away"}]
    words = [{"w": " Hello", "start": 0, "end": 0.4}, {"w": " there.", "start": 0.4, "end": 0.8},
             {"w": " Go", "start": 1.5, "end": 1.7}, {"w": " away!", "start": 1.7, "end": 2.1}]
    assert line_spans(lines, words, "en-US") == [(0, 0.8), (1.5, 2.1)]


if __name__ == "__main__":
    test_zh()
    test_en()
    print("PASS")
