---
name: sheelshort
description: Turn a user's script into finished vertical short-drama (短剧) episodes on their ComfyUI servers. Use when the user gives a script, story, outline or novel chapter and wants episodes produced, or asks to run/resume/fix a sheelshort project.
---

# sheelshort: script → episodes

You do the creative and judgment work; `python -m sheelshort` does everything deterministic (GPU jobs, retries,
timing, assembly). Every command is resumable: re-run it after any failure.

## 0. Setup (once)
- `sheelshort.toml` at the repo root lists the ComfyUI servers (copy `sheelshort.example.toml`).
- `python -m sheelshort new <project> --script <file>` creates `projects/<project>/`.

## 1. Read the script, plan the series
Read the whole script. Decide the episode split (60–120 s each; one hook, one turn, one cliffhanger per episode).
If the script is a single scene, it is one episode. Keep the user's dialogue; tighten it only where a line is too long
for its shot (rule below), and say what you changed.

## 2. Write `bible.json` (see `reference/schema.md`)
Characters, locations, props, visual style. Rules that matter:
- **`style`** (opens every prompt) is lens, grade and realism only, with **no time of day or light source**. Each
  location has its own **`light`** (e.g. "deep night indoors; the only light is the single bare bulb and moonlight
  through the window; no daylight anywhere"). A sunny style sentence turned a night scene's close-ups into daylight.
- **`look`**: one precise sentence, age, face shape, brows, eyes, one or two identity anchors (beauty mark, scar)
  that will be *visible in a front close-up*, hair, outfit. Never a real person or celebrity.
- **`keep`**: the short list of things that must never change (anchors, hair, outfit).
- **`voice_design`** (Chinese): **timbre only**: age, pitch, texture, accent, pace. *No emotion, no attitude*
  ("压抑着愤怒" or "lazy, mocking" in a voice description makes every line come out in that one register).
- **`voice_sample`**: one neutral sentence of ~25–35 characters the cast voice will say (its transcript).
- **`voice_en`**: the same timbre in English, a few words.

## 3. Write `episodes/<EP>.json`
Set `music` to mood tags for the instrumental bed (e.g. "suspenseful night drama underscore, solo cello, sparse piano, no vocals"). Clips of 4–15 s; each clip is a sequence of cuts (default 3 s, `dur` may be 2–6). One location per clip, at most
three characters (better two). Fields per cut: `type`, `subjects`, `speaker`, `over`, `line`, `say`, `act`,
`emotion`, `delivery`, `composition`, `action`, `camera`, `sound`, `end`, `props`. Write character and location
references in text as `{charid}` / `{loc}`.

**Pacing like the mature series** (the original 全村抢 E01: 151 s, 73 shots, mean 2.1 s, median 1.8 s, 2 shots over
5 s): aim for **2–2.5 s per cut, 4–6 cuts per 10–15 s clip**, and keep the script's full content (episodes of 90–150 s,
not a summary). Long lines are split with **reaction cuts** (`RX`): the listener's close-up while the line continues
off-screen. `check` lets a line span its own cut plus the `RX` cuts after it.

**Name cards:** give every main character `name_zh` and `card` (a short role: 「黄谷村大学生」, 「强盛集团开发商」) in
the bible. Their first solo close-up in the series (or a cut with `"intro": true`) gets a vertical name card beside the
head, as in the reference series. Write that shot as a held chest-up close-up composed off-centre.

**Shot grammar (enforced by `check`):** speaking character → `CU`; two-person dialogue → `OTS` (`over` = whose
shoulder, never the speaker); positioning and movement → `MS`; a scene's geography → `WS` (wide, no dialogue);
reactions and actions → `CU`; inner voice → `VO` (close-up pushing in, lips closed; `subjects` is who is on screen:
the speaker, or someone the speaker is thinking about, never empty); listener reacting while a line continues → `RX`
(one listener, no line). Dialogue never in an `MS` or `WS`. One line per cut, one speaker per cut.

**Editing like a cutter, not a slideshow** (owner review: "cuts feel jarring and nonsensical"):
- **No jump cuts** (`check` refuses them): two adjacent cuts never show the same people at the same size, also across
  a clip boundary in the same location. The trap is *A speaks → `RX` on B → B answers in `CU`*: B's two close-ups jump.
  Instead let the reaction be someone else (a third person, the crowd), answer in an `OTS` past A's shoulder, or give
  B one longer `CU` that reacts first and then answers.
- **Cover an exchange as shot/reverse-shot:** alternate singles and `OTS`; aim for at most 65% single close-ups.
- **Show the geography:** after the hook, every scene has a `WS` or `MS` that shows who stands where (the hero facing
  the crowd, the table between them). Without it the singles float.
- **Screen direction is automatic:** the engine puts the two sides of each exchange on opposite sides of the line
  and makes every single look across it at the person addressed, never into the lens. Set `to` on a cut when the
  addressee is not the obvious one, and `blocking` on the episode (`{"loc": {"charid": "left"|"right"}}`) when allies
  must stand together.
- **Cut on a reason:** a new line, a reaction to it, an action, a reveal. Each cut's `camera` should carry the beat:
  a slow push-in on a realisation, handheld drift in a quarrel, a pull-back to isolate someone, a rack focus between a
  foreground and a background face, a tilt down to the prop. Keep "static" for stillness that means something.
- **Inserts** (`ECU` on a prop or hands, 1–1.5 s) carry plot: the contract, the pen, the money.

**Look:** `style` names the medium and grade, never a wide lens or "crisp" detail (the engine adds long lenses, shallow
focus, directional light, real skin and restrained acting to every prompt). Each location's `light` has a direction and
a source: low golden sun behind or beside the actors with a warm rim, a bare bulb as key with blue window light, neon
reflections on wet ground. "Bright midday sun, soft shadows" or "overcast" gives the flat AI look; `check` notes it.

**Micro-drama structure:** the first cut of every episode is the most dramatic moment (a slap, an accusation, a
reveal, a face mid-reaction), never an establishing shot. Something changes every 8–10 s. The episode ends on a
new complication at peak emotion (cliffhanger), and the next episode resolves it in its first beat. Show emotion
through observable cues (eyes drop, jaw tightens, a delayed answer), not emotion labels.

**Acting fields (this is what makes the voices sound acted):**
- `line`: the clean subtitle text.
- `say`: what the actor actually says, with the rhythm in the punctuation: `……` for a break, `——` for a cut-off,
  `！`/`？！` for force, repeated syllables for stammers (「求……求您。」). Same words as `line`.
- `act`: a Chinese performance direction for the voice, specific and physical: 「压着哭腔，声音发抖，最后两个字几乎说不出口」,
  「冷笑一声，一字一顿」, 「急促喘息，声音拔高，快要失控」. Not just 「生气」.
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
python -m sheelshort assemble <project> E01   # master.mp4 (+ master_clean.mp4): RTX 1080p upscale, subtitles, name cards, music, -14 LUFS
python -m sheelshort review <project> E01     # edit/E01/review.html
python -m sheelshort package <project>        # delivery/: episodes, clean cuts, SRTs, cover, thumbnails, metadata
```
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
- **Servers are on demand:** with `[fleet] core` set, every server is off while nothing renders. The first GPU job
  of a command powers one on, which takes about 1–3 min: a quiet start is not a hang. The render adds more while clips
  wait. Servers power off about 10 min after the last command ends. `python -m sheelshort fleet <project>` shows each
  server and which run uses it, and `... fleet <project> off` powers off every server no run uses.
- Never change a tested default without an A/B; report what you changed and why.
