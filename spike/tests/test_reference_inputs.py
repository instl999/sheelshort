"""Unit checks for camera-move parsing, vision-tag parsing and location-picture selection.
Run: .venv/Scripts/python spike/tests/test_reference_inputs.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from blockout import camera_texts, parse_move  # noqa: E402
import qc  # noqa: E402
from library import assign, covers, parse_tags, select, words  # noqa: E402


def test_moves():
    assert parse_move({"camera": "Slow push in, with small amplitude, at slow speed."}) == {"kind": "push_in", "amount": "small"}
    assert parse_move({"camera": "Slow lateral track to the right, with small amplitude, at slow speed."})["kind"] == "truck_right"
    assert parse_move({"camera": "The camera pulls back to reveal the room."}) == {"kind": "pull_out", "amount": "normal"}
    assert parse_move({"camera": "Static camera with a barely perceptible handheld breathing."})["kind"] == "handheld"
    assert parse_move({"camera": "Static camera, focus held on his eyes."})["kind"] == "static"
    assert parse_move({"camera": "anything", "move": "orbit_left"}) == {"kind": "orbit_left", "amount": "small"}


def test_tags():
    t = parse_tags('Sure!\n```json\n{"framing": "Wide", "features": ["Window", "sofa"], "people": "false", '
                   '"visible_text": true, "time_of_day": "evening"}\n```')
    assert t["framing"] == "wide" and t["features"] == ["window", "sofa"]
    assert t["people"] is False and t["visible_text"] is True and t["time_of_day"] == "unknown"


def entry(dhash, **tags):
    return {"dhash": dhash, "tags": {"framing": "wide", "facing": "", "features": [], "landmarks": [],
                                     "time_of_day": "night", "people": False, "visible_text": False, **tags}}


def test_select():
    clip = {"cuts": [{"type": "MS", "composition": "She stands by the floor-to-ceiling window with the skyline behind her."},
                     {"type": "CU", "composition": "He waits in the dark doorway."}]}
    lib = {"entries": {
        "window.png": entry("0000000000000000", features=["window", "skyline"]),
        "window_copy.png": entry("0000000000000003", features=["window", "skyline"]),   # near-duplicate of window.png
        "door.png": entry("ffffffffffffffff", framing="medium", features=["doorway"]),
        "day.png": entry("0f0f0f0f0f0f0f0f", features=["window", "skyline", "doorway"], time_of_day="day"),
        "waiter.png": entry("f0f0f0f0f0f0f0f0", features=["window", "skyline", "doorway"], people=True),
    }}
    picks = [k for k, _ in select(lib, clip, "night", 3)]
    assert picks[:2] == ["window.png", "door.png"], picks
    assert not {"window_copy.png", "day.png", "waiter.png"} & set(picks), picks
    lib["entries"]["waiter.png"]["allow_people"] = True
    assert "waiter.png" in [k for k, _ in select(lib, clip, "night", 3)]
    lib["entries"]["day.png"]["pin"] = True
    lib["entries"]["window.png"]["veto"] = True
    picks = [k for k, _ in select(lib, clip, "night", 1)]
    assert picks != ["window.png"], "a vetoed photo is never picked"


def view(pitch=0.0, roll=0.0, lens=50, height=1.6, subjects=None, pieces=(), forward=0.0):
    return {"lens": lens, "height_m": height, "pitch_deg": pitch, "roll_deg": roll, "subjects": subjects or {},
            "set_pieces": list(pieces), "move": {"forward_m": forward, "right_m": 0, "up_m": 0, "pan_deg": 0, "tilt_deg": 0,
                                                 "roll_deg": 0, "subject_distance_m": 2.0}}


def subject(x, frame_h, facing=0, looks="camera"):
    return {"x": x, "y": 0.5, "depth_m": 2.0, "in_frame": True, "frame_height_m": frame_h, "facing_deg": facing, "looks_screen": looks}


def test_camera_text():
    clip = {"cuts": [{"type": "MS"}, {"type": "CU"}]}
    manifest = {"cuts": [
        {"cut": 1, "set_camera": True, "view": view(pitch=-30, lens=16, height=3.1, pieces=["sofa"], subjects={
            "a": subject(0.2, 9.0, 90, "right"), "b": subject(0.9, 9.0, 160, "left")})},
        {"cut": 2, "set_camera": False, "view": view(roll=20, lens=85, subjects={"a": subject(0.5, 0.5, 10, "right")},
                                                     pieces=["window"], forward=0.5)},
    ]}
    t = camera_texts(manifest, clip, "a room with a big window, a sofa and a lamp", {"sofa", "window", "lamp", "rug"})
    one, two = t[1], t[2]
    assert one["replace"] and one["size"] == "a wide shot" and one["move"] == "Static camera."
    assert "high angle from 3.1 m up" in one["text"] and "16 mm wide-angle" in one["text"]
    assert "{a} is in the left third, small in the frame, full figure, in profile looking toward screen right" in one["text"]
    assert "{b} is at the right edge of the frame" in one["text"] and "with the back to the camera" in one["text"]
    assert "Not in frame: the lamp, the window." in one["text"] and "rug" not in one["text"]   # only what the location mentions
    assert not two["replace"] and "Dutch tilt" in two["text"] and "an 85 mm telephoto" in two["text"]
    assert "{a} looks toward screen right" in two["text"]


def test_per_cut():
    lib = {"entries": {
        "window.png": entry("0000000000000000", features=["window", "skyline", "sofa"]),
        "door.png": entry("ffffffffffffffff", framing="medium", features=["doorway"]),
    }}
    wants = [("MS", words("window skyline"), set()), ("CU", words("doorway"), words("city skyline")), ("CU", words("rug"), set())]
    assert assign(lib, ["window.png", "door.png"], wants) == {1: "window.png", 2: "door.png"}
    assert covers(lib, words("doorway"), words("skyline")) and not covers(lib, words("sofa"), words("skyline"))
    picks = [k for k, _ in select(lib, {"cuts": [{"type": "CU"}]}, "night", 1, [("CU", words("sofa"), words("skyline"))])]
    assert picks == ["door.png"], "a photo showing what the camera must not see loses to a neutral one"


def test_qc():
    r = qc.parse('Here:\n{"people": [{"x": 0.8, "sex": "female"}, {"x": "0.2", "sex": "Man"}], "mannequins": "no"}')
    assert [p["sex"] for p in r["people"]] == ["woman", "man"] and r["mannequins"] is False
    view = {"subjects": {"a": {"x": 0.9, "depth_m": 1, "in_frame": True}, "b": {"x": 0.1, "depth_m": 1, "in_frame": True},
                         "c": {"x": -2.0, "depth_m": 1, "in_frame": False}}}
    must, at_most = qc.expected_people(view, {"a": "woman", "b": "man"})
    assert must == [("man", 0.1), ("woman", 0.9)] and at_most == 2
    assert qc.check(r, must, at_most) == []
    r["people"].append({"x": 0.5, "sex": "man", "clothing": ""})
    assert qc.check(r, must, at_most) == ["3 people seen, expected 2"]
    swapped = qc.parse('{"people": [{"x": 0.1, "sex": "woman"}, {"x": 0.9, "sex": "man"}]}')
    assert len(qc.check(swapped, must, at_most)) == 2


if __name__ == "__main__":
    test_per_cut()
    test_qc()
    test_camera_text()
    test_moves()
    test_tags()
    test_select()
    print("PASS")
