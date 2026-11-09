---
name: sheelshort
description: Turn a user's script into finished vertical short-drama (short drama) episodes on their ComfyUI servers. Use when the user gives a script, story, outline or novel chapter and wants episodes produced, or asks to run/resume/fix a sheelshort project.
---

# sheelshort: script → episodes

You do the creative and judgment work; `python -m sheelshort` does everything deterministic (GPU jobs, retries,
timing, assembly). Every command is resumable: re-run it after any failure.

## 0. Setup (once)
- `sheelshort.toml` at the repo root lists the ComfyUI servers (copy `sheelshort.example.toml`).
- `python -m sheelshort new <project> --script <file>` creates `projects/<project>/`.
- `python tools/install_sound_skill.py` installs the sound skill that `sfx search` uses (once per machine, for the
  Freesound fallback; owner-library catalog searches do not need it).
- For BGM and editorial effects, use the owner's private
  [short-drama-audio-library](https://github.com/instl999/short-drama-audio-library). Read
  [reference/audio-library.md](reference/audio-library.md) before the sound pass: clone its catalog without
  downloading all audio, search English mood/function tags, and fetch only chosen files. Keep its audio in the
  separate library checkout or project inputs, outside this repository's tracked source files.

## 1. Read the script, plan the series
Read the whole script. Decide the episode split (**60–90 s each**, as the reference hits run; one hook, one turn, one
cliffhanger per episode). A script scene that runs longer becomes two episodes split at its turn, each with its own
cliffhanger: `check` notes any episode over 100 s.
If the script is a single scene, it is one episode. Keep the user's dialogue; tighten it only where a line is too long
for its shot (rule below), and say what you changed.

**For a series, write `arc.json` first** (one row per episode, before any episode file):
`{"episodes": [{"ep": "E001", "hook": "...", "conflict": "...", "shape": "humiliation", "turn": "...",
"cliffhanger": "...", "paywall": false}, ...]}`. `shape` is the episode's conflict in one or two words
(humiliation, reveal, confrontation, rescue, betrayal, reversal...). `check` refuses an episode file without its row
and notes three episodes running with the same shape or the same places, and an episode that does not open on the
cliffhanger before it (the same people as that episode's last clip, or a `picks_up` line in the episode saying how it
picks up). Long runs die of sameness: vary the shapes.

## 2. Write `bible.json` (see `reference/schema.md`)
Characters, locations, props, visual style. Rules that matter:
- **`style`** (opens every prompt) is lens, grade and realism only, with **no time of day or light source**. Each
  location has its own **`light`** (e.g. "deep night indoors; the only light is the single bare bulb and moonlight
  through the window; no daylight anywhere"). A sunny style sentence turned a night scene's close-ups into daylight.
- **`look`**: one precise sentence, age, face shape, brows, eyes, one or two identity anchors that will be
  *visible in a front close-up*, hair, outfit. Never a real person or celebrity. Make the anchors large and stable:
  a hairstyle, glasses, earrings, a hair clip, a coat's colour. Small facial marks (a mole, a beauty mark, freckles,
  a thin scar) drift and flicker from shot to shot in the video model: use one only when the story needs it, and
  then make it clearly visible. `check` notes them.
- **`keep`**: the short list of things that must never change (anchors, hair, outfit).
- **`voice_design`** (Chinese): **timbre only**: age, pitch, texture, accent, pace. *No emotion, no attitude*
  ("suppressed anger" or "lazy, mocking" in a voice description makes every line come out in that one register).
- **`voice_sample`**: one neutral sentence of ~25–35 characters the cast voice will say (its transcript).
- **`voice_en`**: the same timbre in English, a few words.
- **`demeanor`**: the character's standing manner, in English, as the audience should read them in any unwritten
  moment ("composed and warm, attentive eyes; never sarcastic or contemptuous"; for a villain "smug, eyes half-lidded,
  a faint sneer"). The engine repeats it in every face shot of the character (a shot's own action overrides it), so
  the video model fills gaps in character: without it, Lin Wan got an eye-roll that read as contempt. `check` notes main
  characters (with a `card`) that have none.

## 3. Write `episodes/<EP>.json`
**What the story changes (season ledger).** The engine carries every episode's end state into the next one
(`python -m sheelshort ledger <project>` writes `ledger.md`). Tell it what changes:
- `when` (the story day and time the episode starts, "Day 3, evening") and `jump` when time has passed since the episode
  before ("three days later"). Without a `jump` the episode continues straight on, so nobody may change clothes between them
  unless a clip's `outfits` shows it.
- `changes`: `{"after": "K3", "who": "linwan", "injury": {"left_hand": "a white gauze bandage wrapped round her left
  palm"}}` (written into every later prompt of hers until `{"who": "linwan", "heals": ["left_hand"]}`; make injuries
  large and plain, like the identity anchors); `{"who": "zhaokai", "learns": ["father"]}`; `{"prop": "bangle",
  "where": "in the tray on the dresser", "holder": "zhoumin"}`; `{"prop": "letter", "gone": "burned in the fireplace"}`
  (and `"back"` if it returns). Without `after` a change happens at the episode's end.
- Secrets are the bible's `facts` (`{"father": "Shen Huaishan is Tang Ning's father"}`); a cut whose line depends on one says
  `"knows": ["father"]`, and `check` refuses it if the speaker has not learned it yet.
`check` also refuses a prop used after it is gone.

Set `music` to mood tags for the instrumental bed (e.g. "suspenseful night drama underscore, solo cello, sparse piano, no vocals"). Clips of 4–15 s; each clip is a sequence of cuts (default 3 s, `dur` may be 2–6). One location per clip, at most
three characters (better two). Fields per cut: `type`, `subjects`, `speaker`, `over`, `line`, `say`, `act`,
`emotion`, `delivery`, `composition`, `action`, `camera`, `sound`, `end`, `props`. Write character and location
references in text as `{charid}` / `{loc}`.

**Pacing like the mature series** (the original Village Grab E01: 151 s, 73 shots, mean 2.1 s, median 1.8 s, 2 shots over
5 s): aim for **2–2.5 s per cut, 4–6 cuts per 10–15 s clip**, and keep the script's full content (spread over more
60–90 s episodes, not cut down to a summary). Long lines are split with **reaction cuts** (`RX`): the listener's close-up while the line continues
off-screen. `check` lets a line span its own cut plus the `RX` cuts after it.

**Name cards:** give every main character `name_zh` and `card` (a short role: "college student from Huanggu Village", "developer from Qiangsheng Group") in
the bible. Their first solo close-up in the series (or a cut with `"intro": true`) gets a vertical name card beside the
head, as in the reference series. Write that shot as a held chest-up close-up composed off-centre.

**Shot sizes, as the reference hits use them:** most dialogue in `MCU` (mid-chest up, hands and gestures in frame:
Mama Wang as a Nanny and Armed Into Song shoot nearly all talk this way); `CU` for the emotional peaks, punching in from the `MCU`;
`MS` two-shots with both faces in frame for intimacy and stand-offs (Backup Contact, Handling the Villain); quick `ECU` inserts of
the props the story turns on (a number card, a contract, a phone, joined hands).

**Shot grammar (enforced by `check`):** speaking character → `CU` or `MCU`; two-person dialogue → `OTS` (`over` = whose
shoulder, never the speaker); positioning and movement → `MS`; a scene's geography → `WS` (wide, no dialogue);
reactions and actions → `CU`; inner voice → `VO` (close-up pushing in, lips closed; `subjects` is who is on screen:
the speaker, or someone the speaker is thinking about, never empty); listener reacting while a line continues → `RX`
(one listener, no line). Dialogue is never in an `MS` or `WS`. One line per cut, one speaker per cut.

**Editing like a cutter, not a slideshow** (owner review: "cuts feel jarring and nonsensical"):
- **No jump cuts** (`check` refuses them): two adjacent cuts never show the same people at the same size, also across
  a clip boundary in the same location. The trap is *A speaks → `RX` on B → B answers in `CU`*: B's two close-ups jump.
  Instead let the reaction be someone else (a third person, the crowd), answer in an `OTS` past A's shoulder, or give
  B one longer `CU` that reacts first and then answers. When one person is alone (a phone call, a monologue), move the
  camera around them: `"angle": "profile"` (or `three-quarter`, `high`, `low`) on the second shot is a real cut.
  `check --fit` makes these repairs where it can.
- **Cover an exchange as shot/reverse-shot:** alternate singles and `OTS`; aim for at most 65% single close-ups.
- **Show the geography:** after the hook, every scene has a `WS` or `MS` that shows who stands where (the hero facing
  the crowd, the table between them). Without it the singles float. Show the room again at least every 25 s: `check`
  notes any longer run of close-ups and over-the-shoulder shots, and `--fit` turns a silent reaction close-up in that
  run into a medium shot of the listener among the others, where the rules allow. Fix the runs it can't by writing a
  `WS` or `MS` beat into the scene.
- **Screen direction is automatic:** the engine puts the two sides of each exchange on opposite sides of the line
  and makes every single look across it at the person addressed, never into the lens. Set `to` on a cut when the
  addressee is not the obvious one, and `blocking` on the episode (`{"loc": {"charid": "left"|"right"}}`) when allies
  must stand together.
- **Cut on a reason:** a new line, a reaction to it, an action, a reveal. Each cut's `camera` should carry the beat:
  a slow push-in on a realisation, handheld drift in a quarrel, a pull-back to isolate someone, a rack focus between a
  foreground and a background face, a tilt down to the prop. Keep "static" for stillness that means something.
- **Inserts** (`ECU` on a prop or hands, 1–1.5 s) carry plot: the contract, the pen, the money.

**Look preset:** set `look_preset` in the bible by genre: `cinematic` (rural, period, thriller: moody, directional
light), `bright` (comedy, bright urban stories: high-key and clean, like Mama Wang as a Nanny), or `glossy` (romance: warm,
glowing, soft bokeh, like Backup Contact). It sets the light and acting of every prompt, the location plates and the
finishing grade. The notes below are for `cinematic`.

**Look:** `style` names the medium and grade, never a wide lens or "crisp" detail (the engine adds long lenses, shallow
focus, directional light, real skin and restrained acting to every prompt). Each location's `light` has a direction and
a source: low golden sun behind or beside the actors with a warm rim, a bare bulb as key with blue window light, neon
reflections on wet ground. "Bright midday sun, soft shadows" or "overcast" gives the flat AI look; `check` notes it.

**Micro-drama structure** (measured on 100 episodes of 5 hits): the conflict is on screen within the first 3 s. Most
episodes and scenes open on 1.5–3 s of a `WS` that already has action in it (the maids lined up being addressed, the
hero walking into the street), with the location card on it (give each location a `name_zh`), and then cut in; a cold
open mid-confrontation (a slap, an accusation, a face mid-reaction) also works. There are 4–5 shots in the first 10 s.
Something changes every 8–10 s. The episode ends on a new complication at peak emotion (cliffhanger), and **its last
shot is a face**, a close-up or reaction held 2–3 s and cut hard, never a wide shot or a fade. The next episode
resolves it in its first beat. Show emotion
through observable cues (eyes drop, jaw tightens, a delayed answer), not emotion labels. In a close-up never write
"looks up at": the model moves the eyes alone and it reads as an eye-roll (contempt). Write "tilts her head back to
look up at..." (the engine adds this too, and clip QC re-takes an eye-roll the script does not ask for).

**Time of day, crowds.** Write one location per place and give it `lights` for its other times of day; a clip picks
one with `"time": "night"` (never a second location for the same room by night). Mark public places `crowd: true`;
a private room shows only the people written into the shot.

**Costume changes, phone calls, extras.** A character who changes clothes gets bible `outfits`, and the episode (or
clip) says who wears what (`"outfits": {"chenmo": "suit"}`). A line heard over the phone is `"via": "phone"` on a shot
of the listener (`MCU`/`CU` of them holding the phone; the caller is not in `subjects`); cut to the caller without
`via` to show them talking. People in the background who never speak go in `extras` on a medium, wide or
over-the-shoulder shot ("two lawyers in black suits behind {chenmo}"); a shot without `extras` holds only its
`subjects`, and clip QC re-takes a close or medium shot that came back with a crowd.

**BGM:** unless the user specifies another source, search the owner's audio library first by the episode's emotional
arc (for example `suspense`, `sad`, `warm`, `romantic`, `epic`). Follow
[reference/audio-library.md](reference/audio-library.md): download and verify the chosen recording, then merge its
absolute local path into the project's `sheelshort.toml` under `[edit].music`. The episode JSON's `music` is a
generation prompt, not a local-file setting. Record the selection per episode; `[edit].music` applies to the whole
project, so update it before assembly when another episode needs a different track. If no library track fits,
use the existing `music = "auto"` generation workflow.

**Sound effects (`sfx` on a cut):** after the cuts are written, do a sound pass. The render already carries the
diegetic sound you describe in `sound` (rain, footsteps, a door); `sfx` adds an *editorial* accent the edit lays on
the beat: the boom as a hidden identity is revealed, a riser into it, the whoosh of a time jump, the ding of the text
the viewer must notice, the heartbeat while someone is about to be caught. Keep them for the beats that turn the story,
at most one every ~10 s (`check` notes more): a cue on every cut is noise and none of them lands. A cue lands on the
cut's first frame as the render actually cut it (a `riser` ends there, a `whoosh` peaks there); `{"cue": "slap",
"at": 0.8}` puts it at a moment inside the cut. `python -m sheelshort sfx <project>` lists the cues.
When no built-in cue fits the beat, search the owner's audio library by mood and function (`suspense`, `horror`,
`riser`, `whoosh`, `transition`) as described in [reference/audio-library.md](reference/audio-library.md). Fetch the
selected file, copy it into the project's `inputs/sfx/`, measure its onset/peak/end with `sheelshort.sfx.measure`,
and register a bible `sfx` cue with its local file, measured `sync`, level and purpose. Retain its catalog ID,
hash and provenance in project notes; the owner library's recordings have unknown licenses, not CC0.
If neither a built-in cue nor an owner-library recording fits the actual source of the beat (a wooden palace door,
a sword drawn, a car braking hard, a sad erhu note), search Freesound:
- `python -m sheelshort sfx <project> search "<precise English description of the source>"`, one query at a time
  (Freesound refuses bursts). It lists the most downloaded CC0 clips with their rating, the uploader's description,
  and where the sound sits in each file (onset, peak, end, loudness), since you cannot listen to them.
- Pick on evidence: most downloads and many ratings (★4.4(380) beats ★4.9(6)); a description that names the same
  source as the beat (reject a different source however popular); a length that fits the cut (a "thud" can be a 3 s
  rumble). `--license by` widens the search to Attribution clips (credited in the delivery's CREDITS.txt); never
  NonCommercial.
- `python -m sheelshort sfx <project> add door_slam fs:571797 --what "a heavy door slammed in anger"` copies it into
  the project and the bible's `sfx` with its measured sync point (`--sync peak` or `end` when the moment that matters
  is not the first sound, e.g. a riser; `--level` in dB against the dialogue, default -4).

**Every cut is written out in full: `check` refuses an episode that isn't, and nothing renders until it passes.**
The video model takes words literally and invents whatever is missing, and what it invents is often wrong (an eye-roll
that made the heroine read as contemptuous). So every cut states everything the camera will see, from its first frame to
its last:
- `composition` (12+ words): the framing, who stands where (left, right, foreground, by the window), what is behind them.
- `action` (15+ words, 8 more for each further person): for each person, by `{id}`: the body (stands, steps, leans,
  turns which way), the head, where the eyes go and when, the mouth (speaking, or closed), the hands, and how the
  expression starts and changes. A face shot covers at least four of eyes, head, mouth, hands, body, expression; a wide
  or medium shot places everyone and moves them.
- `camera` (4+ words): the move (static, slow push in, handheld drift, pan left), its speed and height.
- `end` (6+ words): the last frame: where each person is, their pose, gaze and expression.
- `delivery` (5+ words, with a line): the face while the line is said.
- No vague verbs and no feelings the camera can't see (see below).

A complete cut:
```json
{"type": "MCU", "subjects": ["linwan"], "speaker": "linwan", "line": "This one. Full payment.", "say": "This one... full payment.",
 "act": "calm, as if buying a coffee, the last two words landing softly", "emotion": "calm",
 "delivery": "calm and offhand, a faint easy smile on the last word, eyes steady on the view",
 "composition": "Medium close-up from mid-chest up of {linwan} on the right of frame against the bright floor-to-ceiling window, the city soft and out of focus behind her.",
 "action": "{linwan} stands still facing the window, shoulders relaxed, her left hand resting on the tote strap. She keeps her head level and her eyes on the view while she speaks, lips moving only on the words, then turns her head slowly to her right toward {zhoumin} off screen, a faint easy smile settling on her face.",
 "camera": "Static at eye level, the lens slightly below her shoulder line.",
 "end": "{linwan} holds the faint smile, head turned right, eyes calm and level.", "dur": 2.6}
```
`--allow-thin` renders anyway; it exists for example episodes written before these rules, never for new work.

**Write every small action out (H3 acts out words literally and invents what is missing).** In every face shot
(`CU`, `MCU`, `ECU`, `OTS`, `RX`, `VO`) the `action` says, from the first frame to the last: where the eyes go and
whether the head moves with them, what the mouth does (closed when not speaking), what the hands do, and how the
expression starts and changes. Ten words is the floor; two sentences is normal. Never a feeling the camera can't see
("unreadable", "complicated"): describe the face instead. Instead of → write:
- "looks up at X" → better not in a close-up at all: show the height in a wide or low-angle shot and keep the face
  level. If it must: "tilts her head back to look up at X, eyes calm and open" (eyes alone read as an eye-roll, and
  even a head tilt can show the eyes travelling first)
- "glances at X" → "turns her eyes, head still, to X for a beat, then back to Y"
- "reacts" → the physical reaction: "her eyes widen, she holds her breath, the cup stops halfway to her lips"
- "smirks" → "one corner of his mouth lifts slightly; his eyes stay cold"
- "turns away" → "turns her head left, away from him, toward the window"
`check` notes face-shot actions that are too short or vague.

**Acting fields (this is what makes the voices sound acted):**
- `line`: the clean subtitle text.
- `say`: what the actor actually says, with the rhythm in the punctuation: `……` for a break, `——` for a cut-off,
  `！`/`？！` for force, repeated syllables for stammers ("Please... please."). Same words as `line`.
- `act`: a Chinese performance direction for the voice, specific and physical: "holding back tears, voice shaking, the last two words almost unspoken",
  "a cold laugh, one word at a time", "rapid breathing, voice rising, about to lose control". Not just "angry".
- `emotion`: one word for scoring: calm, cold, firm, warm, whisper (kept calm) or sob, broken, plead, anger, shout,
  fear, shock, despair (intense: wider pitch band allowed).
- `delivery`: the same performance in English for the *face* in the picture prompt ("eyes brimming, voice
  cracking on the last word").
- Length: a line must fit about 4.5 Chinese characters per second of its cut; the performance may lengthen the cut
  (the engine retimes cuts to the acted lines, up to 15 s per clip). If `perform` says a clip needs > 15 s, split it.

## 4. Produce
```
python -m sheelshort check <project> --fit    # lengthens dialogue cuts too short for their lines, prints each
                                              # episode's planned pacing; fix every rule it reports before GPU time
python -m sheelshort cast <project>           # close-ups, three-view sheets, props, plates, cast voices
python -m sheelshort perform <project> E01    # only with render.dialogue = "acted": Drama 3 acted takes + dialogue tracks
python -m sheelshort render <project> E01     # H3 renders each clip and voices the lines (default) from the cast voices
python -m sheelshort assemble <project> E01   # master.mp4 (+ master_clean.mp4): RTX 1080p upscale, subtitles, cards, music, sound effects, -11 LUFS
python -m sheelshort review <project> E01     # edit/E01/review.html
python -m sheelshort package <project>        # delivery/: episodes, clean cuts, SRTs, cover, thumbnails, metadata
```
**Lock the series once the cast is approved** (before the long run): `python -m sheelshort lock <project> --note
"cast approved"` freezes the workflows, render and edit settings, LoRAs, models and every cast and location reference
in `series_lock.json`. `check` (so `make`/`render`) then refuses anything that drifted, and a render server whose image
changed is not used. A deliberate change (a re-cast, a new look after an A/B) is locked again with a note; never
relock to get past a drift you cannot explain. After a server image or model change, `python -m sheelshort smoke
<project> E01 --only K1` renders one clip at its production seed against the reference render (`--accept` the first
time).

**Graph A/Bs:** `python -m sheelshort ab <project> E01 --only K1,K2 --variants current,tuned` renders the same clips
with the same prompt, references and seed under each variant (`ab/<EP>/`: a frame sheet per clip, a side-by-side
video and `report.md`). The owner judges; a default changes only after that.

(`make <project> E01` runs them all. **Test first:** `make <project> E01 --seconds 20` produces only the opening
~20 s as a finished video (`edit/E01/master_20s.mp4`), casting only the characters in it: use it to check casting, voices
and look before spending GPU time on the whole episode.)

## 5. Judge, then fix only what fails
- **Casting:** `cast` checks every close-up and sheet with the servers' vision model before anything is rendered.
  The model describes the image blind, and the engine compares that with the bible: apparent age, sex, each `keep`
  mark's feature and side, glasses, costume colours, and three matching views on a sheet.
  - A failing image is regenerated (`qc.attempts`).
  - Whatever text can fix is then aligned to the image in `bible.json` (mark side and position, stated age).
  - Once a project has rendered clips, its existing images are canon and are never regenerated.

  Read `assets/qc.json` and look at the images yourself. For what remains (e.g. "glasses missing"), regenerate
  (delete the file and its `cast/...` job in `state.json`) or rewrite the character.
- **Performance:** read `perform/<EP>/choice.json`. A line marked "no take passed" needs a new `act` or `say`, or
  more takes (`perform --only K1_2` after deleting its takes). Listen to the chosen takes in the review page.
- **Clip QC is automatic:** `render` checks every take (each line heard, on-screen text, day/night vs the location's
  `light`, wrong person, malformed faces) and re-takes failures up to `qc.max_takes`; results are in
  `edit/<EP>/clipqc/` and the review page. Re-running `render` on a finished episode re-checks it.
- **Picture:** still sample frames of each clip (ffmpeg `-vf fps=1`) and look: faces match the close-ups, the right
  person speaks, no extra people, no on-screen text, mouths move on the lines. A failed clip:
  `render ... --only K2 --takes 2`, then choose the take in `edit/<EP>/picks.json` (`{"K2": 2}`) and re-assemble.
- **Long batches:** `make <project> E001,E002,...` keeps going when an episode fails (it is listed in
  `BATCH_REPORT.md` with the reason, the others are finished), retries a failed job on another server, re-renders a
  clip whose takes all failed, keeps the Mac awake, and stops cleanly when the disk runs low (`min_free_gb`). Use
  zero-padded ids for long series (E001). A take is kept when only the engine's prompt wording changed, not when the
  writer changed the clip, a reference or a voice.
- **Servers are on demand:** with `[fleet] core` set, every server is off while nothing renders. The first GPU job
  of a command powers one on, which takes about 1–3 min: a quiet start is not a hang. The render adds more while clips
  wait. Servers power off about 10 min after the last command ends. `python -m sheelshort fleet <project>` shows each
  server and which run uses it, and `... fleet <project> off` powers off every server no run uses. When AutoDL has no
  GPU in stock the run keeps asking (every stopped server, several rounds, then every minute) and waits. Every server
  is probed for RTX upscaling each time it powers on (a power-on can land on another GPU); one that cannot is replaced:
  a new server is acquired and probed first, then the old one is powered off. An episode no server could upscale is
  held back and reported, never cut from a soft resize. Instances shut down for 3 hours are released (deleted);
  `fleet <project> release <uuid>` releases one at once.
- **Estimated times:** every GPU job logs when it should be done (`queued on X, done ~21:04 (in ~6 min)`). Every
  phase (render, check, re-take, upscale) logs `done/total` with its end time. `python -m sheelshort status <project>`
  shows the same from another terminal. Estimates learn from the servers' measured times
  (`.cache/job_times.json`), so the first run on a new setup is rougher. Give the user the end time when they ask
  how long a run will take.
- **Drift across the series:** after each episode, `make` measures every single-person face shot against the
  character's close-up (SFace), the master's brightness and white balance, and each line's pitch against the cast
  voice (`drift/<EP>.json`); `drift.md` lays the episodes side by side, and alarms go into `BATCH_REPORT.md`. A face
  under 0.36 or well under its own series level, a colour shift, or a voice over two semitones off means: look at
  those shots before the next episode.
- **Compliance:** every master carries the visible AI label (`edit.ai_label`, top right) and the AIGC metadata field;
  the clean cut too. `[compliance] terms` lists words that may not appear in any line, title or card; `check` also
  notes phone numbers, web addresses and contact handles (platforms reject traffic diversion).
- Never change a tested default without an A/B; report what you changed and why.
