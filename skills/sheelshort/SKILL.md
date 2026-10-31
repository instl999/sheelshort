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

**Shot grammar (enforced by `check`):** speaking character → `CU`; two-person dialogue → `OTS` (`over` = whose
shoulder, never the speaker); positioning and movement → `MS`; reactions and actions → `CU`; inner voice → `VO`
(close-up pushing in, lips closed). Dialogue never in an `MS`. One line per cut, one speaker per cut.

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
python -m sheelshort check <project>          # fix every rule it reports before spending GPU time
python -m sheelshort cast <project>           # close-ups, three-view sheets, props, plates, cast voices
python -m sheelshort perform <project> E01    # only with render.dialogue = "acted": Drama 3 acted takes + dialogue tracks
python -m sheelshort render <project> E01     # H3 renders each clip and voices the lines (default) from the cast voices
python -m sheelshort assemble <project> E01   # master.mp4: clean dialogue, subtitles, music bed, -14 LUFS
python -m sheelshort review <project> E01     # edit/E01/review.html
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
- **Picture:** sample frames of each clip (ffmpeg `-vf fps=1`) and look: faces match the close-ups, the right
  person speaks, no extra people, no on-screen text, mouths move on the lines. A failed clip:
  `render ... --only K2 --takes 2`, then choose the take in `edit/<EP>/picks.json` (`{"K2": 2}`) and re-assemble.
- Never change a tested default without an A/B; report what you changed and why.
