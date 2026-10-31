"""Minimal ComfyUI HTTP client: upload inputs, validate and queue API-format workflows, collect outputs.

Workflows are API-format JSON (exported with "Save (API Format)", or converted from UI
format by the ComfyUI frontend's graphToPrompt). Templates may use "{{name}}" placeholders;
flows.py patches node inputs directly instead.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from pathlib import Path

import httpx

PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")
OUTPUT_KEYS = ("images", "gifs", "videos", "audio")


class ComfyError(RuntimeError):
    pass


def fill_template(template: dict, params: dict) -> dict:
    def walk(v):
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, str):
            whole = PLACEHOLDER.fullmatch(v)
            if whole:
                return params[whole.group(1)]
            return PLACEHOLDER.sub(lambda m: str(params[m.group(1)]), v)
        return v

    try:
        return walk(template)
    except KeyError as e:
        raise ComfyError(f"workflow template needs param {e}") from None


def missing_nodes(workflow: dict, object_info: dict) -> set[str]:
    return {n["class_type"] for n in workflow.values() if n["class_type"] not in object_info}


def validate(workflow: dict, object_info: dict) -> list[str]:
    """Check literal inputs against the server's node schemas.

    Catches what the server would reject, plus values the UI mis-mapped after a node
    changed its widgets (e.g. a device name landing in a BOOLEAN input)."""
    problems = []
    for nid, node in workflow.items():
        info = object_info.get(node["class_type"])
        if info is None:
            problems.append(f"#{nid} {node['class_type']}: node type not installed")
            continue
        spec = {**info.get("input", {}).get("required", {}), **info.get("input", {}).get("optional", {})}
        for name, value in node["inputs"].items():
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str):
                if value[0] not in workflow:
                    problems.append(f"#{nid} {node['class_type']}.{name}: links to missing node #{value[0]}")
                continue
            if name not in spec:
                continue  # dynamic sub-inputs such as "mode.scale" or "ref_images.ref_image_0"
            kind = spec[name][0]
            opts = spec[name][1] if len(spec[name]) > 1 and isinstance(spec[name][1], dict) else {}
            choices = kind if isinstance(kind, list) else (opts.get("options") if kind == "COMBO" else None)
            if any(opts.get(f) for f in ("image_upload", "audio_upload", "video_upload")):
                continue  # file pickers: names we upload just before queueing aren't in the cached list
            if choices is not None:
                if choices and isinstance(choices[0], str) and value not in choices:
                    problems.append(f"#{nid} {node['class_type']}.{name}: {value!r} not in options")
            elif kind == "BOOLEAN" and not isinstance(value, bool):
                problems.append(f"#{nid} {node['class_type']}.{name}: expected BOOLEAN, got {value!r}")
            elif kind in ("INT", "FLOAT") and (isinstance(value, bool) or not isinstance(value, (int, float))):
                problems.append(f"#{nid} {node['class_type']}.{name}: expected {kind}, got {value!r}")
        for name in info.get("input", {}).get("required", {}):
            if name not in node["inputs"] and spec[name][0] not in ("COMFY_DYNAMICCOMBO_V3",):
                problems.append(f"#{nid} {node['class_type']}: missing required input {name!r}")
    return problems


class ComfyClient:
    def __init__(self, base_url: str, headers: dict | None = None, timeout: float = 120):
        self.base_url = base_url.rstrip("/")
        self.http = httpx.Client(base_url=self.base_url, headers=headers or {}, timeout=timeout)
        self.client_id = uuid.uuid4().hex
        self._object_info: dict | None = None

    def _json(self, r: httpx.Response) -> dict:
        if r.status_code >= 400:
            raise ComfyError(f"HTTP {r.status_code} {r.request.url.path}: {r.text[:800]}")
        return r.json()

    def system_stats(self) -> dict:
        return self._json(self.http.get("/system_stats"))

    def queue_info(self) -> tuple[int, int]:
        q = self._json(self.http.get("/queue"))
        return len(q.get("queue_running", [])), len(q.get("queue_pending", []))

    def object_info(self, cache: Path | None = None) -> dict:
        if self._object_info is None:
            if cache and cache.exists() and time.time() - cache.stat().st_mtime < 6 * 3600:
                self._object_info = json.loads(cache.read_text(encoding="utf-8"))
            else:
                self._object_info = self._json(self.http.get("/object_info", timeout=300))
                if cache:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_text(json.dumps(self._object_info), encoding="utf-8")
        return self._object_info

    def upload(self, path: Path, subfolder: str = "sheelshort") -> str:
        """Upload any input file (image, audio, video) to the server's input folder.

        The server-side name carries a content hash ("sheelshort/char_linxia_1a2b3c4d.png"), so a changed file never
        reuses an old name: otherwise an identical request could hit ComfyUI's cache or a resumed job read new content."""
        data = path.read_bytes()
        name = f"{path.stem}_{hashlib.sha1(data).hexdigest()[:8]}{path.suffix}"
        r = self.http.post(
            "/upload/image",
            files={"image": (name, data, "application/octet-stream")},
            data={"overwrite": "true", "subfolder": subfolder, "type": "input"},
        )
        info = self._json(r)
        return f"{info['subfolder']}/{info['name']}" if info.get("subfolder") else info["name"]

    def queue(self, workflow: dict) -> str:
        r = self.http.post("/prompt", json={"prompt": workflow, "client_id": self.client_id})
        data = self._json(r)
        if data.get("node_errors"):
            raise ComfyError(f"node errors: {json.dumps(data['node_errors'], ensure_ascii=False)[:800]}")
        return data["prompt_id"]

    def history(self, prompt_id: str) -> dict | None:
        return self._json(self.http.get(f"/history/{prompt_id}")).get(prompt_id)

    def wait(self, prompt_id: str, poll_s: float = 3, timeout_s: float = 3600) -> dict:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            hist = self.history(prompt_id)
            if hist:
                status = hist.get("status", {})
                if status.get("status_str") == "error":
                    msgs = [m for m in status.get("messages", []) if m[0] == "execution_error"]
                    detail = msgs[0][1] if msgs else status.get("messages")
                    if isinstance(detail, dict):
                        detail = f"{detail.get('node_type')} #{detail.get('node_id')}: {detail.get('exception_message', '').strip()}"
                    raise ComfyError(f"prompt {prompt_id} failed: {str(detail)[:800]}")
                if status.get("completed", True) and hist.get("outputs") is not None:
                    return hist
            time.sleep(poll_s)
        raise TimeoutError(f"prompt {prompt_id} still running after {timeout_s}s")

    def download_outputs(self, hist: dict, out_dir: Path, stem: str, kinds: tuple[str, ...] = OUTPUT_KEYS) -> list[Path]:
        """Save a finished prompt's outputs. kind "text" saves text shown by display nodes (e.g. ShowText) to <stem>.txt."""
        if kinds == ("text",):
            texts = [t for node in hist["outputs"].values() for t in node.get("text", []) if isinstance(t, str)]
            if not texts:
                raise ComfyError("workflow showed no text output")
            out_dir.mkdir(parents=True, exist_ok=True)
            dest = out_dir / f"{stem}.txt"
            dest.write_text("\n".join(texts), encoding="utf-8")
            return [dest]
        refs = [f for node in hist["outputs"].values() for k in kinds for f in node.get(k, [])
                if isinstance(f, dict) and f.get("type") == "output"]
        if not refs:
            raise ComfyError(f"workflow saved no {'/'.join(kinds)} outputs")
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for i, ref in enumerate(refs):
            r = self.http.get("/view", params={k: ref.get(k, "") for k in ("filename", "subfolder", "type")}, timeout=600)
            r.raise_for_status()
            suffix = Path(ref["filename"]).suffix or ".bin"
            dest = out_dir / (f"{stem}{suffix}" if i == 0 else f"{stem}_{i}{suffix}")
            dest.write_bytes(r.content)
            paths.append(dest)
        return paths

    def run(self, workflow: dict, out_dir: Path, stem: str, kinds: tuple[str, ...] = OUTPUT_KEYS, timeout_s: float = 3600) -> list[Path]:
        return self.download_outputs(self.wait(self.queue(workflow), timeout_s=timeout_s), out_dir, stem, kinds)
