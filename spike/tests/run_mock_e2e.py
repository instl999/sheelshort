"""End-to-end check of the spike against two mock ComfyUI servers (no network, no GPU).

Covers: doctor, location library indexing + vision tagging + selection, white-mesh blockout renders (real Blender,
tiny resolution; skipped if Blender is not installed), reference packs, props, keyframes, voices, all five H3
variants, dispatch across servers, resume-without-requeue, and the eval report.
Run: .venv/Scripts/python spike/tests/run_mock_e2e.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SPIKE = Path(__file__).resolve().parents[1]


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-y", "-v", "error", *args], check=True)


class Mock:
    """One fake ComfyUI server. Outputs depend on the workflow's output node type."""

    def __init__(self, media_dir: Path, classes: set[str]):
        self.media = media_dir
        self.classes = classes
        self.prompts: dict[str, dict] = {}
        self.uploads: list[str] = []
        self.lock = threading.Lock()

    def output_for(self, wf: dict) -> dict:
        types = {n["class_type"] for n in wf.values()}
        if "llama_cpp_instruct_adv" in types and "List every real person" in next(
                n["inputs"]["custom_prompt"] for n in wf.values() if n["class_type"] == "llama_cpp_instruct_adv"):
            return {"text": [json.dumps(self.people_check(next(n["inputs"]["image"] for n in wf.values() if n["class_type"] == "LoadImage")))]}
        if "llama_cpp_instruct_adv" in types:   # vision tagging: answer from the photo's file name
            name = next(n["inputs"]["image"] for n in wf.values() if n["class_type"] == "LoadImage")
            if m := re.search(r"view_(K\d+)_(\d+)", name):   # a view plate shows exactly what its camera measured
                manifest = json.loads((self.root / "out" / "blockout" / "manifest.json").read_text(encoding="utf-8"))
                shown = manifest["clips"][m[1]]["cuts"][int(m[2]) - 1]["view"]["set_pieces"]
                return {"text": [json.dumps({"framing": "medium", "facing": "", "features": shown, "landmarks": [],
                                             "time_of_day": "night", "people": False, "visible_text": False, "caption": ""})]}
            tags = {"framing": "detail" if "lamp" in name else "wide", "facing": "windows and skyline",
                    "features": ["window", "sofa"] + (["doorway"] if "door" in name else []), "landmarks": [],
                    "time_of_day": "day" if "day" in name else "night", "people": "people" in name,
                    "visible_text": "people" in name, "caption": f"mock caption for {name}"}
            return {"text": ["```json\n" + json.dumps(tags) + "\n```"]}
        if "VHS_VideoCombine" in types:
            secs = next(n["inputs"]["seconds"] for n in wf.values() if n["class_type"] == "ZealmanH3PickReferenceMedia")
            return {"gifs": [{"filename": f"clip_{secs}.mp4", "subfolder": "", "type": "output"}]}
        if "SaveAudio" in types:
            return {"audio": [{"filename": "voice.flac", "subfolder": "", "type": "output"}]}
        # a distinct image per request, so retries are not mistaken for the same picture
        return {"images": [{"filename": f"img_{zlib.crc32(json.dumps(wf, sort_keys=True).encode()):08x}.png", "subfolder": "", "type": "output"}]}

    def people_check(self, name: str) -> dict:
        """Vision check answers: view plates are empty rooms; a keyframe's first attempt shows an extra person
        (so it must be retried), later attempts show exactly the people the measured camera view expects."""
        import qc
        if "view_" in name:
            return {"people": [], "mannequins": False, "visible_text": False}
        clip = re.search(r"bkey_(K\d+)", name)[1]
        manifest = json.loads((self.root / "out" / "blockout" / "manifest.json").read_text(encoding="utf-8"))
        must, _ = qc.expected_people(manifest["clips"][clip]["cuts"][0]["view"], {"linxia": "woman", "luchen": "man"})
        people = [{"x": x, "sex": sex, "clothing": ""} for sex, x in must]
        if re.search(r"bkey_K\d+_0_", name):
            people.append({"x": 0.5, "sex": "man", "clothing": "black suit"})
        return {"people": people, "mannequins": False, "visible_text": False}

    def file(self, name: str) -> Path:
        p = self.media / name
        if not p.exists():
            if name.endswith(".png"):
                colour = f"0x{name[4:10]}" if name.startswith("img_") else "teal"
                ffmpeg("-f", "lavfi", "-i", f"color=c={colour}:s=720x1280", "-frames:v", "1", str(p))
            elif name.endswith(".flac"):
                ffmpeg("-f", "lavfi", "-i", "sine=f=180:d=9", str(p))
            else:
                secs = int(re.search(r"clip_(\d+)", name)[1])
                ffmpeg("-f", "lavfi", "-i", f"testsrc=s=432x768:r=24:d={secs}", "-f", "lavfi", "-i", f"sine=f=220:d={secs}",
                       "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(p))
        return p


def handler_for(mock: Mock):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path, _, query = self.path.partition("?")
            if path == "/system_stats":
                return self.send(200, {"system": {"comfyui_version": "mock"},
                                       "devices": [{"name": "cuda:0 Mock GPU", "vram_total": 32 * 2**30, "vram_free": 30 * 2**30}]})
            if path == "/queue":
                return self.send(200, {"queue_running": [], "queue_pending": []})
            if path == "/object_info":
                return self.send(200, {c: {"input": {}} for c in mock.classes})
            if m := re.fullmatch(r"/history/(\w+)", path):
                wf = mock.prompts.get(m[1])
                if wf is None:
                    return self.send(200, {})
                return self.send(200, {m[1]: {"status": {"status_str": "success", "completed": True}, "outputs": {"9": mock.output_for(wf)}}})
            if path == "/view":
                name = re.search(r"filename=([^&]+)", query)[1]
                return self.send(200, mock.file(name).read_bytes(), "application/octet-stream")
            self.send(404, {"error": path})

        def do_POST(self):
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/upload/image":
                name = re.search(rb'filename="([^"]+)"', raw)[1].decode()
                mock.uploads.append(name)
                return self.send(200, {"name": name, "subfolder": "sheelshort", "type": "input"})
            if self.path == "/prompt":
                wf = json.loads(raw)["prompt"]
                assert "{{" not in json.dumps(wf), "unfilled placeholder sent to ComfyUI"
                with mock.lock:
                    pid = f"p{len(mock.prompts) + 1}_{id(mock) % 1000}"
                    mock.prompts[pid] = wf
                return self.send(200, {"prompt_id": pid, "node_errors": {}})
            self.send(404, {"error": self.path})

    return H


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="sheelshort_mock_"))
    root = work / "spike"
    root.mkdir()
    shutil.copy(SPIKE / "scene.json", root)
    shutil.copytree(SPIKE / "workflows", root / "workflows")
    classes = {n["class_type"] for p in (root / "workflows").glob("*.api.json")
               for n in json.loads(p.read_text(encoding="utf-8")).values()}
    classes |= {"LoadImage", "llama_cpp_model_loader", "llama_cpp_parameters", "llama_cpp_instruct_adv", "ShowText|pysssss"}

    mocks, urls = [], []
    for i in range(2):
        (work / f"media{i}").mkdir()
        m = Mock(work / f"media{i}", classes)
        m.root = root
        srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(m))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        mocks.append(m)
        urls.append(f"http://127.0.0.1:{srv.server_port}")

    # a small location library: the selector must skip the daytime and people/text photos
    lib = root / "inputs" / "locations" / "penthouse"
    lib.mkdir(parents=True)
    for name, colour in [("window_wide", "navy"), ("door_wide", "maroon"), ("lamp_detail", "olive"),
                         ("day_view", "skyblue"), ("people_sign", "red")]:
        ffmpeg("-f", "lavfi", "-i", f"testsrc2=s=720x1280,hue=s=0,lutrgb=r=val:g=val:b=val,drawbox=c={colour}:t=fill:w=360:h=640",
               "-frames:v", "1", str(lib / f"{name}.png"))
    try:
        sys.path.insert(0, str(SPIKE))
        import blockout
        blender = blockout.find_blender()
    except FileNotFoundError:
        blender = None
    variants = "ABCDEFG" if blender else "ABC"
    if blender:
        (root / "inputs" / "blockout").mkdir()
        set_file = str(root / "inputs" / "blockout" / "penthouse.blend")
        subprocess.run([blender, "-b", "--factory-startup", "-noaudio", "-P", str(SPIKE / "blender" / "make_test_set.py"), "--",
                        set_file], check=True, capture_output=True)
        # hand-placed cameras on four cuts: exercises camera text that replaces the composition, and view plates
        subprocess.run([blender, "-b", set_file, "--factory-startup", "-noaudio", "-P", str(SPIKE / "blender" / "add_test_cams.py"),
                        "--", set_file], check=True, capture_output=True)
    else:
        scene = json.loads((root / "scene.json").read_text(encoding="utf-8"))
        scene["locations"]["penthouse"].pop("blockout", None)
        (root / "scene.json").write_text(json.dumps(scene, ensure_ascii=False), encoding="utf-8")
        print("Blender not found: blockout and variants D/E are not tested")

    cfg = (SPIKE / "config.example.toml").read_text(encoding="utf-8")
    cfg = re.sub(r"servers = \[[^\]]*\]", "servers = " + json.dumps(urls), cfg)
    cfg = re.sub(r"variants = \[[^\]]*\]", "variants = " + json.dumps(list(variants)), cfg)
    cfg = re.sub(r"still_res = \[[^\]]*\]", "still_res = [90, 160]", cfg)
    cfg = re.sub(r"animatic_res = \[[^\]]*\]", "animatic_res = [72, 128]", cfg)
    cfg = re.sub(r"animatic_samples = \d+", "animatic_samples = 1", cfg)
    (root / "config.toml").write_text(cfg, encoding="utf-8")
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}

    def run(*args: str, expect: int = 0) -> str:
        p = subprocess.run([sys.executable, str(SPIKE / "spike.py"), "--config", str(root / "config.toml"), *args],
                           capture_output=True, text=True, encoding="utf-8", env=env)
        print(f"$ spike {' '.join(args)}  -> exit {p.returncode}")
        print("  " + (p.stdout + p.stderr).strip().replace("\n", "\n  "))
        assert p.returncode == expect, f"expected exit {expect}"
        return p.stdout + p.stderr

    out = root / "out"
    queued = lambda: sum(len(m.prompts) for m in mocks)
    assert "all checks passed" in run("doctor")
    run("locations")
    tags = json.loads((lib / "library.json").read_text(encoding="utf-8"))["entries"]
    assert len(tags) == 5 and all(e["tags"] for e in tags.values()), "every library photo tagged"
    n_tag = queued()
    assert n_tag == 5
    run("locations")
    assert queued() == n_tag, "unchanged photos are not re-tagged"
    if blender:
        run("blockout")
        manifest = json.loads((out / "blockout" / "manifest.json").read_text(encoding="utf-8"))
        for cid, clip in manifest["clips"].items():
            assert subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", clip["animatic"]],
                                  capture_output=True, text=True).stdout.strip().startswith("12"), f"{cid} animatic is 12 s"
            assert all(Path(v["clay"]).exists() and Path(v["depth"]).exists() for v in clip["stills"].values())
    log = run("images")
    scene = json.loads((root / "scene.json").read_text(encoding="utf-8"))
    n_chars, n_clips = len(scene["characters"]), len(scene["clips"])
    n_views = log.count("needs a view plate")
    # close-ups + sheets, props, A keyframes; with a blockout, per clip a set plate and two keyframe attempts (the
    # first is rejected by the vision check), per uncovered cut a view plate, and a vision check per generated still.
    # The library replaces the generated location plate.
    expected_images = 2 * n_chars + len(scene.get("props", {})) + n_clips
    if blender:
        assert n_views == 3, f"the override cameras on K1/2, K2/1, K2/3 need view plates, got {n_views}"
        assert log.count("rejected") == n_clips and log.count("vision check passed") == n_clips + n_views
        assert log.count("content check passed") == n_views
        expected_images += 3 * n_clips + n_views + 2 * n_clips + 2 * n_views
        state = json.loads((out / "state.json").read_text(encoding="utf-8"))["jobs"]
        assert all(state[f"qc/bkey_{c}"]["passed"] and state[f"qc/bkey_{c}"]["attempts"] == 2 for c in ("K1", "K2"))
    assert queued() - n_tag == expected_images, f"expected {expected_images} image jobs, got {queued() - n_tag}"
    assert all(m.prompts for m in mocks), "jobs should be spread over both servers"
    run("voices")
    assert (out / "voices" / "linxia.wav").exists() and (out / "voices" / "luchen.wav").exists()

    before = queued()
    run("clips", "--variant", "B", "--dry-run")
    assert queued() == before and len(list((out / "requests").glob("clip_B_*.json"))) == n_clips
    for v in variants:
        log = run("clips", "--variant", v)
        assert "day_view" not in log and "people_sign" not in log, "decoy photos must never be picked"
    assert queued() == before + len(variants) * n_clips

    # Each variant's references as sent on the wire.
    h3_jobs = [wf for m in mocks for wf in m.prompts.values() if any(n["class_type"] == "VHS_VideoCombine" for n in wf.values())]
    picks = [next(n["inputs"] for n in wf.values() if n["class_type"] == "ZealmanH3PickReferenceMedia") for wf in h3_jobs]
    refs = lambda p, kind: [k for k in p if k.startswith(f"ref_{kind}_")]
    assert any(len(refs(p, "image")) == 1 and not refs(p, "audio") and "keyframe completion" in p["prompt"] for p in picks), "variant A"
    assert any(len(refs(p, "image")) >= 5 and not refs(p, "audio") for p in picks), "variant B"
    assert any(len(refs(p, "audio")) == 2 and "<Audio 2>" in p["prompt"] for p in picks), "variant C"
    assert all("No on-screen text" in p["prompt"] and "no background music" in p["prompt"].lower() for p in picks), "house rules"
    if blender:
        with_video = [p for p in picks if refs(p, "video")]
        assert len(with_video) == 2 * n_clips and all("<Video 1>" in p["prompt"] and "weak_reference" in p["prompt"] for p in with_video), "D/E"
        assert any("keyframe completion" in p["prompt"] and refs(p, "video") for p in picks), "variant E"
        h3_nodes = [next(n["inputs"] for n in wf.values() if n["class_type"] == "MiniMaxH3ReferenceToVideo") for wf in h3_jobs]
        assert sum("ref_videos.ref_video_0" in n for n in h3_nodes) == 2 * n_clips, "reference video must reach the H3 node"
        assert sum("Camera at eye level" in p["prompt"] and not refs(p, "video") for p in picks) == 2 * n_clips, "F/G camera text"
        fg = [p for p in picks if "Camera at eye level" in p["prompt"] and not refs(p, "video")]
        assert all("high angle from 3.1 m up" in p["prompt"] or "Dutch tilt" in p["prompt"] for p in fg), "set cameras replace the composition"
        assert all("The background in this shot is the part of" in p["prompt"] for p in fg), "per-shot background binding"
        assert any(r.startswith("sheelshort/view_") for p in fg for k, r in p.items() if k.startswith("ref_image_")), "view plates sent"

    # Resume: a job left "queued" in state must be polled again, never re-queued.
    state = json.loads((out / "state.json").read_text(encoding="utf-8"))
    state["jobs"]["clip/C/K1"]["status"] = "queued"
    (out / "state.json").write_text(json.dumps(state), encoding="utf-8")
    Path(state["jobs"]["clip/C/K1"]["files"][0]).unlink()
    n = queued()
    assert "resuming" in run("clips", "--variant", "C", "--clips", "K1")
    assert queued() == n and (out / "clips" / "C" / "K1.mp4").exists()

    run("eval")
    assert (out / "report.html").exists() and all((out / f"sequence_{v}.mp4").exists() for v in variants)
    if blender:
        assert all((out / f"compare_{v}_{cid}.mp4").exists() for v in "DEFG" for cid in ("K1", "K2"))
    print(f"\nPASS - artifacts in {work}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
