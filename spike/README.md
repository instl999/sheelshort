# M0 spike

This spike tests whether MiniMax H3, run through the **U22** workflow on our ComfyUI servers, keeps faces and voices consistent across the clips of one scene under the house shot rules. The rules are listed in [docs/DESIGN.md §0.2](../docs/DESIGN.md).

The test scene is [scene.json](scene.json): 归来, two 12 s clips, each four 3 s cuts, Mandarin dialogue plus one voice-over. Lin Xia and Lu Chen both speak in both clips, so each voice can be compared across clips.

| Variant | H3 references | Tests |
|---|---|---|
| **A** | one Qwen-edit keyframe as the first frame of [Shot 1] | Baseline: does identity hold when only an opening frame is given? |
| **B** | per character: close-up + three-view sheet; locked prop; location plate | Does a locked reference pack hold faces, costumes and props? |
| **C** | B + one reference voice per speaking character | Does reference audio stop voices drifting between clips? |
| **D** | C + the clip's white-mesh blockout animatic as `<Video 1>` | Does H3 follow the blockout's camera placement, framing and moves? **No**: with contradicting cameras it follows the text (`config_cams.toml`, `out_cams/`) |
| **E** | D + a [Shot 1] keyframe painted from the blockout's clay render | Does a first frame that matches the blockout lock the opening composition? |
| **F** | C + camera text measured from the blockout (angle, lens, shot size, who is where and facing which way, set pieces in and out of frame) | Does H3 follow the blockout camera when the prompt states it? |
| **G** | F + the [Shot 1] blockout keyframe | Does a matching first frame add to the camera text? |

## Pipeline

```bash
.venv/Scripts/python spike/spike.py doctor     # servers, GPUs, queues, Blender; every workflow validated against live node schemas
.venv/Scripts/python spike/spike.py locations  # index location photo libraries; tag new photos with the servers' vision model
.venv/Scripts/python spike/spike.py blockout   # Blender, headless: per-cut clay + depth stills, one animatic per clip
.venv/Scripts/python spike/spike.py images     # close-ups, three-view sheets, props, location plates, keyframes
.venv/Scripts/python spike/spike.py fix-image char_luchen "…instruction…"   # repair a locked asset with Qwen edit
.venv/Scripts/python spike/spike.py voices     # inputs/voice_<char>.* recordings, else Qwen3-TTS designed voices
.venv/Scripts/python spike/spike.py clips --variant B --dry-run   # writes out/prompts/*.txt and out/requests/*.json
.venv/Scripts/python spike/spike.py clips --variant B
.venv/Scripts/python spike/spike.py eval       # out/report.html + out/sequence_{A,B,C}.mp4
```

- **Resuming is safe.** Every queued job's server and prompt ID are saved before waiting, so re-running a command resumes the job instead of queuing it twice.
- **Dispatch.** Jobs go to whichever server in `config.toml` is least busy.

## Supplying your own assets (`spike/inputs/`)

| File | Replaces |
|---|---|
| `char_<id>.png` | generated close-up (e.g. an imported face photo) |
| `sheet_<id>.png` | generated three-view sheet |
| `prop_<id>.png` | generated prop reference |
| `locations/<loc>/**` (any images) | generated location plate. A photo library of the location, any size; see below |
| `loc_<id>.png`, `loc_<id>_2.jpg`, … | same as above (older flat form, still read) |
| `blockout/<loc>.blend` (or `.glb`/`.fbx`/`.obj`) | nothing: a white-mesh set, named in `scene.json` as `locations.<loc>.blockout`; see below |
| `key_<clip>.png` | generated keyframe (variant A) |
| `voice_<id>.wav/.mp3/.flac/.m4a` | designed voice. The first 7 s of speech is used, after trimming and loudness normalization. |

## Location photo libraries

- **Index and tag.** `locations` hashes every photo in `inputs/locations/<loc>/` and asks the servers' vision model (Qwen3.8-27B via the llama.cpp nodes of the `通用反推` workflow) for framing, what the camera faces, visible features, landmarks, time of day, people and visible text. About 10 s per photo once the model is loaded. Tags live in `inputs/locations/<loc>/library.json`; unchanged photos are never re-tagged.
- **Hand edits win.** Set `"pin": true` (always use), `"veto": true` (never use), `"allow_people"` / `"allow_text"`, or rewrite `tags` yourself (or let the agent do it); re-indexing keeps them.
- **Selection per clip.** Up to `library.max_per_clip` photos, within what is left of H3's 9-image budget. Photos with people or visible text are dropped (they leak into the video as extras or on-screen text), and so are photos at the wrong time of day (`locations.<loc>.time` or `clips[].time`). The rest are picked greedily to cover what each cut's background needs: a cut's `bg` hint, or else its composition text, matched against the tags, with wide photos preferred for medium shots and medium or detail photos for close-ups. Near-duplicates are skipped. The choice and the reason for it are logged.

## White-mesh blockouts (Blender)

- **What it does.** `blockout` runs Blender headless on the location's set file and renders, for every cut, a clay still and a depth map, plus one animatic per clip with the same cut timing as the H3 clip (`out/blockout/`, contact sheets in `out/blockout/<clip>_stills.jpg`).
- **Set conventions** (see `blender/render_shots.py`):
  - Characters and props are objects named by their scene id. Characters face local −Y.
  - `linxia@K2` or `linxia@K2.3` moves a character for one clip or one cut.
  - A camera named `CAM_K1_2` overrides the automatic camera for clip K1, cut 2.
- **Automatic cameras** follow the house shot grammar: MS (staged in depth when the two characters are far apart), CU, OTS behind the `over` character's shoulder, and VO as a close-up pushing in to an extreme close-up. The camera stays on one side of the line of action for the whole clip, so eyelines match, and it only stands where there is floor. Moves are read from each cut's camera text (push in, track left/right, handheld…) or an explicit `move`.
- **Uses.**
  - The animatic goes to H3 as `<Video 1>`, a `weak_reference` for camera, framing, cut timing and blocking (variants D and E). U22 as shipped drops reference videos; `flows.h3_reference` wires them in. Even wired, the camera-override test showed H3 does not follow the animatic's camera, so treat this as experimental.
  - The clay still becomes the [Shot 1] keyframe through two Qwen edits: first a set plate from the clay still plus the best library photo, then the cast painted over the mannequins (variant E).
  - The depth maps are ready for a depth ControlNet. They are not wired in yet.
- **Camera text (variants F, G).** Every cut's camera view is measured in Blender: height, tilt, roll, lens, shot size, each character's screen position, scale and facing, and which labelled set pieces are in or out of frame. It is written into the prompt (`blockout.camera_texts`). A camera placed by hand in the set (`CAM_<clip>_<cut>`) is the director's intent, so its text *replaces* the cut's written shot size, composition and move; an automatic camera only adds angle, lens and eyelines. Voice-over cuts keep the house push-in. Give set pieces a `label` custom property (e.g. "floor-to-ceiling window") for readable names; `blockout --measure-only` re-measures without rendering.
- **No GPU needed.** `blender.engine = "cycles"` renders on the CPU, about 2 min per 12 s clip at the default sizes. On a machine with a GPU, `"workbench"` is much faster.
- **Test set.** `blender/make_test_set.py` builds the penthouse used by `scene.json`, and `tests/make_test_library.py` makes six stand-in "photos" of it, including two decoys (daytime; a waiter with a neon sign).

## Prompts

- **Format.** Prompts follow MiniMax's official `h3-prompt-writing` skill. Install it for the agent with `python tools/install_h3_skill.py`.
- **Builder.** [h3prompt.py](h3prompt.py) builds the six-section Ref2VA prompt from the scene. It refuses scenes that break the shot rules.
- **Overrides.** A hand- or agent-written `spike/prompts/<clip>_<variant>.txt` replaces the built prompt for that clip.

## Tests (offline, no GPU)

```bash
.venv/Scripts/python spike/tests/run_mock_e2e.py    # whole pipeline against two mock ComfyUI servers (+ real Blender, tiny renders)
.venv/Scripts/python spike/tests/test_text.py       # CER/WER and line alignment
.venv/Scripts/python spike/tests/test_reference_inputs.py  # camera-move parsing, vision tags, location selection
.venv/Scripts/python spike/tests/run_metrics_sapi.py  # ASR + speaker similarity on real (Windows SAPI) speech
```
