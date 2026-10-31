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


def drama3(voice_audio: str, transcript: str, text: str, seed: int, prefix: str, max_frames: int = 400) -> dict:
    """Fish Audio Drama 3 (local): one acted take of `text` in the voice of `voice_audio`.

    `text` may carry inline acting tags in brackets, e.g. "[声音发抖，快要哭出来] 求……求您。". The cast clip is encoded
    with its own transcript, which Drama 3 needs to clone the voice."""
    return {
        "1": {"class_type": "Drama3Loader", "inputs": {"model_name": "drama3_int8experts_bf16.safetensors",
                                                       "experts": "in RAM", "expert_cache_gb": 0.0,
                                                       "second_gpu": "none", "split_layer": 0}},
        "2": {"class_type": "Drama3CodecLoader", "inputs": {"codec_name": "drama3_codec.safetensors"}},
        "3": {"class_type": "LoadAudio", "inputs": {"audio": voice_audio}},
        "4": {"class_type": "Drama3EncodeVoice", "inputs": {"codec": ["2", 0], "audio": ["3", 0], "transcript": transcript}},
        "5": {"class_type": "Drama3Generate", "inputs": {
            "model": ["1", 0], "text": f"<|speaker:1|>{text}", "voices.voice1": ["4", 0], "seed": seed,
            "temperature": 1.0, "top_p": 0.9, "top_k": 0, "max_frames": max_frames, "split_long_text": False,
            "speakers": "one take per line"}},
        "6": {"class_type": "Drama3Decode", "inputs": {"codec": ["2", 0], "codes": ["5", 0], "shorten_long_pauses": True}},
        "7": {"class_type": "SaveAudio", "inputs": {"audio": ["6", 0], "filename_prefix": prefix}},
    }


# U22's tuned model chain (the look validated in M0) and U24's T8 sampling chain (audio conditioning with a drive track,
# dual-clock sampler, learned 2x latent upscale, two-pass refine, AV decode).
U22_MODEL_NODES = ("36", "37", "38", "53", "128", "192", "294")
U24_SAMPLING_NODES = ("8", "9", "10", "11", "12", "13", "15", "16", "17", "19", "20", "21", "27", "29", "30", "54", "95",
                     "96", "106")


def h3_drive(u22: dict, u24: dict, prompt: str, seconds: int, images: list[str], drive_audio: str, seed: int,
             prefix: str, aspect: str = H3_ASPECT_9_16, megapixels: float = 0.52, upscale: float = 2.0,
             lock_strength: float = 0.35) -> dict:
    """MiniMax H3 reference-to-video rendered around a fixed dialogue track (performance first).

    The drive track is locked into the audio stream of the AV latent (`audio_mode` lock_source) and presented to the
    model as <Audio 1>, so the picture is generated to the acted lines and the lips follow them. Pass 2 reuses the
    pass-1 audio (`second_pass_audio_source` first_pass): the explicit audio-lock route of the T8 nodes."""
    if not 1 <= len(images) <= 9:
        raise ValueError("H3 takes 1-9 reference images")
    api = {k: copy.deepcopy(u22[k]) for k in U22_MODEL_NODES}
    api |= {k: copy.deepcopy(u24[k]) for k in U24_SAMPLING_NODES}
    api = fix_known_issues(api)
    model = ["294", 0]
    api["8"]["inputs"]["model"] = model
    api["16"]["inputs"]["model"] = model
    for i, name in enumerate(images):
        api[f"70{i}"] = {"class_type": "LoadImage", "inputs": {"image": name}}
    api["901"] = {"class_type": "LoadAudio", "inputs": {"audio": drive_audio}}
    for k in ("95", "96"):
        ins = {a: b for a, b in api[k]["inputs"].items() if not a.startswith(("ref_audios.", "ref_images."))}
        ins.update(clip=["38", 0], video_vae=["36", 0], audio_vae=["37", 0], prompt=prompt, task_type="auto",
                   audio_mode="lock_source", audio_denoise_strength=lock_strength, add_source_as_reference=True,
                   prompt_primary_audio_ordinal=1, drive_audio=["901", 0], ref_image_size="max",
                   **{f"ref_images.ref_image_{i}": [f"70{i}", 0] for i in range(len(images))})
        api[k]["inputs"] = ins
    api["20"]["inputs"].update(video_vae=["36", 0], audio_vae=["37", 0])
    api["15"]["inputs"].update(second_pass_audio_source="first_pass", second_pass_audio_strength=0)
    api["29"]["inputs"].update(aspect_ratio=aspect, megapixels=megapixels)
    api["54"]["inputs"]["value"] = upscale
    api["27"]["inputs"]["value"] = seconds
    api["11"]["inputs"]["noise_seed"] = seed
    api["21"]["inputs"].update(filename_prefix=prefix, save_output=True)
    return api


def mmaudio(video: str, seconds: float, prompt: str, seed: int, prefix: str, steps: int = 50, cfg: float = 4.5) -> dict:
    """MMAudio video-to-audio: room tone, footsteps and foley synchronised to a rendered clip (no speech, no music).

    Used under the performed dialogue, whose render is otherwise silent between lines."""
    return {
        "1": {"class_type": "VHS_LoadVideo", "inputs": {"video": video, "force_rate": 24, "custom_width": 0, "custom_height": 0,
                                                       "frame_load_cap": 0, "skip_first_frames": 0, "select_every_nth": 1,
                                                       "format": "None"}},
        "2": {"class_type": "MMAudioModelLoader", "inputs": {"mmaudio_model": "mmaudio_large_44k_v2_fp16.safetensors",
                                                            "base_precision": "fp16"}},
        "3": {"class_type": "MMAudioFeatureUtilsLoader", "inputs": {
            "vae_model": "mmaudio_vae_44k_fp16.safetensors", "synchformer_model": "mmaudio_synchformer_fp16.safetensors",
            "clip_model": "apple_DFN5B-CLIP-ViT-H-14-384_fp16.safetensors", "mode": "44k", "precision": "fp16"}},
        "4": {"class_type": "MMAudioSampler", "inputs": {
            "mmaudio_model": ["2", 0], "feature_utils": ["3", 0], "images": ["1", 0], "duration": float(seconds),
            "steps": steps, "cfg": cfg, "seed": seed, "prompt": prompt,
            "negative_prompt": "speech, talking, voice, dialogue, whispering, singing, music, melody",
            "mask_away_clip": False, "force_offload": True}},
        "5": {"class_type": "SaveAudio", "inputs": {"audio": ["4", 0], "filename_prefix": prefix}},
    }


def music(tags: str, seconds: float, seed: int, prefix: str, steps: int = 50, cfg: float = 5.0) -> dict:
    """ACE-Step v1 instrumental bed (the ComfyUI reference graph): mood tags in, no lyrics."""
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "ace_step_v1_3.5b.safetensors"}},
        "2": {"class_type": "TextEncodeAceStepAudio", "inputs": {"clip": ["1", 1], "tags": tags, "lyrics": "[instrumental]",
                                                                "lyrics_strength": 0.99}},
        "3": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["2", 0]}},
        "4": {"class_type": "EmptyAceStepLatentAudio", "inputs": {"seconds": float(seconds), "batch_size": 1}},
        "5": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1", 0], "shift": 5.0}},
        "6": {"class_type": "KSampler", "inputs": {"model": ["5", 0], "positive": ["2", 0], "negative": ["3", 0],
                                                   "latent_image": ["4", 0], "seed": seed, "steps": steps, "cfg": cfg,
                                                   "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "7": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["6", 0], "vae": ["1", 2]}},
        "8": {"class_type": "SaveAudio", "inputs": {"audio": ["7", 0], "filename_prefix": prefix}},
    }
