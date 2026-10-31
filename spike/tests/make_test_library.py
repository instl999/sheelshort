"""Make a stand-in location photo library for the test scene (there are no real photos of the 归来 penthouse).

Six pictures in spike/inputs/locations/penthouse/: the generated plate plus five Qwen-edit variations of it,
including two decoys the selector must avoid: a daytime view (wrong time of day) and one with a waiter and a
neon sign (people + visible text). Every picture is marked synthetic in library.json.

    python spike/tests/make_test_library.py
"""
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import flows  # noqa: E402
import library  # noqa: E402
from spike import Spike  # noqa: E402

VIEWS = {
    "door_wide": "Show the same penthouse living room from a different camera angle: the camera now faces the dark wooden doorway "
                 "on the right-hand wall leading into shadow, with part of the charcoal velvet sofa and a glowing brass table lamp "
                 "in the foreground and the edge of the floor-to-ceiling window at the far left. Same night lighting, same materials. "
                 "The room is empty, no people, no text.",
    "lamp_detail": "A close detail shot inside the same room: one brass table lamp glowing warmly beside the arm of the charcoal "
                   "velvet sofa, the city skyline lights blurred into soft bokeh through the window behind. No people, no text.",
    "window_corner": "A medium view of one corner of the same room: the floor-to-ceiling windows meet the wall, the Lujiazui night "
                     "skyline with the Oriental Pearl Tower glitters outside, and the dark grey marble floor reflects the lights. "
                     "No people, no text.",
    "daytime_decoy": "The same room from the same camera angle in bright daytime: sunlight floods in, clear blue sky over the skyline, "
                     "lamps off. No people, no text.",
    "people_text_decoy": "The same room from the same camera angle. Add a waiter in a white jacket standing in the middle of the room "
                         "holding a silver tray, and a glowing red neon sign on the wall that reads SKY BAR.",
}


def main() -> None:
    s = Spike(HERE / "config.toml")
    dest = library.folder(s.inputs, "penthouse")
    dest.mkdir(parents=True, exist_ok=True)
    plate = s.require(s.out / "images" / "loc_penthouse.png", "run `images` first")
    if not (dest / "window_wide.png").exists():
        shutil.copy(plate, dest / "window_wide.png")
    edit = s.wf("edit")
    for name, prompt in VIEWS.items():
        if (dest / f"{name}.png").exists():
            continue
        key = f"testlib/{name}"
        files = s.run_job(key, lambda cl, prompt=prompt, key=key: flows.edit(edit, prompt, [s.put(cl, plate)], 1280, s.seed(key),
                                                                               f"sheelshort/testlib_{name}"),
                          s.out / "testlib", name, ("images",), 900)
        if files:
            shutil.copy(files[0], dest / f"{name}.png")
    lib = library.refresh(s.inputs, "penthouse")
    for e in lib["entries"].values():
        e["synthetic"] = True
        e["note"] = "test stand-in made by tests/make_test_library.py"
    library.save(s.inputs, "penthouse", lib)
    print(f"{len(lib['entries'])} pictures in {dest}")


if __name__ == "__main__":
    main()
