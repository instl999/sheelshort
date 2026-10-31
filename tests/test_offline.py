"""Offline tests (no GPU, no servers): python -m pytest tests/ or python tests/test_offline.py"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["SHEELSHORT_FLEET_OWNED"] = str(Path(tempfile.gettempdir()) / "sheelshort_test_fleet_owned.json")   # never the real one

from sheelshort import assemble, h3prompt, perform, qc, speech  # noqa: E402
from sheelshort.project import Project  # noqa: E402


def test_error_rate_ignores_punctuation_and_tags():
    assert speech.error_rate("整整三年……我以为你——死了。", "整整三年,我以为你死了") == 0.0
    assert speech.error_rate("[哽咽] 求……求您。", "求求您") == 0.0
    assert speech.error_rate("你嫁的那个人回不来了", "你家的那个人回不来") == 0.2


def test_locate_does_not_swallow_the_previous_line():
    ws = [(0, .5, "我上个月"), (.5, 1, "投了五万"), (1, 2, "这才二十天"), (2, 3, "已经变成六万八了。"), (3.5, 4, "二十天"),
          (4, 4.5, "赚一万八?"), (4.5, 5, "这是真的假的?"), (6, 7, "年化收益百分之三百,"), (7, 8, "投十万一个月就是近三万!")]
    lines = ["我上个月投了五万……这才二十天，已经变成六万八了！", "二十天赚一万八？！这是真的假的？",
             "年化收益……百分之三百！投十万，一个月就是近三万！"]
    assert speech.locate(lines, ws) == [(0, 3), (3.5, 5), (6, 8)]
    assert speech.locate(["完全不同的一句话"], ws) == [None]


def test_wrap_two_lines_at_punctuation():
    assert assemble.wrap("我签！") == "我签！"
    w = assemble.wrap("钱是死的，股份是活的，长远看股票更划算！")
    assert w.count(r"\N") == 1 and all(len(x) <= 14 for x in w.split(r"\N")) and "，" not in w
    assert not any(row.startswith(("！", "？")) for row in assemble.wrap("我看他就是拿了开发商的好处！黄谷村不要叛徒，滚出去！").split(r"\N"))
    assert "我们" in assemble.wrap("高老板，您可真是我们黄谷村的大恩人啊！").replace(r"\N", "|").split("|")[0] or \
        "我们" in assemble.wrap("高老板，您可真是我们黄谷村的大恩人啊！").replace(r"\N", "|").split("|")[1]


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
    ids = [c["id"] for c in p.clips("E01")]
    assert ids[0] == "C1" and 15 <= sum(h3prompt.clip_len(c) for c in p.clips("E01")) <= 25
    assert {"dahua", "xiaokang"} <= p.in_use()[0] and "gaoliang" not in p.in_use()[0] and p.master_name() == "master_20s"
    p.limit_s = None
    assert len(p.clips("E01")) == len(json.loads((ROOT / "projects/quancun/episodes/E01.json").read_text())["clips"])


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


def test_reaction_cuts():
    scene = json.loads((ROOT / "projects/quancun/bible.json").read_text()) | json.loads((ROOT / "projects/quancun/episodes/T01.json").read_text())
    assert h3prompt.check_rules(scene) == []                         # lines may run over the reaction cut after them
    bad = json.loads(json.dumps(scene))
    bad["clips"][0]["cuts"].insert(0, {"type": "RX", "subjects": ["xiaokang"], "dur": 2})
    assert any("must follow a line" in e for e in h3prompt.check_rules(bad))
    text = h3prompt.build(scene, scene["clips"][0], h3prompt.Refs(char_pictures={"dahua": ["a.png", "b.png"], "xiaokang": ["c.png", "d.png"]},
                                                                  location_pictures=["loc.png"]))
    assert "line continues off-screen over this shot" in text


def test_name_card_goes_beside_the_face():
    left = assemble.card_events({"name": "王大花", "role": "黄谷村村民", "start": 1, "end": 3, "face": (0.55, 0.3, 0.8, 0.45)}, 1080, 1920, "Songti SC")
    right = assemble.card_events({"name": "王大花", "role": "黄谷村村民", "start": 1, "end": 3, "face": (0.2, 0.3, 0.45, 0.45)}, 1080, 1920, "Songti SC")
    import re as _re
    x = lambda ev: float(_re.search(r"\\pos\((\d+)", ev).group(1))   # noqa: E731
    assert x(left[0]) < 540 < x(right[0])                               # face right of centre: card on the left, and back
    assert "王\\N大\\N花" in left[0]                                  # vertical type


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


def test_fleet_does_not_flap_after_an_idle_power_off():
    from sheelshort.fleet import Fleet
    api, p, now = _FakeAutoDL(), _FakeProject(), [1000.0]
    fl = Fleet(p, api=api, clock=lambda: now[0])
    fl.pending = 9
    _scaled(fl)
    fl.pending = 8
    fl.tick()                                     # the new server is idle from now on
    now[0] += 11 * 60                             # still waiting, yet the new server got no work for 11 min
    _scaled(fl)
    assert api.calls[-1][0] == "power_off" and not fl.managed
    now[0] += 60
    _scaled(fl)
    assert sum(c[0] == "create" for c in api.calls) == 1        # no new server while the backlog has not moved
    fl.pending = 7
    _scaled(fl)
    assert sum(c[0] == "create" for c in api.calls) == 2        # the backlog moves: scaling may resume


def test_fleet_dry_run_changes_nothing():
    from sheelshort.fleet import Fleet
    api = _FakeAutoDL()
    p = _FakeProject(dry_run=True)
    fl = Fleet(p, api=api, clock=lambda: 1000.0)
    fl.pending = 20
    _scaled(fl)
    assert api.calls == [] and len(p.cfg["comfy"]["servers"]) == 2



def test_fit_durations_gives_lines_room_and_never_shortens():
    from sheelshort import h3prompt
    scene = {"language": "zh-CN", "clips": [{"id": "C1", "cuts": [
        {"type": "CU", "line": "这是一句需要差不多四秒钟才能说完的台词啊", "dur": 2.0},
        {"type": "RX", "dur": 1.0},
        {"type": "CU", "line": "好。", "dur": 2.5}]}]}
    changes = h3prompt.fit_durations(scene)
    cuts = scene["clips"][0]["cuts"]
    need = h3prompt.spoken_len(cuts[0]["line"], "zh") + h3prompt.LINE_SLACK_S
    assert len(changes) == 1 and cuts[0]["dur"] + cuts[1]["dur"] >= need and cuts[2]["dur"] == 2.5
    assert h3prompt.fit_durations(scene) == []                   # idempotent
    pc = h3prompt.pacing(scene)
    assert pc["shots"] == 3 and pc["duration_s"] == round(sum(c["dur"] for c in cuts), 1)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
