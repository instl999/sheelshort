"""Series lock: what a series looks and sounds like, frozen once the cast is approved, so it cannot drift mid-series.

`python -m sheelshort lock <project>` writes `series_lock.json`:
- the workflow files (hash) and the render and edit settings that decide the picture and sound;
- the LoRAs and model files the video graph loads, and the input schema of every node class in it;
- the approved cast and location references (hashes of the images and voices in assets/ and voices/).

`check` (and so `make`, `render`, `perform`, `cast`) refuses a project whose local part has changed, and every render
job compares the server it lands on with the node schemas and model files of the lock: a server whose image moved is
not used. A deliberate change (a re-cast, a new look after an A/B) is locked again with the same command; files
added later (a new character) are not drift, a locked file that changed is.
"""
from __future__ import annotations

import copy
import hashlib
import json
import time
from pathlib import Path

from . import flows
from .project import PKG, Project, load_json, save_json

RENDER_KEYS = ("dialogue", "voice_ref", "h3_megapixels", "h3_loras", "h3_steps", "h3_lowres_scale", "interp",
               "megapixels", "upscale", "lock_strength")
EDIT_KEYS = ("width", "height", "fps", "grade", "upscale", "loudness", "music_under_lu")
MODEL_EXTS = (".safetensors", ".pth", ".pt", ".pkl", ".gguf", ".ckpt", ".bin")


def path(p: Project) -> Path:
    return p.root / "series_lock.json"


def _sha(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()[:16]


def video_graph(p: Project) -> dict:
    """The H3 graph as this config renders it (workflow + LoRAs + sampler + interpolation)."""
    from . import render
    acted = p.cfg["render"].get("dialogue") == "acted"
    if acted:
        api = flows.h3_drive(copy.deepcopy(p.wf("h3_drive_model")), copy.deepcopy(p.wf("h3_drive")), "", 5,
                             ["x.png"], "x.wav", 0, "lock")
    else:
        api = flows.h3_reference(copy.deepcopy(p.wf("h3_ref")), "", 5, ["x.png"], [], 0, "lock")
    return render.tune(p, api)


def models(api: dict) -> list[str]:
    return sorted({v for n in api.values() for v in _strings(n["inputs"]) if v.endswith(MODEL_EXTS)})


def _strings(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for v in x.values():
            yield from _strings(v)


def _signature(node_info: dict) -> str:
    """A node's inputs by name and type: what a graph depends on. Option lists (the files on that server, in the
    order it lists them) and defaults differ from server to server of the same image and are left out; the files
    the graph loads are checked by name instead."""
    sig = {sec: {k: "COMBO" if isinstance(v[0], list) else v[0] for k, v in spec.items() if v}
           for sec, spec in node_info.get("input", {}).items() if isinstance(spec, dict)}
    return _sha(json.dumps(sig, sort_keys=True).encode())


def node_schemas(api: dict, object_info: dict) -> dict[str, str]:
    return {cls: _signature(object_info[cls]) for cls in sorted({n["class_type"] for n in api.values()})
            if cls in object_info}


def local_state(p: Project) -> dict:
    w = p.cfg["workflows"]
    api = video_graph(p)
    refs = {}
    for d in ("assets", "voices"):
        for f in sorted((p.root / d).glob("*")):
            if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".wav", ".flac", ".mp3"):
                refs[f"{d}/{f.name}"] = _sha(f.read_bytes())
    return {"workflows": {k: _sha((PKG / "workflows" / v).read_bytes()) + f" {v}" for k, v in sorted(w.items())
                          if (PKG / "workflows" / v).exists()},
            "render": {k: p.cfg["render"].get(k) for k in RENDER_KEYS},
            "edit": {k: p.cfg["edit"].get(k) for k in EDIT_KEYS},
            "loras": flows.loras(api), "models": models(api), "refs": refs}


def write(p: Project, object_info: dict | None = None, note: str = "") -> Path:
    data = {"locked_at": time.strftime("%Y-%m-%d %H:%M"), "note": note, **local_state(p)}
    if object_info:
        data["nodes"] = node_schemas(video_graph(p), object_info)
    old = load_json(path(p)) if path(p).exists() else None
    if old:
        data["history"] = old.get("history", []) + [{"locked_at": old["locked_at"], "note": old.get("note", ""),
                                                     "changed": diff(old, data)}]
    save_json(path(p), data)
    return path(p)


def diff(locked: dict, now: dict) -> list[str]:
    out = []
    for section in ("workflows", "render", "edit", "loras"):
        a, b = locked.get(section, {}), now.get(section, {})
        for k in sorted(set(a) | set(b)):
            if a.get(k) != b.get(k):
                out.append(f"{section}.{k}: {a.get(k)!r} -> {b.get(k)!r}")
    if locked.get("models") and set(locked["models"]) != set(now.get("models", [])):
        gone, new = set(locked["models"]) - set(now["models"]), set(now["models"]) - set(locked["models"])
        out.append(f"models: {', '.join(sorted(gone)) or '-'} -> {', '.join(sorted(new)) or '-'}")
    for f, h in locked.get("refs", {}).items():
        if f in now.get("refs", {}) and now["refs"][f] != h:
            out.append(f"{f}: changed since the lock")
        elif f not in now.get("refs", {}):
            out.append(f"{f}: missing (it was locked)")
    return out


def problems(p: Project) -> list[str]:
    """The local drift from the lock, for `check`; none when the series has no lock yet."""
    if not path(p).exists():
        return []
    return [f"series lock: {d} (deliberate? `python -m sheelshort lock {p.root.name} --note \"why\"`)"
            for d in diff(load_json(path(p)), local_state(p))]


def server_problems(p: Project, object_info: dict) -> list[str]:
    """A render server against the lock: node schemas changed or model files missing."""
    if not path(p).exists():
        return []
    locked = load_json(path(p))
    out = []
    for cls, h in locked.get("nodes", {}).items():
        if cls not in object_info:
            out.append(f"node {cls} is gone")
        elif _signature(object_info[cls]) != h:
            out.append(f"node {cls} has changed its inputs")
    have = {v for n in object_info.values() for spec in n.get("input", {}).get("required", {}).values()
            for v in (spec[0] if spec and isinstance(spec[0], list) else []) if isinstance(v, str)}
    have |= {v for n in object_info.values() for spec in n.get("input", {}).get("optional", {}).values()
             for v in (spec[0] if spec and isinstance(spec[0], list) else []) if isinstance(v, str)}
    out += [f"model {m} is missing" for m in locked.get("models", []) if m not in have and Path(m).name not in
            {Path(h).name for h in have}]
    return out
