"""Offline tests (no GPU, no servers): python -m pytest tests/ or python tests/test_offline.py"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sheelshort import assemble, h3prompt, perform, qc, speech  # noqa: E402
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
    p.limit_s, p.only_eps = 20, ["E01"]
    assert [c["id"] for c in p.clips("E01")] == ["C1", "C2"]          # 9 s + 6 s; C3 would overshoot to 26 s
    assert p.in_use()[0] == {"dahua", "xiaokang", "cunmin"} and p.master_name() == "master_20s"
    p.limit_s = None
    assert len(p.clips("E01")) == 7


def _scratch_project(tmp, ch):
    root = Path(tmp) / "p"
    (root / "episodes").mkdir(parents=True)
    (root / "bible.json").write_text(json.dumps({"characters": {"a": ch}, "locations": {}}))
    return Project(root)


def test_qc_reads_identity_marks():
    ch = {"keep": "thin scar across the left eyebrow, mole above the left corner of the lips, black suit"}
    marks = qc.expected_marks(ch)
    assert [(m["kind"], m["side"], m["feature"]) for m in marks] == [("scar", "left", "eyebrow"), ("mole", "left", "lips")]
    assert qc.written_age({"look": "a 32-year-old Chinese man", "role": "man"}) == 32
    assert qc._colour_problems("a light-blue cotton shirt, grey trousers", "pale blue shirt", "clothing") == []
    assert qc._colour_problems("a deep-red satin gown", "black dress", "clothing")


def test_qc_aligns_text_to_the_image():
    ch = {"name": "A", "role": "man", "look": "a 56-year-old man with a small mole on his right cheekbone and a thin scar across his left eyebrow.",
          "keep": "small mole on the right cheekbone, thin scar across the left eyebrow", "face_detail": "a small mole on the right cheekbone, a thin scar across the left eyebrow."}
    with tempfile.TemporaryDirectory() as tmp:
        p = _scratch_project(tmp, ch)
        left = qc.align_text(p, "a", [
            "side: mole on the right cheekbone is on the person's left",
            "missing or misplaced: scar across the left eyebrow (seen: scar on nose bridge (center of image))",
            "looks about 72, written as 56", "glasses missing"],
            {"face_marks": [{"kind": "scar", "on": "nose bridge", "image_side": "center"}]})
        a = json.loads((p.root / "bible.json").read_text())["characters"]["a"]
    assert left == ["glasses missing"]                                  # text cannot fix a missing object
    assert "mole on his left cheekbone" in a["look"] and "left cheekbone" in a["keep"]
    assert "scar on the nose bridge" in a["keep"] and "eyebrow" not in a["face_detail"]
    assert a["look"].startswith("a 72-year-old")


def test_offscreen_mention_uses_the_name():
    scene = json.loads((ROOT / "projects/quancun/bible.json").read_text()) | json.loads((ROOT / "projects/quancun/episodes/E03.json").read_text())
    clip = next(c for c in scene["clips"] if c["id"] == "C7")          # mentions {gaoliang}, who is not on screen
    text = h3prompt.build(scene, clip, h3prompt.Refs(char_pictures={"fugui": ["a.png", "b.png"], "xiaokang": ["c.png", "d.png"]},
                                                     location_pictures=["loc.png"]))
    assert "Gao Liang" in text and "{gaoliang}" not in text


class _FakeAutoDL:
    def __init__(self, instances=()):
        self.list = [dict(i) for i in instances]
        self.calls = []

    def instances(self):
        return self.list

    def create(self, app, version, spec, name, dcs=None):
        self.calls.append(("create", spec, name, tuple(dcs or ())))
        uuid = f"pro-new{len(self.list)}"
        self.list.append({"uuid": uuid, "name": name, "status": "running", "region_name": "北京B区"})
        return uuid

    def power_on(self, uuid):
        self.calls.append(("power_on", uuid))

    def power_off(self, uuid):
        self.calls.append(("power_off", uuid))

    def wait_status(self, uuid, want, timeout_s=0):
        return 0.0

    def comfy_url(self, uuid):
        return f"https://{uuid}.example:8443"


class _FakeProject:
    def __init__(self, **fleet_cfg):
        import threading
        from sheelshort.project import config
        self.cfg = config()
        self.cfg["comfy"]["servers"] = ["https://a", "https://b"]
        self.cfg["fleet"].update({"dry_run": False, **fleet_cfg})
        self.lock, self._assigned = threading.Lock(), {}
        self.busy = set()

    def client(self, url):
        p = self

        class C:
            def queue_info(self):
                return (1, 0) if url in p.busy else (0, 0)
        return C()


def _scaled(fl):
    fl.tick()
    if fl._up:
        fl._up.join(5)


def test_fleet_scales_up_and_down():
    from sheelshort.fleet import Fleet
    api = _FakeAutoDL([{"uuid": "pro-west", "name": "sheelshort-1", "status": "shutdown", "region_name": "西北B区"},
                       {"uuid": "pro-mine", "name": "", "status": "running", "region_name": "北京B区"}])
    p = _FakeProject()
    fl = Fleet(p, api=api, clock=lambda: 1000.0)
    fl.pending = 4                               # 20 min of work on 2 servers: (20 - 10) / 6 = 1.7 min saved < 3
    _scaled(fl)
    assert api.calls == []
    assert round(fl.gain_minutes(), 1) == 1.7
    fl.pending = 9                               # 45 min of work: (45 - 10) / 6 = 5.8 min saved: add a server
    _scaled(fl)
    assert api.calls == [("create", "5090-p", "sheelshort-bj-1", ("beijingDC2",))]   # never the stopped west one
    url = fl.managed["pro-new2"]["url"]
    assert url in p.cfg["comfy"]["servers"]
    p.busy.add(url)
    fl.pending = 0
    fl.tick()                                    # busy: stays
    assert url in p.cfg["comfy"]["servers"]
    p.busy.clear()
    fl.tick()                                    # idle and no work left: drained and powered off
    assert url not in p.cfg["comfy"]["servers"] and ("power_off", "pro-new2") in api.calls
    assert all(c[1] != "pro-mine" for c in api.calls if c[0] == "power_off")       # the user's own server untouched


def test_fleet_reuses_a_stopped_server_and_cleans_up():
    from sheelshort.fleet import Fleet
    api = _FakeAutoDL([{"uuid": "pro-old", "name": "sheelshort-bj-1", "status": "shutdown", "region_name": "北京B区"}])
    p = _FakeProject()
    fl = Fleet(p, api=api, clock=lambda: 1000.0)
    fl.pending = 9
    _scaled(fl)
    assert api.calls == [("power_on", "pro-old")]
    fl.stop()
    assert api.calls[-1] == ("power_off", "pro-old") and len(p.cfg["comfy"]["servers"]) == 2


def test_pick_waits_for_a_free_slot():
    import threading
    from sheelshort.project import Project
    with tempfile.TemporaryDirectory() as tmp:
        p = _scratch_project(tmp, {})
        p.cfg["comfy"]["servers"] = ["https://a"]
        p.client = lambda url: type("C", (), {"queue_info": lambda self: (0, 0), "base_url": url})()
        p.pick(), p.pick()                                   # the server's 2 slots are taken
        got = []
        t = threading.Thread(target=lambda: got.append(p.pick().base_url), daemon=True)
        t.start(); t.join(1)
        assert not got                                       # a third job waits locally
        p.cfg["comfy"]["servers"].append("https://new")      # the fleet adds a server
        t.join(5)
        assert got == ["https://new"]


def test_fleet_dry_run_changes_nothing():
    from sheelshort.fleet import Fleet
    api = _FakeAutoDL()
    p = _FakeProject(dry_run=True)
    fl = Fleet(p, api=api, clock=lambda: 1000.0)
    fl.pending = 20
    _scaled(fl)
    assert api.calls == [] and len(p.cfg["comfy"]["servers"]) == 2


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
