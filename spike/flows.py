"""Patch the servers' own workflows (converted to API format) for one job each.

Nodes are found by class type rather than id, so a re-exported workflow keeps working
as long as its structure is the same.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

H3_ASPECT_9_16 = "9:16 (Portrait Widescreen)"


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def ids(api: dict, cls: str) -> list[str]:
    return [k for k, n in api.items() if n["class_type"] == cls]


def one(api: dict, *classes: str) -> str:
    found = [k for c in classes for k in ids(api, c)]
    if len(found) != 1:
        raise ValueError(f"expected exactly one {'/'.join(classes)} node, found {len(found)}")
    return found[0]


def new_id(api: dict) -> str:
    return str(max(int(k.split(":")[0]) for k in api) + 1)


def drop(api: dict, cls: str) -> None:
    for k in ids(api, cls):
        del api[k]


def fix_known_issues(api: dict) -> dict:
    """Repair inputs the frontend mis-mapped after node updates (see comfy.validate)."""
    for k in ids(api, "MinimaxH3LatentUpscaler3D"):
        inp = api[k]["inputs"]
        if not isinstance(inp.get("enable_temporal_chunking"), bool):
            inp.update(enable_temporal_chunking=True, force_unload=True, device="cuda", precision="fp16")
    drop(api, "PlaySound|pysssss")
    return api


def t2i(tpl: dict, prompt: str, width: int, height: int, seed: int, prefix: str) -> dict:
    """Z-Image text-to-image (C01)."""
    api = fix_known_issues(copy.deepcopy(tpl))
    api[one(api, "CLIPTextEncode")]["inputs"]["text"] = prompt
    drop(api, "easy promptList")
    api[one(api, "EmptyLatentImage", "EmptySD3LatentImage")]["inputs"].update(width=width, height=height, batch_size=1)
    api[one(api, "KSampler")]["inputs"]["seed"] = seed
    api[one(api, "SaveImage")]["inputs"]["filename_prefix"] = prefix
    return api


def edit(tpl: dict, prompt: str, images: list[str], long_side: int, seed: int, prefix: str,
         aspect: str = "original", background: str = "#000000") -> dict:
    """Qwen-Image-Edit 2511 (Y01) with up to three input images.

    images[0] sets the canvas; `aspect` (e.g. "16:9") letterboxes it onto a wider canvas,
    which is how a single close-up becomes a front/side/back sheet."""
    if not 1 <= len(images) <= 3:
        raise ValueError("Qwen edit takes 1-3 images")
    api = fix_known_issues(copy.deepcopy(tpl))
    api[one(api, "LoadImage")]["inputs"]["image"] = images[0]
    ks = api[one(api, "KSampler")]["inputs"]

    def encoder(link):  # KSampler.<pos|neg> -> ReferenceLatent -> TextEncodeQwenImageEditPlus
        return api[link[0]]["inputs"]["conditioning"][0]

    pos, neg = encoder(ks["positive"]), encoder(ks["negative"])
    api[pos]["inputs"]["prompt"] = prompt
    for i, name in enumerate(images[1:], start=2):
        nid = new_id(api)
        api[nid] = {"class_type": "LoadImage", "inputs": {"image": name}}
        for enc in (pos, neg):
            api[enc]["inputs"][f"image{i}"] = [nid, 0]
    for k in ids(api, "INTConstant"):
        api[k]["inputs"]["value"] = long_side
    for k in ids(api, "LayerUtility: ImageScaleByAspectRatio V2"):
        api[k]["inputs"].update(aspect_ratio=aspect, fit="letterbox", background_color=background)
    ks["seed"] = seed
    api[one(api, "SaveImage")]["inputs"]["filename_prefix"] = prefix
    return api


def voice_design(tpl: dict, text: str, instruct: str, language: str, prefix: str) -> dict:
    """Qwen3-TTS VoiceDesign (N06 创造): a new voice from a written description."""
    api = fix_known_issues(copy.deepcopy(tpl))
    node = api[one(api, "TDQwen3TTSVoiceDesign")]["inputs"]
    for key in ("text", "instruct"):
        if isinstance(node.get(key), list):
            del api[node[key][0]]
    node.update(text=text, instruct=instruct, language=language)
    api[one(api, "SaveAudio")]["inputs"]["filename_prefix"] = prefix
    return api


def voice_clone(tpl: dict, ref_audio: str, text: str, language: str, prefix: str) -> dict:
    """Qwen3-TTS VoiceClone (N06 克隆): a user's recording speaking a new line."""
    api = fix_known_issues(copy.deepcopy(tpl))
    api[one(api, "LoadAudio")]["inputs"]["audio"] = ref_audio
    node = api[one(api, "TDQwen3TTSVoiceClone")]["inputs"]
    if isinstance(node.get("text"), list):
        del api[node["text"][0]]
    node.update(text=text, language=language)
    drop(api, "PreviewAny")
    api[one(api, "SaveAudio")]["inputs"]["filename_prefix"] = prefix
    return api


def h3_reference(tpl: dict, prompt: str, seconds: int, images: list[str], audios: list[str], seed: int, prefix: str,
                 aspect: str = H3_ASPECT_9_16, megapixels: float = 0.7, videos: list[str] = ()) -> dict:
    """MiniMax H3 reference-to-video (U22) through ZealmanH3PickReferenceMedia."""
    if len(images) > 9 or len(videos) > 3 or len(audios) > 3 or len(images) + len(videos) + len(audios) > 12:
        raise ValueError("H3 takes at most 9 images, 3 videos, 3 audio clips and 12 files in total")
    if audios and not images:
        raise ValueError("reference audio needs at least one reference image")
    api = fix_known_issues(copy.deepcopy(tpl))
    pick = one(api, "ZealmanH3PickReferenceMedia")
    inp = api[pick]["inputs"]
    for k in [k for k in inp if k.startswith(("ref_image_", "ref_video_", "ref_audio_"))] + ["media_files"]:
        inp.pop(k, None)
    inp.update(prompt=prompt, seconds=seconds, aspect_ratio=aspect, megapixels=megapixels)
    inp.update({f"ref_image_{i}": n for i, n in enumerate(images)})
    inp.update({f"ref_video_{i}": n for i, n in enumerate(videos)})
    inp.update({f"ref_audio_{i}": n for i, n in enumerate(audios)})
    # U22 as shipped never wires the pick node's video outputs (9-11) into the H3 node, so reference videos were
    # loaded and then silently dropped. Connect the ones in use.
    h3 = api[one(api, "MiniMaxH3ReferenceToVideo")]["inputs"]
    for i in range(3):
        h3.pop(f"ref_videos.ref_video_{i}", None)
    for i in range(len(videos)):
        h3[f"ref_videos.ref_video_{i}"] = [pick, 9 + i]
    for k in ids(api, "RandomNoise"):
        api[k]["inputs"]["noise_seed"] = seed
    for k in ids(api, "SelfLiftH3Sampler"):
        api[k]["inputs"]["seed"] = seed
    out = api[one(api, "VHS_VideoCombine")]["inputs"]
    out.update(filename_prefix=prefix, save_output=True)
    return api


def vlm_describe(image: str, prompt: str, model: str, mmproj: str, chat_handler: str, seed: int,
                 max_tokens: int = 700, max_size: int = 1024) -> dict:
    """Ask a local llama.cpp vision model (as in the server's 通用反推 workflow) about one image; text via ShowText."""
    return {
        "1": {"class_type": "LoadImage", "inputs": {"image": image}},
        "2": {"class_type": "llama_cpp_model_loader", "inputs": {
            "model": model, "mmproj": mmproj, "chat_handler": chat_handler, "n_ctx": 8192, "vram_limit": -1,
            "image_min_tokens": 0, "image_max_tokens": 0, "load_mtp": False}},
        "3": {"class_type": "llama_cpp_parameters", "inputs": {
            "max_tokens": max_tokens, "top_k": 30, "top_p": 0.9, "min_p": 0.05, "typical_p": 1.0, "temperature": 0.2,
            "repeat_penalty": 1.0, "frequency_penalty": 0.0, "present_penalty": 0.0, "mirostat_mode": 0,
            "mirostat_eta": 0.1, "mirostat_tau": 5.0, "state_uid": -1}},
        "4": {"class_type": "llama_cpp_instruct_adv", "inputs": {
            "llama_model": ["2", 0], "parameters": ["3", 0], "images": ["1", 0], "preset_prompt": "Empty - Nothing",
            "custom_prompt": prompt, "system_prompt": "", "inference_mode": "images", "max_frames": 24,
            "max_size": max_size, "seed": seed, "force_offload": False, "save_states": False}},
        "5": {"class_type": "ShowText|pysssss", "inputs": {"text": ["4", 0]}},
    }
