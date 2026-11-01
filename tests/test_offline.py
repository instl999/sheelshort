"""Offline tests (no GPU, no servers): python -m pytest tests/ or python tests/test_offline.py"""
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ["SHEELSHORT_FLEET_OWNED"] = str(Path(tempfile.gettempdir()) / "sheelshort_test_fleet_owned.json")   # never the real one

from sheelshort import assemble, h3prompt, media, perform, qc, speech  # noqa: E402
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
    assert left == ["looks about 72, written as 56", "glasses missing"]  # text cannot fix an object; the age is the script's
    assert "mole on his left cheekbone" in a["look"] and "left cheekbone" in a["keep"]
    assert "scar on the nose bridge" in a["keep"] and "eyebrow" not in a["face_detail"]
    assert a["look"].startswith("a 56-year-old")
    from sheelshort import cast
    assert "exactly 56 years old" in cast.age_hint(left, a) and "no deep wrinkles" in cast.age_hint(left, a)


def _talk_clip(cid="C1", loc="village_gate"):
    def cut(t, who, line=None, **kw):
        c = {"type": t, "subjects": [who], "composition": "", "action": "", "camera": "Static camera.", "dur": 2.4, **kw}
        if line:
            c |= {"speaker": who, "line": line, "act": "冷笑", "emotion": "cold", "delivery": "cold"}
        return c
    return {"id": cid, "location": loc, "soundscape": "Wind.", "summary": "{dahua} and {xiaokang} argue.", "cuts": [
        cut("CU", "dahua", "你是不是脑子有病！"),
        cut("RX", "xiaokang"),
        cut("OTS", "xiaokang", "这合同有问题。", over="dahua"),
        cut("CU", "dahua", "就你懂？"),
    ]}


def test_reaction_cuts():
    scene = json.loads((ROOT / "projects/quancun/bible.json").read_text())
    scene["clips"] = [_talk_clip()]
    assert h3prompt.check_rules(scene) == []                         # lines may run over the reaction cut after them
    bad = json.loads(json.dumps(scene))
    bad["clips"][0]["cuts"].insert(0, {"type": "RX", "subjects": ["xiaokang"], "dur": 2})
    assert any("must follow a line" in e for e in h3prompt.check_rules(bad))
    text = h3prompt.build(scene, scene["clips"][0], h3prompt.Refs(char_pictures={"dahua": ["a.png", "b.png"], "xiaokang": ["c.png", "d.png"]},
                                                                  location_pictures=["loc.png"]))
    assert "line continues off-screen over this shot" in text


def test_jump_cuts_are_refused():
    scene = json.loads((ROOT / "projects/quancun/bible.json").read_text())
    clip = _talk_clip()
    clip["cuts"][2] = {**clip["cuts"][2], "type": "CU"}                # RX on Xiaokang, then his close-up: a jump
    del clip["cuts"][2]["over"]
    scene["clips"] = [clip]
    assert any("C1 cut 3: jump cut" in e for e in h3prompt.check_rules(scene))
    a, b = _talk_clip("C1"), _talk_clip("C2")                         # C1 ends and C2 opens on Dahua's close-up
    scene["clips"] = [a, b]
    assert any("C2 cut 1: jump cut" in e for e in h3prompt.check_rules(scene))
    b["location"] = "lijia_home"                                      # a new scene: not a jump
    assert not any("jump" in e for e in h3prompt.check_rules(scene))
    scene["clips"] = [clip]                                           # --fit turns the repeated close-up into the reverse angle
    assert h3prompt.fix_jump_cuts(scene) == ["C1 cut 3: close-up -> over-the-shoulder past dahua"]
    assert clip["cuts"][2]["type"] == "OTS" and clip["cuts"][2]["over"] == "dahua"
    assert not any("jump" in e for e in h3prompt.check_rules(scene))


def test_singles_keep_screen_direction_and_look_off_axis():
    scene = json.loads((ROOT / "projects/quancun/bible.json").read_text())
    clip = _talk_clip()
    scene["clips"] = [clip]
    sides = h3prompt.blocking(scene, clip)
    assert {sides["dahua"], sides["xiaokang"]} == {"left", "right"}
    text = h3prompt.build(scene, clip, h3prompt.Refs(char_pictures={"dahua": ["a.png", "b.png"], "xiaokang": ["c.png", "d.png"]},
                                                     location_pictures=["loc.png"]))
    shots = text.split("detailed_description:")[1].split("[Shot ")[1:]
    look = {"left": "right", "right": "left"}
    assert f"toward the {look[sides['dahua']]} of frame" in shots[0] and "never looks into the lens" in shots[0]
    assert f"toward the {look[sides['xiaokang']]} of frame" in shots[1]     # the reaction looks back across the line
    assert f"at the {sides['dahua']} edge of frame" in shots[2]           # the shoulder sits on its own side
    assert "85mm" in shots[0] and h3prompt.CINEMA in text
    scene["blocking"] = {"village_gate": {"dahua": "right", "xiaokang": "left"}}
    assert h3prompt.blocking(scene, clip) == {"dahua": "right", "xiaokang": "left"}


def test_craft_notes_flag_flat_light_and_a_static_camera():
    scene = json.loads((ROOT / "projects/quancun/bible.json").read_text())
    scene["clips"] = [_talk_clip()]
    scene["locations"]["village_gate"]["light"] = "bright natural midday sunlight, soft shadows"
    notes = " | ".join(h3prompt.craft_notes(scene))
    assert "flat light" in notes and "static camera" in notes and "close-ups" in notes
    scene["locations"]["village_gate"]["light"] = "low golden sun behind the crowd, warm rim light, long shadows"
    for c in scene["clips"][0]["cuts"]:
        c["camera"] = "Slow push in."
    assert not any("flat light" in n or "static" in n for n in h3prompt.craft_notes(scene))


def test_edge_trims_drop_flash_shots_but_never_a_line():
    real = media.shot_cuts
    try:
        media.shot_cuts = lambda v: [0.3, 4.0, 11.8]
        assert assemble.edge_trims(Path("x.mp4"), 12.2, [{"start": 0.5, "end": 11.0}]) == (0.3, 11.8)
        assert assemble.edge_trims(Path("x.mp4"), 12.2, [{"start": 0.1, "end": 12.0}]) == (0.0, 12.2)
        media.shot_cuts = lambda v: [4.0]
        assert assemble.edge_trims(Path("x.mp4"), 12.2, []) == (0.0, 12.2)
    finally:
        media.shot_cuts = real


def test_name_card_goes_beside_the_face():
    left = assemble.card_events({"name": "王大花", "role": "黄谷村村民", "start": 1, "end": 3, "face": (0.55, 0.3, 0.8, 0.45)}, 1080, 1920, "Songti SC")
    right = assemble.card_events({"name": "王大花", "role": "黄谷村村民", "start": 1, "end": 3, "face": (0.2, 0.3, 0.45, 0.45)}, 1080, 1920, "Songti SC")
    import re as _re
    x = lambda ev: float(_re.search(r"\\pos\((\d+)", ev).group(1))   # noqa: E731
    assert x(left[0]) < 540 < x(right[0])                               # face right of centre: card on the left, and back
    assert "王\\N大\\N花" in left[0]                                  # vertical type


def test_offscreen_mention_uses_the_name():
    scene = json.loads((ROOT / "projects/quancun/bible.json").read_text())
    clip = {"id": "C1", "location": "restaurant_door", "soundscape": "Traffic.",
            "summary": "{fugui} praises {gaoliang} to {xiaokang}.",            # mentions {gaoliang}, who is not on screen
            "cuts": [{"type": "CU", "subjects": ["fugui"], "speaker": "fugui", "line": "高老板可是大恩人！", "act": "谄媚",
                      "emotion": "joy", "delivery": "fawning", "composition": "Close-up of {fugui}.",
                      "action": "{fugui} grins, thinking of {gaoliang}.", "camera": "Static camera.", "end": "", "dur": 3},
                     {"type": "RX", "subjects": ["xiaokang"], "composition": "Close-up of {xiaokang}.", "action": "{xiaokang} frowns.",
                      "camera": "Static camera.", "end": "", "dur": 2}]}
    scene["clips"] = [clip]
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

    def _set(self, uuid, status):
        for i in self.list:
            if i["uuid"] == uuid:
                i["status"] = status

    def power_on(self, uuid):
        self.calls.append(("power_on", uuid))
        self._set(uuid, "running")

    def power_off(self, uuid):
        self.calls.append(("power_off", uuid))
        self._set(uuid, "shutdown")

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
        self.busy, self.dead = set(), set()
        self.root, self.dry = Path("/nonexistent"), False

    def client(self, url):
        p = self

        class C:
            def queue_info(self):
                if url in p.dead:
                    raise ConnectionError(url)
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
        p.client = lambda url: type("C", (), {"queue_info": lambda self, quick=False: (0, 0), "base_url": url})()
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
    assert api.calls[-1] == ("power_on", "pro-new0")            # the backlog moves: scaling resumes, reusing the stopped one


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



def test_voice_over_keeps_the_speaker_in_frame():
    scene = json.loads((ROOT / "projects/quancun/bible.json").read_text())
    clip = {"id": "C1", "location": "village_gate", "soundscape": "Wind.", "summary": "{xiaokang} reads.",
            "cuts": [{"type": "VO", "subjects": ["xiaokang"], "speaker": "xiaokang", "line": "我发现了一个问题。", "act": "低沉",
                      "emotion": "calm", "delivery": "tense", "composition": "Close-up of {xiaokang}.", "action": "He reads.",
                      "end": "", "dur": 5}]}
    scene["clips"] = [clip]
    text = h3prompt.build(scene, clip, h3prompt.Refs(char_pictures={"xiaokang": ["c.png", "d.png"]}, location_pictures=["loc.png"]))
    assert "off-screen" not in text and "stays in frame" in text


def test_clipqc_flags_a_character_shot_with_nobody_in_it():
    from sheelshort import clipqc
    chars = {"xk": {"name": "Li Xiaokang", "role": "young man", "look": "a young man"}}
    empty = {"people": 0, "main_person_sex": "none", "night": False}
    assert clipqc.judge_panel(chars, 3, {"type": "VO", "subjects": ["xk"]}, empty, False, True) == \
        ["cut 3: nobody in the shot, should show Li Xiaokang"]
    assert clipqc.judge_panel(chars, 3, {"type": "ECU", "subjects": ["xk"]}, empty, False, True) == []   # hands or a prop
    assert clipqc.judge_panel(chars, 1, {"type": "CU", "subjects": ["xk"]}, {"people": 1, "main_person_sex": "woman"}, False, False) == \
        ["cut 1: shows a woman, should be Li Xiaokang"]



def test_clipqc_samples_a_shot_h3_inserted():
    from sheelshort import clipqc
    clip = {"cuts": [{"type": "MS", "dur": 2.4}, {"type": "CU", "dur": 1.8}, {"type": "VO", "dur": 7.0}]}
    # E02 C3 as rendered: H3 cut at 2.46, 4.17 and 7.0; 4.17-7.0 was an empty set that no planned midpoint fell in
    times = clipqc.sample_times(clip, 11.54, [2.458, 4.167, 7.0])
    assert len(times) == 4 and any(4.17 < t < 7.0 for t in times)
    assert clipqc.cut_at(clip, 5.6) == 2 and clipqc.cut_at(clip, 99) == 2
    assert len(clipqc.sample_times(clip, 11.2, [2.4, 4.2])) == 3      # cut as planned: one sample per cut



def test_fleet_replaces_an_address_that_does_not_answer_as_comfyui():
    from sheelshort.fleet import Fleet
    api = _FakeAutoDL()
    p = _FakeProject()
    now = [1000.0]
    fl = Fleet(p, api=api, clock=lambda: now[0])
    fl.pending = 9
    _scaled(fl)
    wrong = fl.managed["pro-new0"]["url"]
    p.dead.add(wrong)                            # the app's other port: answers, but not as ComfyUI
    right = "https://pro-new0-6006.example:8443"
    api.comfy_url = lambda uuid: right
    fl.tick()
    assert wrong in p.cfg["comfy"]["servers"]    # one failed probe is not enough
    assert not any(c[0] == "power_off" for c in api.calls)                   # unreachable never counts as idle
    now[0] += 200
    fl.tick()
    assert right in p.cfg["comfy"]["servers"] and wrong not in p.cfg["comfy"]["servers"]
    assert fl.managed["pro-new0"]["url"] == right



def test_tile_of_one_image():
    import tempfile
    from sheelshort import media
    d = Path(tempfile.mkdtemp())
    media._run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=red:s=64x96", "-frames:v", "1", str(d / "a.jpg")])
    out = media.tile([d / "a.jpg"], d / "t.jpg", cols=1, cell=(32, 48))
    assert out.exists() and out.stat().st_size > 0



def test_fleet_keeps_a_server_that_was_just_handed_a_job():
    from sheelshort.fleet import Fleet
    api = _FakeAutoDL()
    p = _FakeProject()
    fl = Fleet(p, api=api, clock=lambda: 1000.0)
    fl.pending = 9
    _scaled(fl)
    url = fl.managed["pro-new0"]["url"]
    fl.pending = 0
    p._assigned[url] = 1                         # a vision check picked it after the last render finished
    fl.tick()
    assert url in p.cfg["comfy"]["servers"] and not any(c[0] == "power_off" for c in api.calls)
    p._assigned[url] = 0
    fl.tick()
    assert url not in p.cfg["comfy"]["servers"] and ("power_off", "pro-new0") in api.calls
    fl.stop()



def test_clipqc_lamp_lit_room_is_a_night_scene():
    from sheelshort import clipqc
    chars = {"sd": {"name": "Li Shoudi", "role": "old man", "look": "an old man"}}
    cu = {"type": "CU", "subjects": ["sd"]}
    lamp = {"people": 1, "main_person_sex": "man", "light": "lamp"}
    assert clipqc.judge_panel(chars, 1, cu, lamp, True, False) == []
    assert clipqc.judge_panel(chars, 1, cu, {**lamp, "light": "daylight"}, True, False) == ["cut 1: daylight in a night scene"]
    assert clipqc.judge_panel(chars, 1, cu, {**lamp, "light": "night"}, False, True) == ["cut 1: night look in a day scene"]



def test_clipqc_text_on_a_prop_is_not_a_caption():
    from sheelshort import clipqc
    chars = {"fg": {"name": "Fugui", "role": "man", "look": "a man"}}
    props = {"card": {"look": "a small white business card with a few lines of tiny printed characters"},
             "cup": {"look": "a plain white cup"}}
    pan = {"people": 1, "main_person_sex": "man", "text_or_subtitles": True}
    assert clipqc.judge_panel(chars, 2, {"type": "CU", "subjects": ["fg"], "props": ["card"]}, pan, False, False, props) == []
    assert clipqc.judge_panel(chars, 2, {"type": "CU", "subjects": ["fg"], "props": ["cup"]}, pan, False, False, props) == \
        ["cut 2: on-screen text"]


def _core_fleet():
    from sheelshort import fleet
    fleet.OWNED.unlink(missing_ok=True)
    api = _FakeAutoDL([{"uuid": "pro-a", "name": "", "status": "shutdown", "region_name": "北京B区"},
                       {"uuid": "pro-b", "name": "", "status": "shutdown", "region_name": "北京B区"},
                       {"uuid": "pro-x", "name": "sheelshort-bj-1", "status": "shutdown", "region_name": "北京B区"},
                       {"uuid": "pro-mine", "name": "other", "status": "running", "region_name": "北京B区"}])
    p = _FakeProject(core=["pro-a", "pro-b"])
    p.cfg["comfy"]["servers"] = []
    return fleet, api, p


def test_core_servers_start_on_the_first_job_and_stop_when_idle():
    fleet, api, p = _core_fleet()
    spawned = []
    ses = fleet.Session(p, api=api, spawn=spawned.append)
    assert api.calls == []                                     # nothing starts before a job needs a GPU
    ses.ensure(); ses.ensure()
    assert api.calls == [("power_on", "pro-a")]                # min_servers = 1: the first core server only
    assert p.cfg["comfy"]["servers"] == ["https://pro-a.example:8443"]
    assert fleet.holders("pro-a") == [os.getpid()]
    assert spawned == [p.root]                                 # the reaper watches from the start (a killed run too)
    ses.release()
    assert spawned == [p.root, p.root] and fleet.holders("pro-a") == []
    t0 = fleet.time.time()                                     # let_go stamped "idle since" with real time
    left = fleet.reap_idle(api, p.cfg["fleet"], log=lambda m: None, clock=lambda: t0 + 60)
    assert api.calls[-1] == ("power_on", "pro-a") and 500 < left <= 600     # 10 min not up yet
    fleet.reap_idle(api, p.cfg["fleet"], log=lambda m: None, clock=lambda: t0 + 601)
    assert api.calls[-1] == ("power_off", "pro-a")
    assert not any(c[1] == "pro-mine" for c in api.calls)   # never an instance that is not ours
    assert fleet.reap_idle(api, p.cfg["fleet"], log=lambda m: None) is None    # nothing left to watch


def test_a_running_core_server_is_used_and_kept_while_another_run_holds_it():
    fleet, api, p = _core_fleet()
    api._set("pro-b", "running")
    ses = fleet.Session(p, api=api, spawn=lambda root: None)
    ses.ensure()
    assert api.calls == [] and p.cfg["comfy"]["servers"] == ["https://pro-b.example:8443"]   # warm: no power-on
    with fleet._registry() as d:
        d["pro-b"]["pids"].append(os.getppid())                 # another live sheelshort run uses it too
    ses.release()
    fleet.reap_idle(api, p.cfg["fleet"], log=lambda m: None, clock=lambda: fleet.time.time() + 3600)
    assert api.calls == []
    with fleet._registry() as d:
        d["pro-b"]["pids"] = []
        d["pro-b"]["since"] = 0.0
    fleet.reap_idle(api, p.cfg["fleet"], log=lambda m: None, idle=lambda url: False)   # its queue has work
    assert api.calls == []
    fleet.reap_idle(api, p.cfg["fleet"], log=lambda m: None, clock=lambda: fleet.time.time() + 601)
    assert api.calls == [("power_off", "pro-b")]


def test_fleet_powers_on_core_servers_before_fleet_ones():
    from sheelshort.fleet import Fleet
    fleet, api, p = _core_fleet()
    fleet.Session(p, api=api, spawn=lambda root: None).ensure()   # pro-a, as the command's first server
    fl = Fleet(p, api=api, clock=lambda: 1000.0)
    fl.pending = 30
    _scaled(fl)
    assert api.calls == [("power_on", "pro-a"), ("power_on", "pro-b")]
    fl.stop()
    assert api.calls[-1] == ("power_off", "pro-b") and p.cfg["comfy"]["servers"] == ["https://pro-a.example:8443"]


def test_eta_learns_job_times_and_chains_jobs_on_a_server():
    import time
    from sheelshort import eta
    with tempfile.TemporaryDirectory() as tmp:
        t = eta.Timing(Path(tmp) / "times.json")
        assert t.expected("render/E01/C1_h3_t1", 10) == 300          # default: 30 s per second of video
        t.record("render/E01/C1_h3_t1", 200, 10)                     # measured: 20 s per second
        assert eta.Timing(Path(tmp) / "times.json").expected("render/E02/C4_h3_t1", 12) == 240   # remembered
        p = _scratch_project(tmp, {})
        p.timing = t
        now = time.time()
        p.state["jobs"]["upscale/E01/a"] = {"status": "queued", "server": "https://s1", "eta": now + 100}
        p._touched.add("upscale/E01/a")
        assert abs(p._eta("upscale/E01/b", "https://s1", 1) - (now + 125)) < 2      # waits for the job ahead on s1
        assert abs(p._eta("upscale/E01/c", "https://s2", 1) - (now + 25)) < 2       # s2 is free
        p.cfg["comfy"]["servers"] = ["https://s1", "https://s2"]
        pr = eta.Progress(p, "upscale", [("upscale/E01/a", 1), ("upscale/E01/x", 1), ("upscale/E01/y", 1)])
        assert abs(pr.eta() - (now + 125)) < 2                       # a ends at +100, then x and y share 2 servers
        pr.finish("upscale/E01/a")
        info = json.loads((p.root / "progress.json").read_text())
        assert info["phases"]["upscale"]["done"] == 1 and info["phases"]["upscale"]["total"] == 3
    assert eta.exec_seconds({"status": {"messages": [["execution_start", {"timestamp": 1000}],
                                                     ["execution_success", {"timestamp": 13500}]]}}) == 12.5
    assert eta.span(40) == "40 s" and eta.span(600) == "10 min"


def test_a_lone_speaker_jump_cut_moves_the_camera_around_them():
    scene = json.loads((ROOT / "projects/quancun/bible.json").read_text())
    phone = _talk_clip()
    phone["cuts"] = [phone["cuts"][0], json.loads(json.dumps(phone["cuts"][0]))]   # dahua alone on the phone, twice
    phone["cuts"][1]["line"] = "你听我说。"
    scene["clips"] = [phone]
    assert any("jump cut" in e for e in h3prompt.check_rules(scene))
    assert h3prompt.fix_jump_cuts(scene) == ["C1 cut 2: same framing again -> profile angle"]
    assert not any("jump cut" in e for e in h3prompt.check_rules(scene))
    text = h3prompt.build(scene, phone, h3prompt.Refs(char_pictures={"dahua": ["a.png", "b.png"]}, location_pictures=["loc.png"]))
    assert "in profile from the side" in text


def test_a_long_run_of_faces_gets_a_medium_shot():
    scene = json.loads((ROOT / "projects/quancun/bible.json").read_text())
    scene["clips"] = [_talk_clip(f"C{n}") for n in range(1, 5)]       # 4 x 9.6 s of close-ups at one place
    for n, cl in enumerate(scene["clips"]):                            # alternate who opens, so no jump cuts
        if n % 2:
            cl["cuts"][0]["subjects"] = ["xiaokang"]; cl["cuts"][0]["speaker"] = "xiaokang"
            cl["cuts"][1]["subjects"] = ["dahua"]
            cl["cuts"][3]["subjects"] = ["xiaokang"]; cl["cuts"][3]["speaker"] = "xiaokang"
    for cl in scene["clips"]:
        cl["cuts"][0]["line"] = "好。"                                   # short enough not to need its reaction cut
    assert h3prompt.close_runs(scene)[0][1:] == (0, 15, 38.4)
    assert any("38 s of close-ups" in n for n in h3prompt.craft_notes(scene))
    changes = h3prompt.fix_close_runs(scene)
    assert changes and all("medium shot" in c for c in changes)
    assert h3prompt.close_runs(scene) == [] and h3prompt.check_rules(scene) == []


def test_clipqc_does_not_read_a_phone_screen_insert_as_a_caption():
    from sheelshort import clipqc
    chars = {"xk": {"name": "Li Xiaokang", "role": "man", "look": "a man"}}
    pan = {"people": 0, "main_person_sex": "none", "text_or_subtitles": True}
    ecu = {"type": "ECU", "subjects": ["xk"], "composition": "Extreme close-up of the phone screen in {xk}'s hand."}
    assert clipqc.judge_panel(chars, 3, ecu, pan, False, False) == []
    assert clipqc.judge_panel(chars, 3, {**ecu, "type": "CU", "composition": "Close-up of {xk} on the phone."},
                              {**pan, "people": 1, "main_person_sex": "man"}, False, False) == ["cut 3: on-screen text"]


def test_reference_structure_notes_length_openings_and_endings():
    scene = json.loads((ROOT / "projects/quancun/bible.json").read_text())
    clips = []
    for n in range(12):                                                   # 12 x 9.6 s = 115 s, all close shots
        cl = _talk_clip(f"C{n + 1}", "village_gate" if n < 6 else "village_tree")
        if n % 2:
            for c in cl["cuts"]:
                c["subjects"] = ["xiaokang" if c["subjects"] == ["dahua"] else "dahua"]
                if c.get("speaker"):
                    c["speaker"] = c["subjects"][0]
                if c.get("over"):
                    c["over"] = "xiaokang" if c["over"] == "dahua" else "dahua"
        clips.append(cl)
    scene["clips"] = clips
    notes = " | ".join(h3prompt.craft_notes(scene))
    assert "runs 115 s" in notes and "open without a wide or medium shot of the place: C7 (village_tree)." in notes
    assert "C1 (" not in notes                                                # the episode's own opening may be a cold open
    assert "last shot is" not in notes                                        # a 2.4 s close-up is a good last shot
    scene["clips"][-1]["cuts"][-1] = {"type": "WS", "subjects": [], "composition": "", "action": "", "camera": "", "dur": 3}
    assert "last shot is WS" in " | ".join(h3prompt.craft_notes(scene))


def test_medium_close_up_carries_a_line_and_look_presets_change_the_prompt():
    scene = json.loads((ROOT / "projects/quancun/bible.json").read_text())
    clip = _talk_clip()
    clip["cuts"][0]["type"] = "MCU"
    scene["clips"] = [clip]
    assert h3prompt.check_rules(scene) == []
    refs = h3prompt.Refs(char_pictures={"dahua": ["a.png", "b.png"], "xiaokang": ["c.png", "d.png"]}, location_pictures=["l.png"])
    text = h3prompt.build(scene, clip, refs)
    assert "medium close-up from mid-chest up of <Subject 1> on a 65mm lens" in text and "high-end streaming drama" in text
    scene["look_preset"] = "bright"
    assert "bright, polished streaming comedy" in h3prompt.build(scene, clip, refs)
    scene["look_preset"] = "noir"
    assert any("look_preset" in e for e in h3prompt.check_rules(scene))
    from sheelshort import cast
    assert "high-key" in cast.plate_prompt({"look": "a kitchen.", "light": "bright window light"}, "bright")


def test_location_card_and_grade_follow_the_bible():
    from sheelshort import assemble
    ev = assemble.card_events({"kind": "place", "name": "秦州府衙", "start": 0.1, "end": 2.6}, 1080, 1920, "Songti SC")
    assert len(ev) == 1 and r"\an7" in ev[0] and "秦\\N州\\N府\\N衙" in ev[0]
    with tempfile.TemporaryDirectory() as tmp:
        p = _scratch_project(tmp, {})
        p.cfg["edit"]["grade"] = "auto"
        assert "vignette=angle=PI/7" in assemble.grade(p)                  # cinematic by default
        p.bible["look_preset"] = "bright"
        assert assemble.grade(p) == assemble.GRADES["bright"] and "vignette" not in assemble.grade(p)
        p.cfg["edit"]["grade"] = "none"
        assert assemble.grade(p) == ""


def test_place_card_waits_for_the_wide_shot_and_subtitles_never_overlap():
    from sheelshort import assemble
    with tempfile.TemporaryDirectory() as tmp:
        p = _scratch_project(tmp, {"name": "A", "name_zh": "林薇", "card": "前女友"})
        p.bible["locations"] = {"room": {"name_zh": "云顶餐厅"}}
        p.cfg["render"]["dialogue"] = "h3"
        clip = {"id": "K1", "location": "room", "cuts": [{"type": "CU", "subjects": ["a"], "dur": 1.7},
                                                          {"type": "WS", "subjects": ["a"], "dur": 2.0}]}
        (p.root / "episodes" / "E01.json").write_text(json.dumps({"id": "E01", "clips": [clip]}))
        real = (assemble.chosen_take, assemble.edge_trims, assemble.heard_lines, media.face_box, media.shot_cuts, media.probe)
        media.probe = lambda v: {"duration": 3.7}
        assemble.chosen_take = lambda p, ep, c: Path(tmp) / "K1.mp4"
        assemble.edge_trims = lambda v, s, l: (0.0, 3.7)
        assemble.heard_lines = lambda p, ep, c, v: [{"text": "二叔。", "start": 0.2, "end": 0.5},
                                                    {"text": "你来了", "start": 0.9, "end": 2.0}]
        media.face_box = lambda v, t: None
        media.shot_cuts = lambda v, threshold=0.3: [1.7]
        try:
            (Path(tmp) / "K1.mp4").write_bytes(b"")
            plan = assemble.edl(p, "E01")
        finally:
            assemble.chosen_take, assemble.edge_trims, assemble.heard_lines, media.face_box, media.shot_cuts, media.probe = real
        name = [c for c in plan["cards"] if c.get("kind") != "place"]
        place = [c for c in plan["cards"] if c.get("kind") == "place"]
        assert name and name[0]["start"] == 0.15                                  # the cold-open face keeps its name card
        assert place and place[0]["start"] == 1.8                                 # the place card sits on the wide shot
        a, b = plan["subtitles"]
        assert a["end"] == 0.86 and b["start"] == 0.9                             # stretched toward 1 s, but no overlap


def test_sound_effects_land_on_the_rendered_cut_at_a_level_set_by_the_dialogue():
    import subprocess
    import numpy as np
    from sheelshort import sfx
    clip = {"id": "K1", "location": "x", "cuts": [{"type": "MS", "dur": 3}, {"type": "CU", "dur": 3, "sfx": "boom"},
                                                   {"type": "CU", "dur": 2, "sfx": [{"cue": "boom", "at": 0.5}, "nope"]}]}
    scene = {"sfx": {"boom": {"file": "inputs/boom.wav", "sync": 0.1, "len": 0.5, "level": 0}}, "clips": [clip]}
    errs = sfx.check(scene)
    assert len(errs) == 1 and "'nope'" in errs[0]
    clip["cuts"][2]["sfx"] = [{"cue": "boom", "at": 0.5}]
    with tempfile.TemporaryDirectory() as tmp:
        p = _scratch_project(tmp, {})
        p.bible["sfx"] = scene["sfx"]
        (p.root / "inputs").mkdir()
        # the cue: 0.1 s of silence, then a 0.4 s tone (its sync point is the tone's onset)
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=880:d=0.4", "-af", "adelay=100,volume=-6dB",
                        str(p.root / "inputs" / "boom.wav")], check=True)
        dia = p.root / "dia.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=220:d=8", "-af", "volume=-30dB", str(dia)], check=True)
        real = media.shot_cuts
        media.shot_cuts = lambda video, threshold=0.3: [3.2, 6.1]          # H3 cut 0.2 s later than written
        try:
            ev = sfx.events_in(p, clip, 10.0, 0.0, 8.0, dia)
        finally:
            media.shot_cuts = real
        assert ev == [{"cue": "boom", "start": 13.2, "dur": 3.0}, {"cue": "boom", "start": 16.5, "dur": 1.5}]
        plan = {"sfx": [dict(ev[0], start=3.2)]}
        ins, filt = ["-i", str(dia)], ["[0:a]aresample=48000,aformat=channel_layouts=stereo[dia]"]
        out = sfx.mix(p, plan, ins, filt, "[dia]", [(dia, 8.0)])
        assert out == "[withfx]"
        raw = subprocess.run(["ffmpeg", "-v", "error", *ins, "-filter_complex", ";".join(filt), "-map", out, "-ac", "1",
                              "-ar", "8000", "-f", "s16le", "-"], capture_output=True, check=True).stdout
        a = np.frombuffer(raw, np.int16).astype(np.float32)
        env = np.sqrt(np.convolve(a ** 2, np.ones(80) / 80, "same"))
        onset = np.argmax(env > 1.25 * np.median(env)) / 8000
        assert abs(onset - 3.2) < 0.03, onset                                   # the tone's onset, not the file's start
        lift = 20 * np.log10(env[int(3.4 * 8000)] / np.median(env))
        assert 1.5 < lift < 6, lift                                             # about as loud as the dialogue (level 0)


def test_an_outfit_is_the_same_person_in_other_clothes():
    from sheelshort import wardrobe
    ch = {"name": "Chen Mo", "name_zh": "陈默", "role": "man", "look": "a 28-year-old man in a blue raincoat", "keep": "scar, blue raincoat",
          "costume": "blue raincoat", "card": "外卖员",
          "outfits": {"suit": {"look": "a 28-year-old man in a charcoal suit", "keep": "scar, charcoal suit", "costume": "charcoal suit"}}}
    with tempfile.TemporaryDirectory() as tmp:
        p = _scratch_project(tmp, ch)
        p.bible["characters"]["b"] = {"name": "B", "role": "woman"}
        ep = {"id": "E02", "outfits": {"a": "suit"}, "blocking": {"room": {"a": "left", "b": "right"}}, "clips": [
            {"id": "K1", "location": "room", "summary": "{a} enters.", "cuts": [
                {"type": "MCU", "subjects": ["a"], "speaker": "a", "to": "b", "line": "我回来了。", "action": "{a} smiles at {b}."}]},
            {"id": "K2", "location": "room", "outfits": {"a": "default"}, "cuts": [{"type": "CU", "subjects": ["a"]}]}]}
        (p.root / "episodes" / "E02.json").write_text(json.dumps(ep, ensure_ascii=False))
        k1, k2 = p.clips("E02")
        cut = k1["cuts"][0]
        assert cut["subjects"] == ["a__suit"] and cut["speaker"] == "a__suit" and cut["to"] == "b"
        assert cut["action"] == "{a__suit} smiles at {b}." and k1["summary"] == "{a__suit} enters."
        assert k1["blocking"] == {"a__suit": "left", "b": "right"} and k2["cuts"][0]["subjects"] == ["a"]
        suit = p.chars["a__suit"]
        assert suit["costume"] == "charcoal suit" and suit["name_zh"] == "陈默" and "outfits" not in suit
        assert p.voice("a__suit") == p.voice("a") and p.base("a__suit") == "a"
        assert p.episode("E02")["clips"][0]["cuts"][0]["subjects"] == ["a"]       # the file keeps the writer's ids
        assert p.scene("E02")["clips"][0]["cuts"][0]["subjects"] == ["a__suit"]
        assert wardrobe.check(p.bible, ep) == []
        ep["clips"][1]["outfits"] = {"a": "tux"}
        assert any("no outfit 'tux'" in e for e in wardrobe.check(p.bible, ep))
        from sheelshort import cast
        assert "Change only the clothing to: charcoal suit" in cast.outfit_prompt(suit)
        assert "Keep every identifying mark exactly where it is, unchanged in size: scar." in cast.outfit_prompt(suit)


def test_a_phone_line_is_heard_through_the_phone_and_extras_are_written_in():
    scene = json.loads((ROOT / "projects/quancun/bible.json").read_text())
    clip = _talk_clip()
    clip["cuts"] = [{"type": "MCU", "subjects": ["xiaokang"], "speaker": "dahua", "via": "phone", "line": "你爹出事了。",
                     "act": "急促", "emotion": "fear", "delivery": "breathless", "composition": "", "action": "", "camera": "Static camera.", "dur": 3},
                    {"type": "MS", "subjects": ["xiaokang"], "extras": "two villagers mending a net", "composition": "",
                     "action": "", "camera": "Static camera.", "dur": 2}]
    scene["clips"] = [clip]
    assert h3prompt.check_rules(scene) == []
    text = h3prompt.build(scene, clip, h3prompt.Refs(char_pictures={"xiaokang": ["a.png", "b.png"], "dahua": ["c.png", "d.png"]},
                                                     location_pictures=["l.png"]))
    assert "never appears in the picture of this clip" in text and "voice comes through the phone held by" in text
    assert "inner voice" not in text and "In the background: two villagers mending a net" in text
    clip["cuts"][0]["subjects"] = ["dahua"]
    assert any("heard from the other end" in e for e in h3prompt.check_rules(scene))
    from sheelshort import clipqc
    chars = scene["characters"]
    crowd = {"people": 6, "main_person_sex": "man"}
    assert any("6 people" in x for x in clipqc.judge_panel(chars, 1, {"type": "MS", "subjects": ["xiaokang"]}, crowd, False, False))
    assert not any("people in" in x for x in clipqc.judge_panel(chars, 1, clip["cuts"][1], crowd, False, False))


def test_phone_filter_thins_only_the_phone_line():
    import subprocess
    import numpy as np
    filt = ["[0:a]anull[p0]"] + assemble.phone_filter("[p0]", "[a0]", [(1.0, 2.0)], "ph0")
    raw = subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=f=120:d=3", "-filter_complex", ";".join(filt),
                          "-map", "[a0]", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"], capture_output=True, check=True).stdout
    a = np.frombuffer(raw, np.int16).astype(np.float32)
    rms = lambda t0, t1: float(np.sqrt(np.mean(a[int(t0 * 8000):int(t1 * 8000)] ** 2)))
    assert rms(1.2, 1.8) < 0.1 * rms(0.2, 0.8)                                 # 120 Hz is cut inside the window
    assert abs(rms(2.2, 2.8) - rms(0.2, 0.8)) < 0.05 * rms(0.2, 0.8)           # and untouched outside it


def test_a_take_survives_an_engine_wording_change_but_not_a_script_change():
    from sheelshort import render
    with tempfile.TemporaryDirectory() as tmp:
        p = _scratch_project(tmp, {"name": "A", "role": "man", "look": "a man"})
        p.bible["locations"] = {"room": {"look": "a room"}}
        clip = {"id": "K1", "location": "room", "cuts": [{"type": "CU", "subjects": ["a"], "action": "He waits."}]}
        ref = Path(tmp) / "char_a.png"
        ref.write_bytes(b"png")
        files, meta = {"char_a.png": ref}, Path(tmp) / "K1_h3_t1.json"
        meta.write_text(json.dumps({"fingerprint": render.fingerprint("old wording", files, None, 3)}))
        assert render.current(p, meta, "E01", clip, "old wording", files, None, 3)
        assert json.loads(meta.read_text())["inputs"]                            # recorded on the way
        assert render.current(p, meta, "E01", clip, "new engine wording", files, None, 3)
        p.cfg["render"]["rerender_on_engine_change"] = True
        assert not render.current(p, meta, "E01", clip, "new engine wording", files, None, 3)
        p.cfg["render"]["rerender_on_engine_change"] = False
        clip["cuts"][0]["action"] = "He leaves."
        assert not render.current(p, meta, "E01", clip, "new engine wording", files, None, 3)
        ref.write_bytes(b"recast")
        clip["cuts"][0]["action"] = "He waits."
        assert not render.current(p, meta, "E01", clip, "new engine wording", files, None, 3)


def test_a_failed_job_is_retried_on_another_server_and_never_kills_the_run():
    from sheelshort import comfy
    with tempfile.TemporaryDirectory() as tmp:
        p = _scratch_project(tmp, {})
        p.cfg["comfy"]["servers"] = ["https://a", "https://b"]
        calls = []

        class C:
            def __init__(self, url):
                self.base_url = url
            def queue_info(self, quick=False):
                if self.base_url == "https://slow":
                    raise OSError("handshake timed out")
                return (0, 0) if self.base_url == "https://a" else (1, 0)
            def queue(self, api):
                calls.append(self.base_url)
                return "pid-" + self.base_url[-1]
            def wait(self, pid, timeout_s=0):
                if self.base_url == "https://a":
                    raise ConnectionResetError("Broken pipe")        # not a ComfyError: used to kill the batch
                return {"outputs": {}}
            def download_outputs(self, hist, out_dir, stem, kinds):
                f = out_dir / f"{stem}.png"
                f.write_bytes(b"x")
                return [f]
        clients = {}
        p.client = lambda url: clients.setdefault(url, C(url))
        p.object_info = lambda c: {}
        p.wf = lambda name: {}
        out = p.run_job("cast/char_x", lambda c: {}, p.d("assets"), "char_x")
        assert out and out[0].name == "char_x.png" and calls == ["https://a", "https://b"]
        assert p.state["jobs"]["cast/char_x"]["status"] == "succeeded"
        p.cfg["comfy"]["servers"].insert(0, "https://slow")       # a server that does not answer is skipped quickly
        import time as _t
        t0 = _t.time()
        assert p.pick().base_url in ("https://a", "https://b") and _t.time() - t0 < 5
        assert p._down_until["https://slow"] > _t.time()


def test_a_lost_prompt_is_noticed_instead_of_waited_on_for_an_hour():
    from sheelshort import comfy
    c = comfy.ComfyClient("https://x")
    c.history = lambda pid: None
    c.in_queue = lambda pid: False
    try:
        c.wait("p1", poll_s=0.01, timeout_s=5, lost_check_s=0.05)
        raise AssertionError("should have raised")
    except comfy.ComfyError as e:
        assert "no longer on the server" in str(e)
    c.in_queue = lambda pid: True                                   # still queued behind others: keep waiting
    try:
        c.wait("p1", poll_s=0.01, timeout_s=0.3, lost_check_s=0.05)
    except TimeoutError:
        pass


def test_episodes_sort_in_story_order_and_a_room_keeps_its_place_at_night():
    from sheelshort import wardrobe
    with tempfile.TemporaryDirectory() as tmp:
        p = _scratch_project(tmp, {"name": "A", "role": "man"})
        for e in ("E1", "E2", "E10", "E99", "E100", "E101", "E11"):
            (p.root / "episodes" / f"{e}.json").write_text(json.dumps({"id": e, "clips": []}))
        assert p.episodes() == ["E1", "E2", "E10", "E11", "E99", "E100", "E101"]
        p.bible["locations"] = {"flat": {"name": "flat", "name_zh": "样板间", "look": "a flat", "keep": "sofa",
                                         "light": "bright daylight through the window", "lights": {"night": "night; a warm floor lamp"}},
                                "flat_bath": {"name": "bath", "look": "a bath", "keep": "tub", "light": "day"}}
        ep = {"id": "E1", "clips": [{"id": "K1", "location": "flat", "time": "night", "cuts": [{"type": "MS", "subjects": ["a"]}]},
                                    {"id": "K2", "location": "flat", "cuts": [{"type": "MS", "subjects": ["a"]}]}]}
        (p.root / "episodes" / "E1.json").write_text(json.dumps(ep))
        k1, k2 = p.clips("E1")
        assert k1["location"] == "flat__night" and k2["location"] == "flat"
        assert p.locs["flat__night"]["light"] == "night; a warm floor lamp" and p.locs["flat__night"]["base"] == "flat"
        assert wardrobe.check(p.bible, ep) == []
        ep["clips"][1]["time"] = "dusk"
        assert any("no light 'dusk'" in e for e in wardrobe.check(p.bible, ep))
        (p.root / "inputs").mkdir()
        for n in ("loc_flat.png", "loc_flat_2.png", "loc_flat_bath.png", "loc_flat__night.png"):
            (p.root / "inputs" / n).write_bytes(b"x")
        assert [x.name for x in p.plates("flat")] == ["loc_flat.png", "loc_flat_2.png"]      # not the bathroom's plate
        from sheelshort import cast
        assert "Change only the lighting and time of day to: night; a warm floor lamp" in cast.relight_prompt(p.locs["flat__night"])


def test_a_private_room_holds_only_its_written_people():
    from sheelshort import clipqc
    chars = {"a": {"name": "A", "role": "man"}, "b": {"name": "B", "role": "woman"}}
    two = {"type": "MS", "subjects": ["a", "b"]}
    assert clipqc.judge_panel(chars, 1, two, {"people": 3}, False, False)                   # a stranger at home
    assert not clipqc.judge_panel(chars, 1, two, {"people": 3}, False, False, crowd=True)   # a diner in a restaurant
    assert clipqc.judge_panel(chars, 1, two, {"people": 5}, False, False, crowd=True)       # but not a crowd in a two-shot
    assert not clipqc.judge_panel(chars, 1, dict(two, extras="two waiters"), {"people": 4}, False, False)
    scene = json.loads((ROOT / "projects/quancun/bible.json").read_text())
    clip = _talk_clip()
    scene["clips"] = [clip]
    text = h3prompt.build(scene, clip, h3prompt.Refs(char_pictures={"dahua": ["a.png", "b.png"], "xiaokang": ["c.png", "d.png"]},
                                                     location_pictures=["l.png"]))
    assert "No one else is in the shot: only <Subject 1>." in text and "Background people" not in text


def test_a_run_keeps_trying_to_start_a_server_while_none_is_in_stock():
    fleet, api, p = _core_fleet()
    ses = fleet.Session(p, api=api, spawn=lambda root: None)
    tries = []
    ses._wake = lambda: tries.append(1)                           # AutoDL: no stock, nothing starts
    ses.ensure(); ses.ensure()
    assert len(tries) == 1                                        # not on every job
    ses.tried_at -= 200
    ses.ensure()
    assert len(tries) == 2                                        # but again after stock_retry_s
    p.cfg["comfy"]["servers"].append("https://up")
    ses.tried_at -= 200
    ses.ensure()
    assert len(tries) == 2                                        # and never once a server is up


def test_a_server_out_of_stock_is_asked_again_within_the_minute():
    from sheelshort import fleet

    class Api:
        def __init__(self):
            self.calls = []
        def power_on(self, u):
            self.calls.append(u)
            if len(self.calls) < 5:
                raise fleet.AutoDLError("power_on: InternalError 当前算力规格暂无库存, 请修改配置或稍等再试")
    api, said = Api(), []
    got = fleet.grab(api, ["pro-a", "pro-b"], {"grab_rounds": 6, "grab_gap_s": 0}, said.append, claim=False)
    assert got == "pro-a" and api.calls == ["pro-a", "pro-b", "pro-a", "pro-b", "pro-a"]   # third round wins
    api2 = Api()
    api2.power_on = lambda u: (_ for _ in ()).throw(fleet.AutoDLError("instance is locked"))
    assert fleet.grab(api2, ["pro-a"], {"grab_rounds": 6, "grab_gap_s": 5}, said.append, claim=False) is None  # no wait
    assert fleet.grab(api, [], {"grab_gap_s": 5}, said.append) is None


def test_only_one_run_works_on_a_project_at_a_time():
    import subprocess
    from sheelshort import cli
    with tempfile.TemporaryDirectory() as tmp:
        p = _scratch_project(tmp, {})
        cli.run_lock(p)
        r = subprocess.run([sys.executable, "-c", "import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
                            "from sheelshort import cli; from sheelshort.project import Project; cli.run_lock(Project(Path(sys.argv[2])))",
                            str(ROOT), str(p.root)], capture_output=True, text=True)
        assert r.returncode == 1 and "another run is already working" in r.stderr
        cli._RUN_LOCK.close()


def _swap_fixture(tmp, instances, probe):
    from sheelshort import fleet
    fleet.OWNED.unlink(missing_ok=True)
    api = _FakeAutoDL(instances)
    p = _scratch_project(tmp, {})
    p.cfg["fleet"].update({"enabled": True, "dry_run": False, "core": [i["uuid"] for i in instances], "min_servers": 1,
                           "max_servers": 1, "grab_gap_s": 0})
    p.cfg["comfy"]["servers"] = []
    p.cfg["edit"]["upscale"] = "rtx"
    old = (fleet.CAPS, fleet.probe_rtx)
    fleet.CAPS = Path(tmp) / "caps.json"
    fleet.probe_rtx = lambda p_, url: probe(url)
    return fleet, api, p, old


def _wait_for(cond, s=5.0):
    import time as _t
    t0 = _t.time()
    while not cond() and _t.time() - t0 < s:
        _t.sleep(0.05)
    return cond()


def test_an_old_driver_machine_is_replaced_first_then_powered_off_and_never_restarted():
    with tempfile.TemporaryDirectory() as tmp:
        fleet, api, p, old = _swap_fixture(tmp, [
            {"uuid": "pro-a", "name": "", "status": "running", "region_name": "北京B区", "machine_id": "m-old"},
            {"uuid": "pro-c", "name": "", "status": "shutdown", "region_name": "北京B区", "machine_id": "m-old2"},
            {"uuid": "pro-b", "name": "", "status": "shutdown", "region_name": "北京B区", "machine_id": "m-new"}],
            probe=lambda url: "pro-b" in url)
        fleet.note_machine("m-old2", False)                       # known old driver: never started again
        try:
            p.waker = fleet.Session(p, api=api, spawn=lambda root: None)
            p.waker.ensure()                                       # pro-a is already running: it joins, and is probed
            assert _wait_for(lambda: ("power_off", "pro-a") in api.calls)
            calls = api.calls
            assert calls.index(("power_on", "pro-b")) < calls.index(("power_off", "pro-a"))   # acquired first
            assert ("power_on", "pro-c") not in calls
            assert p.cfg["comfy"]["servers"] == ["https://pro-b.example:8443"]
            assert fleet.machine_known("m-old") is False and fleet.machine_known("m-new") is True
        finally:
            fleet.CAPS, fleet.probe_rtx = old


def test_a_replacement_that_fails_too_is_powered_off_and_the_window_is_bounded():
    with tempfile.TemporaryDirectory() as tmp:
        fleet, api, p, old = _swap_fixture(tmp, [
            {"uuid": "pro-a", "name": "", "status": "running", "region_name": "北京B区", "machine_id": "m-old"},
            {"uuid": "pro-b", "name": "", "status": "shutdown", "region_name": "北京B区", "machine_id": "m-also-old"}],
            probe=lambda url: False)
        p.cfg["fleet"]["replace_max_new"] = 1
        p.cfg["fleet"]["probe_rtx"] = False                        # vetted by hand below
        try:
            p.waker = fleet.Session(p, api=api, spawn=lambda root: None)
            p.waker.ensure()
            fleet.note_machine("m-old", False)
            assert p.waker.replace("pro-a", "https://pro-a.example:8443") is False
            assert ("power_on", "pro-b") in api.calls and ("power_off", "pro-b") in api.calls   # tried, failed, off
            assert ("power_off", "pro-a") not in api.calls          # no replacement: the old one keeps rendering
            assert fleet.machine_known("m-also-old") is False
        finally:
            fleet.CAPS, fleet.probe_rtx = old


def test_an_episode_is_held_back_rather_than_cut_from_a_soft_resize():
    import subprocess
    from sheelshort import fleet
    with tempfile.TemporaryDirectory() as tmp:
        p = _scratch_project(tmp, {})
        p.cfg["comfy"]["servers"] = ["https://old"]
        p.cfg["edit"]["upscale"] = "rtx"
        old = fleet.CAPS
        fleet.CAPS = Path(tmp) / "caps.json"
        fleet.note_machine("m-old", False)
        asked = []
        p.waker = type("W", (), {"machines": lambda self: {"https://old": "m-old"},
                                 "replace": lambda self: asked.append(1) or False})()
        take = p.d("clips", "E01") / "K1_h3_t1.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=gray:s=360x640:d=1", "-c:v", "libx264", str(take)], check=True)
        try:
            try:
                assemble.upscaled(p, "E01", take)
                raise AssertionError("should have held the episode back")
            except RuntimeError as e:
                assert "held back" in str(e) and asked == [1]
            p.cfg["edit"]["upscale_fallback"] = "resize"
            assert assemble.upscaled(p, "E01", take) == take                      # only when asked for
        finally:
            fleet.CAPS = old


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
