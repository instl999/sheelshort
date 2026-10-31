"""Minimal client for the MiniMax H3 video API (v2).

Request shape follows platform.minimax.io/docs/api-reference/video-generation-v2-create:
POST /v2/video_generation  -> {"task_id": ...}
GET  /v2/query/video_generation/{task_id} -> {"task": {"status": ..., "content": {"url": ...}}}
"""
from __future__ import annotations

import base64
import mimetypes
import time
from pathlib import Path
from typing import Callable

import httpx

TERMINAL = {"succeeded", "failed", "cancelled"}
REMOTE_PREFIXES = ("http://", "https://", "mm_file://", "data:")

MAX_REF_IMAGES = 9
MAX_REF_CLIPS = 3          # applies to reference video and reference audio separately
MAX_REF_FILES = 12
MAX_REF_TOTAL_SECONDS = 15


class H3Error(RuntimeError):
    pass


def _url_for(source: str | Path) -> str:
    s = str(source)
    if s.startswith(REMOTE_PREFIXES):
        return s
    path = Path(source)
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if path.suffix.lower() == ".wav":
        mime = "audio/wav"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


def _media(kind: str, source: str | Path, role: str | None) -> dict:
    item = {"type": f"{kind}_url", f"{kind}_url": {"url": _url_for(source)}}
    if role:
        item["role"] = role
    return item


def build_request(
    *,
    model: str,
    prompt: str,
    duration: int,
    resolution: str,
    ratio: str | None = None,
    first_frame: str | Path | None = None,
    last_frame: str | Path | None = None,
    ref_images: list = (),
    ref_videos: list = (),
    ref_audios: list = (),
    callback_url: str | None = None,
) -> dict:
    """Build a request body, enforcing the documented H3 constraints up front
    so a malformed shot fails locally instead of costing a round trip."""
    if int(duration) != duration or not 4 <= duration <= 15:
        raise ValueError(f"duration must be an integer 4-15, got {duration}")
    if len(prompt) > 7000:
        raise ValueError("prompt exceeds 7000 characters")
    has_frames = bool(first_frame or last_frame)
    has_refs = bool(ref_images or ref_videos or ref_audios)
    if has_frames and has_refs:
        raise ValueError("H3 cannot mix first/last-frame inputs with reference inputs")
    if ref_audios and not (ref_images or ref_videos):
        raise ValueError("reference audio requires at least one reference image or video")
    if len(ref_images) > MAX_REF_IMAGES:
        raise ValueError(f"at most {MAX_REF_IMAGES} reference images")
    if len(ref_videos) > MAX_REF_CLIPS or len(ref_audios) > MAX_REF_CLIPS:
        raise ValueError(f"at most {MAX_REF_CLIPS} reference videos and {MAX_REF_CLIPS} reference audio clips")
    if len(ref_images) + len(ref_videos) + len(ref_audios) > MAX_REF_FILES:
        raise ValueError(f"at most {MAX_REF_FILES} reference files in total")

    content: list[dict] = [{"type": "text", "text": prompt}]
    if first_frame:
        content.append(_media("image", first_frame, "first_frame"))
    if last_frame:
        content.append(_media("image", last_frame, "last_frame"))
    content += [_media("image", s, "reference_image") for s in ref_images]
    content += [_media("video", s, "reference_video") for s in ref_videos]
    content += [_media("audio", s, "reference_audio") for s in ref_audios]

    body = {"model": model, "content": content, "resolution": resolution, "duration": int(duration)}
    if has_frames:
        body["ratio"] = "adaptive"  # image-to-video always follows the input image
    elif has_refs:
        body["ratio"] = ratio or "adaptive"
    else:
        if not ratio or ratio == "adaptive":
            raise ValueError("text-to-video needs a concrete ratio such as 9:16")
        body["ratio"] = ratio
    if callback_url:
        body["callback_url"] = callback_url
    return body


def redact(body: dict, keep: int = 48) -> dict:
    """Copy of a request body with data URIs shortened, for logs and dry runs."""
    def walk(v):
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [walk(x) for x in v]
        if isinstance(v, str) and v.startswith("data:") and len(v) > keep:
            return f"{v[:keep]}...<{len(v)} chars>"
        return v
    return walk(body)


class H3Client:
    def __init__(self, base_url: str, api_key: str, timeout: float = 120):
        self.base_url = base_url.rstrip("/")
        self.http = httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    def _json(self, r: httpx.Response) -> dict:
        if r.status_code >= 400:
            try:
                err = r.json().get("error", {})
            except ValueError:
                err = {}
            raise H3Error(f"HTTP {r.status_code} {err.get('type', '')}: {err.get('message', r.text[:300])}")
        return r.json()

    def submit(self, body: dict) -> str:
        return self._json(self.http.post("/v2/video_generation", json=body))["task_id"]

    def query(self, task_id: str) -> dict:
        data = self._json(self.http.get(f"/v2/query/video_generation/{task_id}"))
        return data.get("task", data)

    def check_auth(self) -> str:
        """Query a task id that cannot exist: 401 means a bad key, anything else means we got past auth."""
        r = self.http.get("/v2/query/video_generation/0")
        if r.status_code == 401:
            raise H3Error("H3 API key rejected (401)")
        return f"auth ok (probe returned HTTP {r.status_code})"

    def wait(
        self,
        task_id: str,
        poll_s: float = 10,
        timeout_s: float = 1800,
        on_status: Callable[[dict], None] | None = None,
    ) -> dict:
        deadline = time.monotonic() + timeout_s
        last = None
        while True:
            task = self.query(task_id)
            status = task.get("status")
            if status != last and on_status:
                on_status(task)
            last = status
            if status in TERMINAL:
                if status != "succeeded":
                    raise H3Error(f"task {task_id} {status}: {task.get('error')}")
                return task
            if time.monotonic() > deadline:
                # Not an H3Error: the task may still finish, so callers keep it resumable.
                raise TimeoutError(f"task {task_id} still {status} after {timeout_s}s")
            time.sleep(poll_s)

    def download(self, url: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        # Result URLs are pre-signed, so don't send our API key to a third-party host.
        with httpx.stream("GET", url, timeout=300, follow_redirects=True) as r:
            r.raise_for_status()
            with tmp.open("wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        tmp.replace(dest)
        return dest
