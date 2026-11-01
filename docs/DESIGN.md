# sheelshort: Design Concept

**Status:** Draft v0.2 · 2026-10-30. §0 records decisions that supersede earlier sections.
**One-liner:** An agent-driven toolkit that turns a script into finished, vertical micro-drama episodes, using MiniMax H3 for video and ComfyUI for images. The output targets PineDrama-style series.

---

## 0. Decisions and requirements since v0.1

### 0.1 Confirmed setup
- **Market:** Chinese-language short drama first. **No automated upload.** The pipeline ends at the delivery package.
- **Compute:** the user's two rented ComfyUI servers. Each is an RTX 5090 (32 GB) running ComfyUI 0.42.1 with about 330 ready-made workflows. **H3 runs locally inside ComfyUI** (`MiniMaxH3ReferenceToVideo` and related nodes), so no MiniMax API key is needed. Jobs go to the least busy server. The MiniMax cloud API adapter (`spike/h3.py`) stays as an optional backend.
- **Default video workflow: U22** (`U22-YZ金鱼-MiniMax-H3-多参考生视频-优化版`). It is two-pass reference-to-video with a turbo LoRA and a 1.3× latent upscale, and takes references by filename through `ZealmanH3PickReferenceMedia` (≤9 images, ≤3 videos, ≤3 audio clips). Variants of it are kept for comparison: `u22_selflift` (终极篇, SelfLift sampler with fight/drama LoRA switches) and `u22_full`.
- **Helper workflows on the servers:**

| Job | Workflow |
|---|---|
| Close-ups, props, location plates | `C01-文生图-Zimage基础版` (Z-Image turbo + AWPortrait LoRA) |
| Three-view sheets, keyframes, image repairs | `Y01-图像修改千问2511` (Qwen-Image-Edit 2511, extended to 3 inputs) |
| Designed voices | `N06-Qwen3TTS音频创造` (voice from a written description) |
| Cloned voices | `N06-Qwen3TTS音频克隆` (a user recording speaking new lines) |

  `Y04` is deliberately not used because it loads an NSFW checkpoint.
- **Workflow handling:** the servers store UI-format workflows. The ComfyUI frontend's own `graphToPrompt()` converts them to API format; hand conversion is error-prone with Set/Get nodes, autogrow inputs and bypass switches. Every job is then **validated against the server's live node schemas** before it is queued. This caught a real bug in U22 as shipped: an updated node received the strings `"cuda"`/`"bf16"` in two BOOLEAN inputs.

### 0.2 House rules for every generation
These are enforced in `spike/h3prompt.py` (`check_rules` plus the prompt builder), not left to prompt wording alone:
- No on-screen text, subtitles or background music. `non_diegetic_music` is always "None".
- Each H3 clip (4–15 s) is a sequence of **3-second cuts** by default.
- Shot types by purpose:
  - A speaking character gets a **close-up**.
  - Two-person dialogue uses **over-the-shoulder** shots.
  - Positioning and movement get **medium shots**.
  - Actions and expressions get **close-ups**.
  - **Voice-over** starts as a close-up and pushes in slowly to an extreme close-up.
- Lines must fit their cut: about 4.5 Mandarin characters per second, with 0.3 s spare.
- Cinematic composition, with fine facial detail called out for every face in close-up (eyelashes, makeup, skin texture).
- A character's appearance never changes across clips. Generated characters must not resemble real celebrities.

### 0.3 Prompts: the official H3 prompt skill
- Prompts follow MiniMax's official [`h3-prompt-writing`](https://github.com/MiniMax-AI/MiniMax-H3/tree/main/skills/h3-prompt-writing) skill, pinned at commit `a107547`. Its full-reference (Ref2VA) format has six sections:
  - `subject_definitions`
  - `summary` (with a task-type prefix)
  - `retention_analysis`
  - `detailed_description`, written as `[Shot N] At MM:SS.mmm` cuts with `(Sx)` speaker IDs and `<d>[Chinese] …</d>` dialogue
  - `overall_soundscape`
  - `non_diegetic_music`
- The sections are written in English while dialogue stays in Mandarin. This also corrects the example prompt that ships in U22, which is written in Chinese.
- The repo has **no license file**. We therefore install the skill for the agent at the pinned commit rather than copying it into this repo.
- **Who writes what:** the agent writes rich per-cut text (composition, action, camera, sound, end state) into the scene file. The builder then assembles the labels, references and structure deterministically. An agent-written prompt in `prompts/<clip>_<variant>.txt` overrides the builder for any clip.
- Prompts are long by design, about 650 words of `detailed_description` per 12 s clip, because the user asked for maximum detail.

### 0.4 Locked reference packs (before any video is generated)

| Asset | Locks | How it is made |
|---|---|---|
| **Close-up** (per character) | face, makeup, identity anchors (beauty mark, scar) | Z-Image portrait, or the user's imported face photo. Repaired with Qwen edit when an anchor is wrong. |
| **Three-view sheet** (per character × costume) | hair, costume, shoes, proportions: front / side / back | Qwen edit from the approved close-up on a 16:9 letterboxed canvas, so the face carries over |
| **Prop reference** (per prop) | shape, material, fill level… | Z-Image product shot |
| **Location set** (per location) | layout, windows, furniture, skyline | Generated plate, or the user's own photo set (see §0.5) |
| **Voice reference** (per speaking character) | timbre and delivery | Qwen3-TTS voice design from a written description, or the user's recording (see §0.5) |

- **Text is subordinate to the image.** Each character's `hair`, `costume` and `keep` text is locked with the pack, and when the image and the text disagree, the text is corrected to match the approved image. The spike moved Lu Chen's scar from "right" to "left" eyebrow this way.
- **H3 reference budget per clip** (9 images, 3 audio clips, 12 files in total) is allocated in this order:
  1. Two images per on-screen character (close-up + sheet).
  2. Props that appear in the clip.
  3. As many location pictures as fit.
  4. One voice per speaking character, up to 3.

  Clips with more than 3 characters must be split.

### 0.5 Reference inputs (location libraries and blockouts built in the spike; faces and voices proposed)
- **Fixed scene references (location libraries): built** (`spike/library.py`, `spike.py locations`):
  - **Input:** any number of photos per location in `inputs/locations/<loc>/` (sub-folders allowed; the flat `inputs/loc_<id>_*` form still works).
  - **Indexing:** each photo is hashed and tagged once by the servers' own vision model: Qwen3.8-27B through the llama.cpp nodes of the `通用反推` workflow, about 10 s per photo. The tags are framing (wide/medium/detail), what the camera faces, features, landmarks, time of day, people and visible text. The index is `library.json` next to the photos. People can pin or veto a photo, allow people or text in it, or rewrite its tags, and re-indexing keeps those edits.
  - **Selection:** for each clip, up to 3 photos (configurable) within the remaining 9-image budget:
    - Dropped: photos with people or legible text, which would leak into the video as extras or on-screen text, and photos at the wrong time of day.
    - Picked: greedily, to cover what each cut's background needs (its `bg` hint or composition text, matched against the tags), preferring wide photos for medium shots and medium or detail photos for close-ups, skipping near-duplicates (dHash), and ranking real photos above synthetic ones.
    - All chosen photos together form the environment `<Subject N>`, as the official skill specifies. Each choice is logged with its reason.
  - **New angles (not built):** for angles the library lacks, the servers' `L02` (Gaussian-splat re-angle) and `B17` (360° panorama) workflows can synthesize views. They would enter the library with `synthetic: true`. A blockout set plate (below) is another source of on-axis views.
- **Blender white-mesh (blockout) files: built** (`spike/blender/render_shots.py`, `spike/blockout.py`, `spike.py blockout`). The agent never needs Blender interactively.
  - **Shot cameras:** a headless `blender -b set.blend -P render_shots.py -- job.json` step uses cameras named `CAM_<clip>_<cut>` from the file, or builds them from the storyboard:
    - Shot types: MS (staged in depth when the characters are far apart), CU, OTS behind the `over` character, and VO as a close-up pushing in to an extreme close-up.
    - The 180° rule is kept per clip, and cameras stand only where there is floor.
    - The move is parsed from the cut's camera text or set as an explicit `move`.
    - Blocking per clip or per cut uses `linxia@K2` / `linxia@K2.3` objects.
  - **Per-cut renders:** a clay still and a depth map (near = white) at 720×1280, plus one animatic per clip with the clip's exact cut timing (288×512, rendered at 8 fps and retimed to 24 fps).
  - **CPU rendering:** the build machine has no GPU (a VM), so the default engine is Cycles on the CPU, about 2 min per 12 s clip. Workbench is an option on GPU machines.
  - **Use 1, keyframes (built, variant E):** two Qwen edits. First, the clay still plus the best library photo give a set plate with the blockout's exact camera and layout. Second, the close-ups paint the cast over the mannequins. In the test, both keyframes kept the blockout's composition and the library's look.
  - **Use 2, H3 reference video (built, variants D/E; U22 needed rewiring; tested and found ineffective as camera control, see §0.6):** the animatic goes to H3 as `<Video 1>`, written as a `weak_reference` for camera, framing, cut timing and blocking. Its clay look is explicitly not reproduced. It uses 1 of H3's 3 reference-video slots.
  - **Use 3, depth control (later):** the depth maps are ready for `C02` (Z-Image ControlNet) keyframes, or for a depth control video through `MiniMaxH3FunControlNetApply`.
  - **Formats:** `.blend` is native. `.glb`/`.gltf`/`.fbx`/`.obj` are imported by the same script.
- **Custom faces:**
  - An imported photo becomes the character's close-up after face alignment, a crop to head and shoulders, and an upscale if needed. The three-view sheet is then generated from it, so all downstream steps are unchanged.
  - **Rights:** we cannot automatically prove a face is *not* a celebrity, so imported faces require the user's confirmation that they have the subject's consent. Generated faces go through a human review gate at casting.
- **Custom voices:**
  - An imported recording is cleaned (leading silence trimmed, loudness normalized, mono 44.1 kHz) and cut to the best 2–7 s of clean speech. It is then used directly as the H3 reference audio.
  - If the recording is noisy or in another language, the Qwen3-TTS clone workflow first re-voices a clean Mandarin reference line in that voice.
  - The same consent rule as faces applies.

### 0.6 M0 spike results (2026-10-30, U22 on both servers)
Scene: two 12 s clips, each four 3 s cuts, Mandarin dialogue plus one voice-over. Each clip was generated in three variants (`spike/out/report.html`, `sequence_{A,B,C}.mp4`).

| | A: keyframe only | B: reference pack | **C: pack + voice refs** |
|---|---|---|---|
| Shot grammar (MS → OTS → CU → VO push-in) | followed | followed | followed |
| Face consistency across cuts/clips (visual review) | drifts (Lin Xia's VO close-up reads as a different, older face; Lu Chen grew a nose ring) | consistent | consistent |
| Prop continuity (wine glass, 3 cuts) | n/a | held | held |
| Dialogue accuracy (Whisper CER) | 0% | 0% | 0% |
| Same-character voice similarity across clips (resemblyzer; same speaker ≈ 0.8+) | 0.59 / 0.66 | 0.66 / 0.77 | **0.86 / 0.85** |
| Render time per 12 s clip (RTX 5090, 832×1504, 24 fps) | ~6 min | ~7 min | ~7 min |

- **Blockout and location-library run (same scene, 2026-10-30):** the selector used the 6-photo stand-in library and never picked the two decoys. The blockout keyframes (E) kept the clay render's composition.
  - **Correction: the first D/E clips never used the animatic.** U22 as shipped loads reference videos in its pick node but never wires them into `MiniMaxH3ReferenceToVideo`, so H3 silently dropped them. A camera-override test caught this: four deliberately contradicting cameras gave clips byte-identical to the originals. The compositions in those clips came from the prompt text (and in E, from the keyframe). `flows.h3_reference` now wires `ref_videos`. The old clips are kept as `clips/D_unwired`, `clips/E_unwired`.
  - **Camera-override test, with the video wired (2026-10-30):**
    - **Setup:** a copy of the set (`penthouse_cams.blend`) puts a deliberately contradicting camera on four cuts: a high angle from the ceiling (K1/1), a low angle (K1/2), a reverse angle from the window with no skyline and the characters swapped left/right (K2/1), and a 25° Dutch tilt (K2/3). D was rerun with the same seeds, prompts and references as the original D, so the animatic was the only difference.
    - **Result: H3 ignored the animatic's camera.** All four override cuts came out nearly frame-for-frame like the original D (`spike/out_cams/cmp_K1.jpg`, `cmp_K2.jpg`). A `weak_reference` video does not override the prompt text and image references. It costs ~15–20% more render time (~500 s vs ~420 s per clip) for no visible effect.
    - **Consequences:**
      1. Drop the animatic as a camera control (variant D) from the production path.
      2. Camera intent from a blockout has to travel as **prompt text derived from the blockout camera** (height, tilt, lens, roll, screen side) and as **keyframes** (E: shown to set shot 1's composition).
      3. Hard geometric control means the **depth path**: per-cut depth stills to C02/Qwen keyframes for every cut, or a depth video through `MiniMaxH3FunControlNetApply`.
  - **Camera text instead (variants F and G, same override set and seeds, 2026-10-30):**
    - **How it works:** Blender measures each cut's camera (height, tilt, roll, lens, shot size, each character's screen position, scale and facing, and labelled set pieces in and out of frame) and `blockout.camera_texts` writes it into the prompt. For cameras placed in the set file, the measured text replaces the written composition. G adds the [Shot 1] keyframe painted from the override clay render. Comparison sheets are `spike/out_cams/cmpFG_K1.jpg` and `cmpFG_K2.jpg`.
    - **Results, override by override:**
      - **High angle (K1/1):** F gives a higher, wider shot looking down, though shallower than the blockout. G matches the blockout closely.
      - **Low angle (K1/2):** both F and G move to a lower angle with the ceiling in view, and from a close-up to a medium close-up.
      - **Reverse angle (K2/1):** both swap the characters left/right as specified, but ignore "not in frame: city skyline", because every location reference shows the skyline. In G, the Qwen keyframe also put two copies of Lu Chen in the shot, and H3 carried the extra man into the clip.
      - **Dutch tilt (K2/3):** background verticals lean, but there is no real 25° roll.
    - **Conclusion:** prompt text derived from the blockout steers H3 where the reference video did not. Shot size, angle direction and screen positions follow it, while extreme values are softened and negative instructions are ignored. A blockout keyframe is the strongest control for the opening shot, but it needs QC first.
    - **Next steps:**
      1. A vision check on keyframes: head count and who is where, matched against the measured view.
      2. Per-cut location pictures chosen to match the camera's view direction, so the references stop contradicting "not in frame".
      3. Depth control for angles the text can't force.
  - **Quality of those runs, unchanged by the bug:** dialogue CER is 0% except one homophone ASR slip (家/嫁) in E/K1, cross-clip voice similarity is 0.69–0.88, and render time is ~7–8 min per clip.
  - **Known issue:** Lu Chen's thin scar is still drawn as a long line in some close-ups (open issue 1 below).
- **Decision:** variant C, the locked reference pack plus reference voices, is the production default. A keyframe on its own does not hold identity.
- **Throughput:** a 60–90 s episode is 5–8 clips. That is roughly 35–55 GPU-minutes, or about 20–30 min wall-clock on the two servers.
- **Open issues found:**
  1. **Identity anchors that are only in the text** fail silently. The beauty mark is described but isn't visible in the approved close-up, so it rarely appears. Lu Chen's thin scar line gets lengthened into the cheek. Fix: a vision-model canon check that compares each close-up against its `keep` list before locking, plus `fix-image` repairs.
  2. **No background music** is requested in the prompt, but no one has confirmed it by listening yet. Add an automatic music detector to QC.
  3. **Face-identity metrics** need `insightface`, which needs C++ build tools on Windows. Run it on a server, or use a pure-Python alternative.

### 0.7 Performance-first dialogue (2026-10-31, the `sheelshort/` engine)
- **Problem.** In the predecessor project (drama-studio), the voices lacked emotion even when the faces showed it.
  Its `docs/DIALOGUE_PERFORMANCE.md` traces three prompt-side causes:
  1. delivery words in the voice definition;
  2. stripped punctuation;
  3. neutral voice references.

  It also traces one hard limit: H3's own speech ceiling. Its best production fix (acted takes as `<Audio n>`
  references) helped but stayed "better, not yet proper", because H3 still re-speaks every line.
- **Decision.** The dialogue is performed before any picture exists, and the picture is rendered around it:
  1. `perform` has Fish Audio Drama 3 act every line in the cast voice, from the episode's Chinese `act`
     direction and the `say` text (rhythm in the punctuation), 4 takes per line.
  2. Each take is scored on three counts: words (Whisper CER), identity (median pitch against the cast voice, with a
     wider band for intense emotions), and expressiveness (semitones of pitch motion between syllables, capped for calm
     lines). The best passing take is kept.
  3. Cuts are retimed to the acted lines, so **shot timing comes from the performance**. A clip that would exceed
     15 s is rejected, and the agent splits it.
  4. `render` uses U22's tuned model chain with U24's T8 audio conditioning: `lock_source`, the drive track presented
     as `<Audio 1>` (`partially_copy`), and a pass-2 audio lock (`flows.h3_drive`). Measured on the first clip: the
     render's audio matches the drive track with envelope correlation 0.999, and mouths move inside the line windows.
  5. Because the locked render is silent between lines, `render` adds an MMAudio foley/room-tone pass from the clip's
     soundscape. `assemble` mixes it under the clean dialogue.
  6. Subtitles come from the script at the exact placed line times.
- **A/B kept.** `--dialogue h3` renders the M0 variant C (H3 voices itself) into `*_h3_t*.mp4`. `evaluate` compares
  the two modes line by line.
- **Owner verdict (2026-10-31, 归来 E01): the H3-voiced master is better.** The default is therefore
  `render.dialogue = "h3"`. The performed track remains available as `"acted"`. The prompt-side lessons stay in both
  modes: `say` with verbatim punctuation inside `<d>`, timbre-only voice definitions, and a per-line `delivery`. The
  objective pitch-motion measure did not separate the modes either (acted median 2.2, H3 3.3, with H3 readings partly
  unreliable), so the ear decides.
- **Acted voice references (`voice_ref = "acted"`), owner verdict 2026-10-31.** In this mode H3 still voices the lines,
  but each speaker's `<Audio N>` is built from acted Drama 3 takes of their own lines. The test used the same seeds,
  so the reference was the only difference.
  - 全村抢 opening: the cast-voice version is better.
  - 归来: about the same, with a slight preference for the acted references.
  - The pitch measures rose on 归来 K1/K1b and were mixed on K2.
  - **Decision:** `voice_ref = "cast"` stays the default. Acted references cost an extra Drama 3 stage per episode for
    no consistent gain. They stay available for scenes that need it.

### 0.8 Throughput and autoscaling (2026-10-31)

**Where the time goes.** Measured on 全村抢 E02 and E03, 13 clips:
- **GPU generation:** about 5 min per clip (median).
- **Getting each result back:** about 110 s per clip (median) when the server has already started its next job. The
  file transfer itself takes 6–8 s; the rest is the server answering slowly while it starts that job.
- **Local processing on the Mac:** small. Whisper subtitle alignment takes about 29 s per clip on the CPU; the final
  encode takes about 30 s.

**Fixes:**
- Each server keeps one extra job queued, so the GPU never waits for a download.
- Subtitle alignment runs while the other clips render.
- The music bed renders alongside the clips.
- The final encode uses a faster preset.

A single episode now runs at about 3.8 min per clip on 2 servers, so it is GPU-bound.

**Autoscaling (`sheelshort/fleet.py`).** The servers are AutoDL.Art instances of the app `spcwDDc6XW` (v9.48), so
the app API can create, power on, power off and release identical servers. A test instance was running 40 s after the
create call; the owner observed ComfyUI ready under 3 minutes after each power-on. Findings from the test:
- A new instance may serve ComfyUI on 6008 instead of 6006, so both addresses are probed.
- Beijing B had no free 5090 when the test instance was created.
- The Northwest region's addresses are not reachable from this network.

**Scaling rule (time-based, owner-approved).** Let n be the servers working, W the remaining GPU work (clips left ×
`clip_minutes`) and S the startup cost of a new server, which is updated from each real scale-up. One more server
saves (W − n·S) / (n·(n+1)) minutes, and the fleet adds one only when that saving is at least `min_gain_minutes`.
The rule follows clip length and startup cost, and each additional server must save more than the last.

**Job hand-out.** A server holds at most `[comfy] in_flight` = 2 of the run's jobs, one running and one queued, and
the rest wait locally. Before this, the live E04 run queued all 7 clips onto the two existing servers at once, which
would have left nothing for an added server.

The owner's decision: **Beijing only.** The fleet adds servers while a render has clips waiting and powers off what
it added when idle and at the end of the run. It never touches other instances.

### 0.9 Quality pass 1 (2026-10-31): measured against the reference series
**The target**, from drama-studio's shot study of the original 全村抢 episode 1: 151 s, 73 shots, mean shot 2.07 s,
median 1.84 s, 2 shots over 5 s. Name cards appear on the intro close-ups. There is no end card: the cut lands on the
cliffhanger line.

**Where we were:** episodes of 61–89 s with a mean shot of 3.2–4.7 s.
- The adaptation compressed the script.
- The one-line-per-cut grammar cannot cut faster than a line lasts.

**What changed:**
- **Reaction cuts (`RX`).** The listener's close-up comes while the previous line continues off-screen. Test T01
  verified this on H3: each line runs across the cut, with the listener's lips closed, and every word is heard.
  The full-length E01 rewrite uses them: 56 cuts over 141 s, mean 2.5 s.
- **Clip QC with re-takes (`clipqc.py`).**
  - Each scripted line must be heard (Whisper CER).
  - One vision call per clip checks overlaid text, day/night against the location's `light`, the wrong person and
    malformed faces.
  - A failing clip is re-taken, and the best take wins.
  - Two QC false positives found on real renders were fixed. Printed text on props (the contract) was counted as
    on-screen text. The greedy line splitter shifted lines; lines are now located by the best-matching word run,
    which also fixed subtitle timing.
- **RTX Video Super Resolution** to 1080×1920 on the servers. It is a small, clean gain over a plain resize. One
  server cannot load NVIDIA VFX; the engine remembers that and routes upscales to the other.
- **Name cards** in the reference style: a vertical name in heavy serif with a thin role column, beside the head
  (OpenCV face detection), on the first solo close-up.
- **Clean version and delivery package** (`package`).
- **Pacing report** in each review page, against the reference numbers.


### 0.10 Quality pass 2 (2026-10-31): every episode at reference pacing
**Episodes.** E02–E04 were rewritten from the complete script episodes, as E01 was. They use reaction cuts, a second
villager (村妇), Gao Liang's aide, and a city street:

| Episode | Before | Now (planned) |
|---|---|---|
| E02 | 61 s, mean shot 4.7 s | 117 s, 47 shots, mean 2.5 s (measured on the master) |
| E03 | 68 s | 173 s, 64 shots, mean 2.7 s |
| E04 | 89 s, mean 4.7 s | 144 s, 49 shots, mean 2.9 s |

**Engine.**
- `check --fit` lengthens each dialogue cut too short for its line, and `check` prints each episode's planned pacing
  against the reference.
- Commands take several episodes (`make quancun E03,E04`). `render` puts all of them in one job queue, so the fleet's
  extra servers stay busy until the last clip instead of powering off between episodes.

**Two defects found by looking at the frames, not the QC verdicts:**
- **Voice-over shots rendered an empty set.** The prompt said the speaker spoke "off-screen, as inner voice-over",
  and H3 took that literally: E02 C3 shows three seconds of an empty signing table before Xiaokang appears. The
  prompt now keeps the speaker in frame with lips closed while the inner voice is heard.
- **Clip QC could not see it.** It sampled only the middle of each planned cut, and the empty shot fell between two
  of them. It now also samples every shot H3 actually cut (ffmpeg scene detection) that contains no planned
  midpoint, and judges it against the cut it plays in. A character shot with nobody in it fails. QC frames are
  kept per episode: E03 and E04 both have a `C3_h3_t1`, and in one queue their strips overwrote each other.

**Three more defects, found in the logs of the pooled E03/E04 run:**
- **The fleet's servers got no work.** On these boots ComfyUI answered on port 6006. The probe took the app's other
  port, which answers `/system_stats` with an HTML page and status 200, so two servers sat in the pool for two
  hours while all 26 clips went to the original pair. An unreachable server also counted as "busy", so neither was
  retired. Now:
  - a server joins the pool only when `/system_stats` returns ComfyUI's JSON with its GPUs;
  - a pool server that stops answering for 3 minutes has its address looked up again.
- **Takes from an older version of the episode.** Only take 1 was checked against the current prompt. E03's C3, C6
  and C8 picked a take 2 left over from the first E03, which shows a different scene. Takes whose fingerprint
  does not match the clip as it is now are never checked, picked or counted, and re-takes replace them.
- **QC skipped one-shot clips.** A one-shot clip (a voice-over) has a one-panel strip, and `xstack` needs at least
  two inputs.

**QC false positives, from looking at what it flagged:**
- **"Daylight in a night scene" on lamp-lit rooms.** Qwen3-VL answers "is it dark?" with no for a warm-lit room.
  It is now asked what lights the scene: daylight, night or lamp.
- **The business card read as a caption** on every take, even when the prompt excluded printed props. A cut that
  holds a prop with printed text is no longer judged on text.
- **A vision check was lost to the fleet.** It was handed to a fleet server one second before the fleet powered
  that server off. Retirement now happens under the job-assignment lock, and a failed vision call is retried on
  another server.

**Result:** every take of all four episodes judged again: 51 of 57 checks pass. E02 C3's empty insert was caught and
re-taken; take 2 is clean. The other flags were the false positives above.

### 0.11 Cinema pass (2026-10-31): realism and editing
**The owner's review of the delivered episodes:** the picture lacks realism, and the edit has little cinematic language,
with cuts that feel jarring and nonsensical. One frame per shot of E02 showed why.

**Editing.**
- **Jump cuts in every episode (8–9 each).** The house pattern *A speaks → reaction close-up of B → B answers in
  close-up* cuts B to B at the same size. E02 C10 also had two close-ups of Erdaye in a row.
- **No geography.** E02 had 47 shots: 44 single close-ups, 1 over-the-shoulder shot and no wide shot. The viewer never
  sees who stands where.
- **No screen direction.** Nothing said where anyone looks, so singles stared into the lens.
- **No camera language.** 93% of cuts were "static camera".

**Look.**
- The style asked for a 35mm lens and "crisp detail". The village light was "bright late-morning sunlight from a
  clear sky, soft shadows": flat light and a wide lens, the phone look.
- The location plate (a reference picture in every clip there) was generated **without the location's light at all**. It
  was a flat phone snapshot with a hazy sky and a gate that read as a Japanese torii, and H3 copied it.

**Engine changes.**
- `check` refuses **jump cuts**: adjacent cuts with the same people at the same size, also across a clip boundary in
  the same location. `check --fit` repairs the common case: a repeated close-up that carries a line becomes the reverse
  angle, an over-the-shoulder shot past the person addressed.
- **Screen direction.** Each character gets a side of the line of action per location: the people who talk to each
  other most go on opposite sides (greedy max-cut), or the episode fixes it with `blocking`. Every single is framed on
  its side and looks across the line at the person addressed (`to`, or the nearest other speaker), never into the
  lens. Over-the-shoulder shots put the shoulder on its own side, and two-shots place both people.
- **`WS`**, a wide shot for geography (no dialogue).
- **Lens per shot size** (85mm with shallow focus for faces, 50mm medium, 35mm wide) and a cinema paragraph in every
  prompt: directional key and rim light, real shadows, film colour, real skin, restrained acting, background extras who
  never look into the lens.
- **Plates** are lit by the location's `light` and shot as film stills.
- **Craft notes** in `check` (advice, not errors): over 70% single close-ups, over 60% static camera, a location without
  a wide, medium or over-the-shoulder shot, flat light words in a location, and wide-lens or "crisp" words in the style.
- **Edit.**
  - A flash shot under 0.6 s at either end of a take is trimmed (E02 had a 0.3 s one at a clip boundary), never
    cutting into a line.
  - A finishing grade (`edit.grade = "film"`): slightly lower saturation, a soft vignette and fine luma grain.

**E02 rewritten under these rules.** It keeps the same lines, and its coverage is:
- 7 over-the-shoulder shots, 2 wide shots, 2 medium shots and 3 inserts;
- a motivated move on most cuts;
- low golden backlight on a relit plate.

The fresh render's 10 clips all passed clip QC on the first take. It runs 122.5 s with 50 detected shots, a mean of
2.45 s. Detection over-counts slightly: a contract waved across the lens reads as a cut. The before/after sheet is
`projects/quancun/edit/E02/compare_before_after.jpg`. The look and the coverage are a clear step up.

**The fresh-agent test** on a new script (《雨夜外卖》, written only from the old skill) produced the same defects:
12 jump cuts and around 80% static camera. So they came from the workflow, not from one adaptation. `--fit` repairs 7
of the 12; the other 5 are one-person phone-call sequences, which need a rewrite.

### 0.12 Every server on demand (2026-10-31)

Before this, the two servers in `[comfy] servers` ran around the clock, pay-as-you-go, and only the fleet's extra
servers were powered on and off. The owner asked for all servers to follow the dynamic rule. Now:

- **Core servers.** The owner's two instances are listed by uuid in `[fleet] core`, and `[comfy] servers` is empty.
  Their address is looked up from the AutoDL API on every wake, because ComfyUI moves between ports 6006 and 6008.
- **Wake on the first GPU job.** A command starts nothing until a job needs a GPU (`Project.pick`, or the pools in
  `cast`, `perform` and `render`), so `check`, or an `assemble` whose upscales are cached, never powers anything on.
  On the first job, every instance of ours that is already running joins the pool (warm, and billed anyway). If that
  is fewer than `min_servers` (1), stopped core servers are powered on first, then stopped `sheelshort-bj-N` ones, and
  only then is a new instance created.
- **Scaling while rendering.** The scaling rule in §0.8 is unchanged and still adds servers while clips wait. It
  counts toward `max_servers` = 4 in all, the same 2 + 2 as before, and powers on a stopped core server before a fleet
  one. A server the fleet started is powered off when it is idle and when the render ends.
- **Off when idle.** When a command ends, it lets go of its servers. A background watcher, `fleet <project> idle`,
  powers a server off once no live sheelshort process holds it and its ComfyUI queue has been empty for
  `idle_off_minutes` (10). Work queued from the ComfyUI page restarts the wait. Back-to-back commands, such as a 20 s
  test followed by `make`, find the server still warm.
- **Shared between runs.** `.cache/fleet_owned.json` records which processes hold which server, under a file lock.
  Two runs never power on the same stopped server, and a run never powers off a server another live run uses. A run
  killed without cleanup counts as gone, and its servers go idle.

Cost: a cold command now waits for a boot, which took 67–68 s to reach the pool on this app plus H3's first model
load. In exchange, the two servers stop billing about 10 minutes after the last run ends.

### 0.13 Review pass (2026-10-31): speed, shot design, estimated times

A review of the E01/E03/E04 recut run (39 clips, 4 servers) and the E01 master.

**Speed.**
- **Upscales now start during re-takes.** E03 C10 was re-taken after the check phase, and for 11 minutes every server
  but one sat idle: the extra servers powered off, and no clip could be upscaled until all picks were final. `render`
  now upscales the chosen take of every settled clip (one with a clean take, or out of takes) right after the check
  phase, on all servers, while the re-takes render. The re-taken clips are upscaled when their pick is made.
  `assemble` finds the upscales done.
- **The network is often the bottleneck, not the GPU.** An RTX upscale runs 13–22 s on the server. While the Mac's
  route to seetacloud.com went through the VPN at 0.2 Mbit/s, each upscale took 400–670 s. A direct route measured
  38.5 Mbit/s and 27–43 s per clip. The per-job log now prints the server's run time next to the wall time
  (`done in 324s (ran 15s on the server)`), so a slow network is visible at once.

**Shot design.**
- **One person alone could not be repaired.** In the first new-script test, 5 of the 12 jump cuts were one person on
  the phone. A cut now has an `angle` (front, three-quarter, profile, high, low), and the jump rule treats a
  different angle as a real cut, which is the 30-degree rule. `fix_jump_cuts` moves the camera to profile, or to
  three-quarter if the shot was already in profile. On that test project, all 12 jump cuts are repaired.
- **Long runs of faces.** E01's master holds about 50 s of close-ups and over-the-shoulder shots in a row at the gate
  (planned: 69 s), and the crowd and the table drop out of view. `check` now notes any run longer than 25 s at one
  location. `--fit` turns the silent reaction close-up nearest the middle of the run into a medium shot of the
  listener among the others, but only when the clip still passes every rule. Runs made only of dialogue cuts are left
  as a note: the writer adds a WS or MS beat.

**Clip QC.** E03 C10 was re-taken for "on-screen text", and the second take was flagged the same way. The shot was
an ECU insert of a phone screen, and the vision model reads the screen's UI as a caption. ECU inserts whose
composition shows something that carries writing (a screen, a contract, a sign, a card) are no longer judged on
text. Close-ups and wider shots still are.

**Image quality.** The cinema-pass look holds across E01: directional golden light, rim light, shallow depth of
field, real skin, and eyelines off the lens. No change without an A/B. The next candidate is `h3_megapixels`
0.7 → 0.85, about 25% more GPU time per clip.

**Estimated times (`sheelshort/eta.py`).**
- **Per job.** Every GPU job logs when it should be back, counting the job ahead of it on the same server.
- **Per phase.** Render, check, re-take and upscale each log `done/total` with an end time: the jobs still running
  finish at their own estimate, and the rest share the servers.
- **Status from another terminal.** `python -m sheelshort status <project>` reads `progress.json` and `state.json`
  and lists each phase and each job in flight with its end time, marking a job that is running late.
- **Learning.** The model learns from every finished job: the server's own execution time from the ComfyUI history,
  plus the download, measured per second of video for a render and per job otherwise. The last 30 per kind are kept
  in `.cache/job_times.json`, shared by all projects.

### 0.14 Five reference hits (2026-10-31): production logic

The owner picked five current hits: 王妈在顾家当保姆后全家只会搞笑了 (live-action comedy), 持械入宋 S10
(photoreal AI, period), 备用联系人 (photoreal AI, urban romance), 和离后我带女儿横扫魔域 (3D xianxia) and
反派拿捏 S3 (stylised AI romance). All 100 episodes (20 per series) were measured with ffmpeg: scene-change cuts,
loudness and format. Episodes 1, 5 and 12 were studied frame by frame, including their opening and closing seconds.

| Series | Episode (median) | Mean shot | Median shot | Shots > 5 s | Shots < 1 s | First 10 s | LUFS |
|---|---|---|---|---|---|---|---|
| 王妈在顾家当保姆 | 84 s | 2.10 s | 1.64 s | 5% | 18% | 5.5 shots | −10.5 |
| 持械入宋 S10 | 88 s | 2.89 s | 2.84 s | 4% | 1% | 4 | −10.3 |
| 备用联系人 | 169 s | 3.27 s | 3.02 s | 15% | 2% | 4 | −15.0 |
| 和离后…横扫魔域 | 70 s | 2.53 s | 2.21 s | 6% | 14% | 4.5 | −9.8 |
| 反派拿捏 S3 | 64 s | 2.51 s | 2.29 s | 4% | 5% | 4 | −13.4 |
| 全村抢 recut (ours) | 118–176 s | 2.45–3.01 s | 2.4–2.5 s | 2–10% | 0% | – | −14.8 |

**What we already match.**
- Shot pacing: a mean of 2.1–3.3 s.
- The share of long shots.
- Vertical name cards with a role line.
- Dirty singles past a foreground shoulder, which is our OTS.
- Mid-lower white subtitles.

**Where we differed, and the changes.**
- **Episode length.** Four of the five run a median 64–88 s; ours ran 118–176 s.
  - The skill now asks for 60–90 s episodes, splitting a long scene at its turn into two episodes with their own
    cliffhangers.
  - `check` notes any episode over 100 s or under 45 s.
  - The script's content is kept, spread over more episodes.
- **Scene openings.** Most episodes and scenes open on 1.5–3 s of a wide shot that already has action in it. 持械入宋
  and 横扫魔域 put a vertical calligraphy location title on it ("秦州府衙后堂", "魔临城").
  - The skill's old rule, "never an establishing shot", is replaced by this pattern, with a cold open as the
    alternative.
  - `check` lists scenes that open without a WS or MS.
  - A location's `name_zh` now becomes a vertical title card, top left, as each new scene opens in an episode
    (`edit.location_cards`). A name card that would overlap it waits until it is gone.
- **Endings.** All five end on a face at the peak, held 2–3 s and cut hard. The last shot runs a median 2.0–3.1 s.
  - The skill says so.
  - `check` notes an episode that ends on a wide shot, an insert, or a shot under 2 s.
- **Shot sizes.** 王妈 and 持械入宋 shoot nearly all dialogue chest-up, with hands and gestures in frame, and punch in
  to a close-up for peaks. Our close-ups were face-filling throughout.
  - New `MCU` type: mid-chest up on a 65mm lens, may carry a line, its own size class, so an MCU-to-CU punch-in is
    a real cut.
  - The skill now puts most dialogue in MCU, uses MS two-shots for intimacy and stand-offs, and quick ECU prop
    inserts.
- **Look by genre.** The comedy is high-key and clean, the romances warm, glowing and glossy, and the period drama
  directional. Our single moody, desaturated look would be wrong for a comedy.
  - `look_preset` in the bible: `cinematic` (the default, unchanged), `bright` or `glossy`.
  - It sets the look paragraph in every prompt, the location plates and the finishing grade (`edit.grade = "auto"`).
  - The flat-light note applies only to `cinematic`.
- **Loudness.** The masters run −9.8 to −15 LUFS (median −10.5); ours were −14. The default is now −11 LUFS, with
  true peak −1.

**Not adopted.**
- **Sub-second flash cuts.** 王妈 has 18% of shots under 1 s and 横扫魔域 14%, in comic beats and action. H3 does not
  place a cut that precisely inside a clip. The 1.2 s minimum stays.
- **Transition stings.** Ink wipes (持械入宋) and white flashes (王妈) appear only in single series.

---

### 0.15 Sound effects and the waimai2 test (2026-10-31)

**Editorial sound effects** (`sheelshort/sfx.py`). A cut may carry `sfx`: an accent the edit lays on a story beat on
top of dialogue, foley and music (boom on a reveal, riser into it, whoosh into a time jump, a message ding). The
engine snaps a cue to the cut as the render actually cut it (scene detection, within 0.5 s of the written time),
aligns the cue file's own sync point to it (a riser's end, a whoosh's peak), and sets its level against the
dialogue's measured loudness before the master is normalised. A library of 15 CC0 Freesound clips was chosen with the
owner's sound skill (github.com/instl999/sound: most downloads, many ratings, the uploader's description) and measured
with ffmpeg for onset, peak, tail and loudness. Beats the library cannot serve are searched per project:
`sfx search` runs the skill, downloads every candidate and prints where its sound sits, since the agent cannot
listen; `sfx add` pins one into the bible with its source and licence (CC0 or Attribution, credited in the
delivery's CREDITS.txt; never NonCommercial). `tools/install_sound_skill.py` fetches the skill at a pinned commit
(upstream has no licence) without its paid-upgrade section. A check of the 100 reference episodes found no
stinger-on-every-cut habit: loud transients start no more often at cuts than at random moments, so cues are kept
sparse (`check` notes more than one every ~10 s). Verified on a copy of waimai2 E01: thunder, buzz, sting, riser and
hit landed on their cuts at +1 to +6 dB over the moment without them.

**What the fresh-agent waimai2 test exposed, and the fixes.** A new agent wrote 《雨夜外卖》 (2 episodes) from the
skill alone. Both masters (102.7 s and 77.5 s) came out with no error exits and no clip re-takes. Faces, eyelines and
light held, and both episodes end on a held face. `make E01,E02` took 78 min: 35 min of it was render, with no 5090
stock for the first 18 min; 16 min was clip QC, 11 of them stalled on an SSL handshake; 16 min was upscale. Fixed:
- A cold open's location card pushed the lead's name card out of the episode. The location card now waits for the
  scene's first wide or medium shot, and gives way when a name card would otherwise be too short to read.
- Subtitles overlapped by up to 0.25 s, and a two-character line stayed up 0.65 s. Subtitles now never overlap, and
  a short line stays up toward 1 s when the next line allows it.
- `check` flagged the episode's own cold open, which the skill allows. The note now covers later scenes only.
- A 20 s test designed voices for the speakers of every episode's opening. It now covers only the episodes in the run.
- schema.md pointed at an outdated example. It now points to waimai2, and SKILL.md gives the loudness as −11 LUFS.

Open: costume changes (the agent made a second character for the suit), phone-call lines (`VO` reads as inner
voice, and there is no phone filter), silent extras, the vision check rewriting a stated age, and the clip-QC stall.

### 0.16 Ready for a 110-episode unattended run (2026-11-01)

Three read-only studies (run logs and job state; 61 picked clips and 293 QC panels; 160 rendered lines, the six masters
and the series structure) found what breaks at the scale of ~1000 clips. Fixed in the engine:
- **One slow server froze the whole run** (waimai2 clip QC, 11.5 min): `pick()` probed servers with 6-try requests while
  holding the run's lock. Probes now run in parallel, once, with 5 s/10 s timeouts and outside the lock; a server that
  fails is skipped for a minute; state writes have their own lock.
- **Any unexpected network error killed the batch** (guilai: `httpx.ReadError: Broken pipe`). `run_job` now catches
  everything, and retries up to `job_retries` (2) times on another server; HTML answers from a restarting server are
  errors, not crashes; a prompt the server lost (restart) is noticed within two minutes instead of waited on for an hour;
  a job whose server left the pool is queued again; a server unreachable for 5 min fails its job. Clips whose takes all
  failed are rendered again; a clip that cannot be prepared is skipped and its episode reported, not the whole batch.
- **The fleet retired its servers before QC and upscale** (waimai2: 18 upscales on one server, 16 min): an added
  server now lingers 2 min after the last render.
- **Downloads** stream to `.part` files and are renamed only when complete (a crash left half takes); each logs its MB/s.
  A take without its record counts only if it has sound (VHS's silent file).
- **Batch**: per-episode isolation in `make`, `BATCH_REPORT.md`, a disk guard, `caffeinate`, the idle reaper from the
  start of a run (a killed run's servers still power off), hard-linked delivery files (~80 GB saved over 110 episodes),
  natural episode order (E100 sorted before E11 and broke name cards), the reference-image budget in `check`.
- **Engine wording vs. writer changes**: a take records a fingerprint of the writer's inputs; an engine prompt change
  no longer re-renders every finished episode (`render.rerender_on_engine_change` to force it).
- **Strangers in 21% of non-wide panels**: every look preset invited "background people". Now only `extras` or a
  `crowd: true` location do; other shots say who alone is in them, and clip QC fails any extra person in a private place
  (the vision model is asked to count blurred figures). A strip answered for fewer panels than cuts is asked again in
  threes.
- **Time of day was fixed per location**: `lights` + clip `time`, plates relit from the base plates by image edit.
- **Loudness**: the music bed sits 18 LU under the measured dialogue (raw beds varied 7 LU); every master is measured
  after encoding and corrected (gain + limiter, video copied): waimai2 E01 -12.9 -> -11.3 LUFS.
- **Cast voices**: a designed voice outside its role's sex range is designed again with the sex spelled out; voices of
  the same sex under a semitone apart are reported.
- Costume changes (`outfits`), phone lines (`via`), extras, sound effects: §0.15.

Open, needing the owner's decisions or a test first (in the review page): episode count vs. 60-90 s length for a
110-集 script, paywall cadence, a series music kit, identity checks in clip QC (face similarity), voice-identity
scoring per line, prop states and injuries, a series layer (`series.json`, `continuity.json`, `check --series`), group
scenes beyond three characters.

## 1. Market research: does this already exist?

**Short answer: partly.** Several open-source projects cover most of the pipeline. None of them matches this exact combination: agent-native, English-market/PineDrama-targeted, H3 plus ComfyUI as user-supplied backends, and fully automatic QC and assembly.

### 1.1 Closest existing projects

| Project | What it does | Gap vs. our vision |
|---|---|---|
| [zenstory-ai/drama-skills](https://github.com/zenstory-ai/drama-skills) (MIT, ~3.6k★, active) | **Closest match.** Claude Code / Codex skills for script → assets → storyboard → image/video prompts → review. Has adapters for Seedance, GPT Image 2, **MiniMax H3** and MiniMax Music. Assembles episodes with ffmpeg (subtitles, loudness). | Chinese-first workflow and pacing. No ComfyUI backend. Generation batches are human-confirmed by design. QC scripts check document cross-references but don't do visual or identity QC. |
| [binwu1/drama-series-agent](https://github.com/binwu1/drama-series-agent/wiki) (Apache-2.0, very early, 7★) | One idea → multi-episode vertical drama. Renders with **MiniMax H3 Ref2VA workflows inside self-hosted ComfyUI**. Has a cast library, tail-frame→first-frame chaining, resumable progress, and a headless CLI. | Tiny and immature. Self-hosted H3 only. Starts from an idea rather than a supplied script. No automated QC. |
| [A-cat-with-carrots/OnlyShot](https://github.com/A-cat-with-carrots/OnlyShot) (MIT, ~345★) | 5-step Claude Skill: script → storyboard JSON → keyframes → Seedance/Jimeng clips. | Jimeng/Seedance only. Final edit is manual in CapCut. Chinese market (Hongguo pacing). |
| [HBAI-Ltd/Toonflow-app](https://www.sourcepulse.org/projects/23907340) (AGPL) | Novel → script → images/video, with decision/execution/supervision agent layers. | Standalone app, not agent-driven. AGPL. No confirmed H3 or ComfyUI support. |
| [HKUDS/ViMax](https://huggingface.co/papers/2606.07649.md) (MIT, research) | Multi-agent: screenwriter, shot planner, character styling, generation, VLM QC with best-of-k. | Research framework, not tied to a vertical-drama format or to H3/ComfyUI. **Good reference for the QC loop.** |
| [Anil-matcha/Open-AI-Micro-Drama-Generator](https://github.com/topics/micro-drama) (~550★) | Multi-agent idea → drama with Seedance 2, Kling 3, Veo 3.1, Sora 2. | Standalone app. No H3 or ComfyUI. |
| Jellyfish, LocalMiniDrama, ZJT ([GitHub topic](https://github.com/topics/script-to-video)) | Script → storyboard → video workspaces. | GUI workspaces rather than agent toolkits. |
| Commercial: [invideo AI Microdrama](https://invideo.io/make/ai-microdrama/), [Morphic](https://morphic.com/resources/tools/microdrama-generator), [microdrama.com](https://microdrama.com/) | Hosted script → vertical episodes with recurring cast. | Closed, locked to their own models, can't plug in your own H3/ComfyUI. An [independent comparison](https://alici.ai/blog/best-ai-short-drama-generators) notes that "assets" and "finished episode" aren't the same deliverable. |

### 1.2 Conclusion and recommendation

The real whitespace is narrow:
1. Built for **English-language, US-market (PineDrama) pacing and conventions**.
2. **Autopilot by default**, with an automated QC loop (vision checks, identity, ASR dialogue check, retries) instead of human-confirmed batches.
3. **Pluggable backends**: MiniMax H3 via the official API *or* via ComfyUI, plus ComfyUI for all image work.
4. **Ends at a delivery package**, not at a folder of clips.

**Update (2026-10-30): the Chinese market is in scope, and automated upload is out of scope.** This removes the biggest drawback of `zenstory-ai/drama-skills`. Its Chinese-first workflow and pacing rules now fit, and it already has an H3 adapter and ffmpeg editing. **Revised recommendation: build on drama-skills, or adopt its skill structure and document formats directly.** Add what it lacks as this project's contribution: the deterministic engine (state, cache, budget, resume), the ComfyUI backend, and the automated QC loop. Make the final fork-vs-depend decision after the M0 spike, once we know which H3 mode holds faces and voices best.

### 1.3 Facts about the targets (with caveats)

- **PineDrama** is TikTok's standalone micro-drama app, launched in January 2026 ([TechCrunch](https://techcrunch.com/2026/01/16/tiktok-quietly-launches-a-micro-drama-app-called-pinedrama/)). It shows serialized ~1-minute vertical episodes and is ad-supported.
  ⚠️ **There is no public upload or creator program.** Content appears to come through negotiated or licensed deals ([Influencer Intelligence](https://www.influencerintelligence.com/blog/GNv/pine-drama-what-tik-toks-microdrama-push-means-for-creators), [Morphic distribution guide](https://morphic.com/resources/guides/filmmakers/where-to-distribute-a-microdrama)). No official AI-content policy was found. The tool should therefore aim at **"PineDrama-grade" deliverables** (also usable on TikTok, ReelShort or DramaBox pitches), not at automated publishing.
- **MiniMax H3** (Hailuo 3, launched 2026-07-31) supports text-to-video, image-to-video (with optional last frame), and reference-to-video. Clips are **4–15 s in whole seconds**, 2K at 24 fps, with 9:16 supported. It generates **native synchronized dialogue, SFX and ambience in the same pass** ([Segmind](https://blog.segmind.com/minimax-h3-release-date-open-weights-and-api-pricing-explained/)). Reference-to-video takes several subject/style images and up to 3 reference audio clips. Reference media **cannot be combined with first/last frame** (400 error) ([Civitai](https://developer.civitai.com/orchestration/recipes/minimax-h3)). Generation is slow (~4–5 min per clip) and async on most providers. Cost is roughly **$0.08–0.16 per output second**. Open-weights status is disputed between sources, but ComfyUI H3 nodes exist (see drama-series-agent).
  ⚠️ Parameter names and limits differ by provider. The adapter must be configurable and must be checked against the official MiniMax API reference.
- **ComfyUI** can be driven over its HTTP/WebSocket API (`/prompt`, `/history`, `/view`, `/upload/image`). An official [Comfy-Org MCP server](https://github.com/Comfy-Org/comfy-mcp) and a [Comfy Cloud MCP endpoint](https://docs.comfy.org/development/cloud/mcp-server) also exist.

---

## 2. Goals and non-goals

**Goals**
- **Market:** Chinese-language short drama is first-class: Mandarin dialogue, Hongguo/Douyin-style pacing, and the mainland API endpoint `api.minimaxi.com`. English is a secondary target. The PineDrama-grade quality bar stays.
- Input: a script (screenplay, prose or outline, in any reasonable format) plus backend links and credentials. Output: N finished episodes as 1080×1920 MP4s with captions, plus a delivery package.
- **The agent runs it end-to-end with zero required human steps.** Approval gates are optional.
- Character, voice and location consistency across all shots and episodes.
- Resumable, idempotent and budget-aware. A crash or a bad shot never forces a full re-run.
- Every artifact is inspectable (JSON + Markdown + media on disk) so a human can step in at any stage.

**Non-goals (v1)**
- Uploading episodes to any platform (PineDrama, Hongguo, Douyin, etc.). The pipeline ends at the delivery package.
- Writing the script from scratch. We adapt and expand a supplied script, and only fill gaps.
- Training custom models or LoRAs. A later option: per-character LoRA via ComfyUI.
- A GUI. A static HTML review page per episode is enough.

---

## 3. Core architecture

**Principle: the agent does the creative and judgment work. The software does the deterministic work.**

```
┌─────────────────────────── Agent (Claude Code, etc.) ───────────────────────────┐
│  Skills (SKILL.md per stage): adapt script, write bible, storyboard, write      │
│  prompts, judge QC results, repair prompts, pick takes                          │
└───────────────┬─────────────────────────────────────────────────────────────────┘
                │ CLI (JSON in / JSON out)  — optionally also exposed as MCP server
┌───────────────▼─────────────────────────────────────────────────────────────────┐
│  sheelshort engine (Python)                                                     │
│  • project state + schemas (pydantic)   • job queue, retries, content-hash cache│
│  • validators (schema, timing, refs)    • cost estimator + budget guard         │
│  • backends:  ImageBackend ─► ComfyUI   VideoBackend ─► H3 API | H3-on-ComfyUI  │
│               ASR (faster-whisper)      TTS (optional fallback)                 │
│  • QC tools: frame sampling, face-embedding identity score, ASR diff            │
│  • editor: ffmpeg assembly, captions, loudness, packaging                       │
└─────────────────────────────────────────────────────────────────────────────────┘
```

Why split it this way:
- The LLM is good at adaptation, shot design and prompt writing. It is unreliable at bookkeeping, polling, retries and ffmpeg flags.
- Every engine command is **deterministic and idempotent**. Re-running a step is safe, and cached results are reused by content hash.
- Validators let the engine reject malformed agent output (for example, a 17 s shot or an unknown character ID) before money is spent.

### 3.1 Agent interface

**Skills** (in `skills/`, loaded by the agent):

| Skill | Purpose |
|---|---|
| `sheelshort-run` | Master playbook: the stage order, when to call which command, and how to handle failures. The entry point for "launch sheelshort". |
| `adapt-script` | Normalize the script and break it into episodes using micro-drama structure rules (§5). |
| `series-bible` | Characters, looks, voices, locations, props, visual style. |
| `storyboard` | Episode → beats → shots, respecting H3 constraints. |
| `prompting-comfy` / `prompting-h3` | Prompt-writing rules and templates per backend. |
| `qc-judge` | How to read QC reports and decide accept, retry, re-prompt, or re-storyboard. |

**CLI** (the engine's whole surface; every command prints JSON):

```
sheelshort init <project> --script script.md --config sheelshort.toml
sheelshort status [--episode E01]            # stage progress, costs, blockers
sheelshort validate <stage>                  # schema + rule checks on agent-written files
sheelshort estimate [--episode E01]          # projected spend before generating
sheelshort gen assets  [--only char:mia]     # ComfyUI: character sheets, locations, props
sheelshort gen keyframes --episode E01 [--shot S03]
sheelshort gen clips     --episode E01 [--shot S03] [--takes 2]
sheelshort qc --episode E01 [--shot S03]     # writes qc report JSON for the agent to judge
sheelshort accept|reject <shot> --take N [--reason ...]
sheelshort edit --episode E01                # assemble master.mp4
sheelshort package [--episodes all]          # delivery folder
sheelshort review --episode E01              # static HTML contact sheet + player
```

An MCP wrapper around the same commands is a thin follow-up.

---

## 4. Inputs and configuration

The user supplies only these:

```toml
# sheelshort.toml
[project]
title = "The Billionaire's Secret Bodyguard"
language = "en-US"
episode_target_seconds = [60, 90]
aspect = "9:16"
budget_usd = 400                     # hard stop
mode = "autopilot"                   # or "gated" (pause after bible + casting)

[video]                              # MiniMax H3
backend = "minimax_api"              # minimax_api | comfyui_h3 | fal | ...
base_url = "https://api.minimax.io"  # the "H3 link" the user provides
api_key_env = "MINIMAX_API_KEY"
model = "MiniMax-H3"
resolution = "2K"

[image]                              # ComfyUI
base_url = "http://127.0.0.1:8188"   # the "ComfyUI link" the user provides
workflows_dir = "workflows/"         # parameterized workflow templates (API format JSON)
auth_header_env = "COMFY_TOKEN"      # optional, for cloud/tunneled instances
```

Secrets are read only from environment variables and never written into project files.

---

## 5. Pipeline stages

Each stage reads and writes files under `projects/<name>/` (layout in §7). Stages marked **A** are agent work, **E** is engine work.

### S0. Ingest (E + A)
- E: convert `.md`, `.txt`, `.docx`, `.fdx` or `.pdf` to plain text and save it as `source/script.txt`.
- A: produce `script.json`, a structured scene list (scene heading, characters, action, dialogue). Mark gaps such as missing scene descriptions.

### S1. Series bible and episode map (A, validated by E)
- **Episode map** (`episodes.json`): split into episodes using micro-drama rules:
  - **Hook in the first 3 seconds** of every episode, with the conflict or reveal up front.
  - **Cliffhanger in the last 5–10 seconds.** The next episode resolves it in its first beat.
  - 60–90 s per episode, 3–6 beats, with an emotional reversal roughly every 15–20 s.
  - Episode 1 must establish the protagonist, their wound or desire, and the antagonist within 30 s.
  - Payoff episodes (slaps, reveals, rescues) at regular intervals, typical of the genre.
- **Bible** (`bible.json`):
  - Characters: ID, age, ethnicity, build, face description, hair, **outfits per arc**, mannerisms, voice description (timbre, accent, pace).
  - Locations: description, time-of-day variants, palette.
  - Props.
  - Visual style: realism level, lens and lighting language, color grade, negative list.

### S2. Storyboard (A, validated by E)
`episodes/E01/storyboard.json`. Per shot:

```json
{
  "id": "E01_S03", "beat": 1, "duration_s": 6,
  "shot": "MCU", "camera": "slow push-in", "angle": "eye level",
  "location": "loc:penthouse_night", "characters": ["char:mia", "char:ryan"],
  "outfits": {"char:mia": "red_gown"},
  "action": "Mia turns from the window, sees Ryan, freezes.",
  "dialogue": [{"char": "char:ryan", "line": "You weren't supposed to be here.", "emotion": "cold"}],
  "sfx": "city hum, glass clink", "music_cue": "tension_rise",
  "video_mode": "i2v",                  // i2v | i2v_firstlast | ref2v | t2v
  "continuity": {"from_prev_tail": false},
  "keyframe_prompt": "...", "video_prompt": "..."
}
```

Validator rules (E):
- `duration_s` is an integer from 4 to 15.
- The episode sum fits the target length.
- All referenced IDs exist in the bible.
- Dialogue fits the duration (about 2.5 words/s).
- At most 2 speaking characters per shot. More than that degrades H3 lip/voice attribution.
- A `ref2v` shot never also sets first/last frame.

### S3. Asset generation: the "casting" stage (E via ComfyUI, judged by A)
- For each character, a **reference pack**: front/¾/profile turnaround, 4–6 expressions, one sheet per outfit, on a neutral background, 9:16 and 1:1.
- Location plates for each time-of-day variant, and key props.
- Generate several candidates per asset. The agent picks one using QC (prompt adherence, artifacts), then the engine **locks** it into `assets/canon/` with a hash. In `gated` mode, this is a human checkpoint.
- **Voice canon:** generate or choose a 5–10 s reference audio clip per speaking character, using TTS or a user-supplied clip. It is later passed to H3 as reference audio to keep voices stable across clips.

### S4. Keyframes (E via ComfyUI, judged by A)
- Per shot, render the **first frame** at 9:16 with canon references. The ComfyUI template does identity conditioning (e.g. IP-Adapter/PuLID/InstantID, or an instruction-edit model composing canon character into the location plate).
- Shots with `from_prev_tail: true` use the previous accepted clip's last frame instead of a new keyframe. This keeps motion and lighting continuous within a scene.
- Quick automated QC (§6) runs before any video money is spent. Keyframes are cheap, so this is the place to iterate.

### S5. Video clips (E via H3)
Mode selection per shot:

| Situation | Mode | Inputs |
|---|---|---|
| Default | `i2v` | keyframe as first frame + `video_prompt` |
| Must land on a specific end state (e.g. a match cut into the next shot) | `i2v_firstlast` | first + last keyframe |
| 2 characters, new setup, identity is the priority | `ref2v` | character canon images, location plate, voice refs (Image 1…N named in the prompt) |
| Establishing or B-roll with no characters | `t2v` | prompt only |

- Dialogue is generated **natively by H3**. The prompt includes exact quoted lines with speaker tags and emotion. Voice reference audio is attached where the mode allows.
- Jobs are submitted async with concurrency limits and polling or webhooks. Raw outputs are stored per take in `clips/E01_S03/take_01.mp4`.
- Every request and response is logged (minus secrets) for reproducibility and cost accounting.

### S6. QC and repair loop (E measures, A decides)
See §6. Possible outcomes per shot: accept, retry with the same prompt, re-prompt, change mode, or send back to storyboard (split or merge shots).

### S7. Edit and assembly (E)
- Trim heads and tails using motion/audio onset detection, plus agent-specified in/out points if any.
- Cuts: hard cuts by default. J/L audio overlaps between dialogue shots so the edit doesn't feel stitched together.
- **Audio:** keep H3 dialogue and SFX, duck under a music bed (generated or from a licensed library folder), and normalize to **-14 LUFS integrated, -1 dBTP**.
- **Captions:** ASR-aligned (faster-whisper word timestamps), checked against the script text. Burned in, mobile-safe (center-lower third, max 2 lines, ~32 chars per line, bold with outline), plus a separate `.srt`.
- Optional opener: series title card on E01. Optional "To be continued" sting on episodes ending in a cliffhanger.
- Output: `E01/master.mp4`, 1080×1920, 30 fps (up-converted from 24 fps), H.264 High, AAC 48 kHz.

### S8. Package (E + A)
`delivery/`:
- Episode MP4s, SRTs, and a burned-in plus clean version of each episode.
- A cover/poster (9:16, generated in ComfyUI from canon assets) and episode thumbnails.
- `metadata.json`: series title, logline, genre tags, episode titles and synopses (A).
- An **AI-generated-content disclosure** note.
- `production_report.md`: cost per episode, takes used, QC scores, known issues.

---

## 6. Quality strategy

Quality is the product. Automated checks feed the agent's judgment:

| Check | How | Threshold / action |
|---|---|---|
| Identity consistency | Face embeddings (InsightFace/ArcFace) on sampled frames vs. character canon | Below threshold → retry, or switch to `ref2v` |
| Visual defects | VLM (the agent itself, viewing ~6 sampled frames) using a rubric: hands, faces, text glitches, morphing, wrong outfit, wrong location | Any "major" → retry |
| Dialogue correctness | ASR transcript vs. script lines (word error rate), speaker check by diarization against voice canon | WER > 15% or wrong speaker → re-prompt |
| Timing | Speech fits the clip, no cut-off words | Fail → lengthen the shot or trim the line |
| Continuity | Adjacent shots: palette/lighting histogram, outfit check by VLM | Flag for the agent |
| Technical | Resolution, fps, duration, black frames, silence, clipping | Hard fail |

- **Best-of-k:** generate 1 take by default. Escalate to 2–3 takes only for shots that fail, or for "hero" shots (hook, cliffhanger) flagged in the storyboard.
- **Retry budget:** at most 3 attempts per shot, and each failed attempt changes something (the prompt, the mode, or the keyframe). After that, the shot goes back to storyboard with a simpler design (fewer characters, less motion, tighter framing).
- **Review page:** `sheelshort review` builds a static HTML page per episode (contact sheet, takes, QC scores, player) so a human can audit quickly.

---

## 7. Project layout on disk

```
projects/<name>/
  sheelshort.toml
  source/script.txt
  script.json
  bible.json
  episodes.json
  assets/
    candidates/char_mia/...
    canon/char_mia/{front,three_quarter,profile,expr_*,outfit_*}.png
    canon/voice/char_mia.wav
    canon/loc_penthouse_night.png
  episodes/E01/
    storyboard.json
    keyframes/E01_S03/{cand_01.png, accepted.png}
    clips/E01_S03/{take_01.mp4, take_01.qc.json, accepted -> take_01.mp4}
    edit/{timeline.json, captions.srt, master.mp4}
  state/
    jobs.sqlite          # job queue, status, provider task IDs, costs
    cache/               # content-hash → output
    logs/
  delivery/
```

The SQLite job table makes everything resumable. A job's key is a hash of (inputs + prompt + params + backend version), so an unchanged shot is never regenerated.

---

## 8. Backend adapters

```python
class VideoBackend(Protocol):
    def capabilities(self) -> VideoCaps: ...           # modes, durations, ratios, ref limits
    def submit(self, req: VideoRequest) -> JobHandle: ...
    def poll(self, h: JobHandle) -> JobStatus: ...
    def fetch(self, h: JobHandle, dest: Path) -> Path: ...
    def price(self, req: VideoRequest) -> float: ...

class ImageBackend(Protocol):
    def run_workflow(self, template: str, params: dict, inputs: dict[str, Path]) -> list[Path]: ...
```

- **`MiniMaxH3Api`**: the official API. Parameter names live in a provider profile (YAML), so differences across MiniMax, fal and others are handled by config rather than code.
- **`ComfyUIH3`**: H3 nodes inside the user's ComfyUI (as in drama-series-agent), for self-hosting or when open weights are available.
- **`ComfyUI` image backend**: workflows are stored as **API-format JSON templates** with named placeholders, e.g. `{{positive}}`, `{{ref_image_1}}`, `{{seed}}`, `{{width}}`. The engine uploads inputs, injects params, queues the prompt, listens on the WebSocket, and downloads outputs. Required custom nodes and models are listed in a `workflows/manifest.json` and checked at `init`, so missing nodes fail fast with a clear message.
- **Reference images:** some H3 providers need **public URLs**. The adapter supports an optional upload target (S3/R2 presigned URLs) or the provider's own upload endpoint.

---

## 9. Cost and performance (rough)

The 60–90 s episode figures assume about 12 shots of ~6 s, 2K, at an average of 1.4 takes:
- Video: 12 × 6 s × 1.4 × ~$0.13/s ≈ **$13 per episode**. At $0.08–0.16/s the range is $8–16.
- Images: negligible on self-hosted ComfyUI.
- A 60-episode series costs roughly **$500–1,000** in video. `estimate` and `budget_usd` guard against overruns.
- Wall-clock: about 5 min per H3 clip. With 4 concurrent jobs, one episode takes ~20–30 min, so throughput depends on provider concurrency limits.

---

## 10. Risks and open questions

1. **Distribution.** PineDrama has no public upload, so "publishing on PineDrama" means pitching or licensing. Deliverables should follow general platform specs. The user should confirm current content and AI-disclosure requirements with TikTok directly.
2. **Voice consistency across clips.** H3 generates a fresh voice per clip, so the same character can sound different between shots. Mitigations, in order: H3 reference audio; reuse of voice prompt text; and, if still inconsistent, a **fallback path**: replace dialogue with consistent TTS plus a lip-sync pass in ComfyUI (e.g. LatentSync), stripping H3's audio. This is the biggest technical unknown and the first thing to prototype.
3. **H3 API uncertainty.** Sources disagree on 768p availability, sync vs. async behavior, reference limits and open weights. This is handled by provider profiles plus a `sheelshort doctor` probe command.
4. **Identity drift in multi-character shots.** Limit to 2 characters per shot, prefer `ref2v` for those shots, and cover with singles and over-the-shoulder shots (standard micro-drama coverage anyway).
5. **Rights.** The user must own or license the script. Generated likenesses must not resemble real people. Add a bible-stage check that rejects descriptions naming celebrities.
6. **Content policy.** Genre staples (violence, romance) must stay within provider and platform rules, or failed generations waste budget. The adapt-script skill should flag risky beats early.

---

## 11. Roadmap

| Milestone | Scope | Exit criteria |
|---|---|---|
| **M0 Spike** (1 week) | Hand-written 3-shot scene. Call H3 (i2v + ref2v) and ComfyUI directly from scripts. | Answer the voice-consistency and identity questions with real outputs. Confirm API parameters. |
| **M1 Paper pipeline** | S0–S2: skills, schemas, validators, `init`, `validate`, `status`, `estimate` | A real script produces a valid bible, episode map and storyboards with no media spend. |
| **M2 Casting + keyframes** | ComfyUI backend, workflow templates and manifest, S3–S4, identity QC | Canon pack and keyframes for E01 pass QC. |
| **M3 Clips** | H3 adapter(s), job queue, cache, S5, cost tracking | All E01 shots generated, resumable after a kill. |
| **M4 QC + edit** | S6 loop, ASR, S7 assembly, review page | E01 `master.mp4` is watchable without manual edits. |
| **M5 Autopilot + package** | Full multi-episode run, S8, budget guard, MCP wrapper | A 10-episode series runs from a single "launch" instruction. |

**Suggested stack:** Python 3.12, `uv`, pydantic v2, httpx, websockets, SQLite, ffmpeg/ffprobe, faster-whisper, insightface, Jinja2 (review page and prompt templates). Skills are plain Markdown and agent-agnostic (Claude Code first).
