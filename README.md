# sheelshort

Script in, finished vertical short-drama (短剧) episodes out, on your own ComfyUI servers.

An agent (Claude Code with `skills/sheelshort/SKILL.md`) adapts the script into a series bible and episode files.
The `sheelshort` engine then:
1. **casts** each character (close-up, three-view sheet, voice);
2. **renders** each clip with MiniMax H3 from those locked references, with H3 voicing the lines;
3. **assembles** the episode: script subtitles timed to the heard speech, an ACE-Step music bed, −14 LUFS.

```
python -m sheelshort new myshow --script script.txt   # the agent then writes bible.json + episodes/E01.json
python -m sheelshort check myshow                     # shot grammar, line lengths, missing fields (no GPU)
python -m sheelshort make myshow E01 --seconds 20     # fast test: only the opening ~20 s, finished
python -m sheelshort make myshow E01                  # the whole episode -> projects/myshow/edit/E01/master.mp4
python -m sheelshort review myshow E01                # edit/E01/review.html: masters, takes, per-cut frames
```

## How it works
- **Identity comes from locked references, never from re-description** (M0 spike, `docs/DESIGN.md` §0.6). Each
  character's close-up and three-view sheet, plus the props and the location plate, go to H3 as `<Picture N>`
  references in every clip. Each speaker's voice clip goes in as `<Audio N>`.
- **Prompts follow MiniMax's official H3 format**: `[Shot N] At mm:ss` cuts, `(Sx)` speakers, and lines in
  `<d>[Chinese] …</d>`. The rhythm stays in the punctuation (`say`: `……`, `——`, `！`), voice definitions describe
  timbre only, and each line carries an acting `delivery`. These are drama-studio's fixes for flat delivery
  (`docs/DESIGN.md` §0.7).
- **Dialogue modes** (`render.dialogue`, `render.voice_ref` in `sheelshort.toml`):
  - `h3` + `cast` (**default**): H3 voices the lines from the cast voice clips. The owner preferred this to the
    performed track: the voices sound more natural, nothing gets cut off, and the U22 look is more realistic.
  - `h3` + `acted` (experiment): H3 still voices the lines, but each speaker's `<Audio N>` is made of acted Drama 3
    takes of their own lines in the clip (`perform` first), so the reference carries the delivery.
  - `acted`: the performed Drama 3 track is locked into the render (T8 audio lock, U24 sampler) and used as the
    final dialogue, with MMAudio foley underneath.
- **Resumable everywhere.** Every GPU job's server and prompt id is saved before waiting. Calls through the servers'
  tunnel retry, queueing is idempotent, renders are fingerprinted, and a changed input re-renders only that clip.

## Autoscaling (AutoDL)
The servers are AutoDL.Art instances of one app, so `[fleet]` in `sheelshort.toml` can add identical servers during a
render: when clips wait beyond what the pool holds, it powers on a stopped `sheelshort-bj-*` instance or creates one
(Beijing only, 5090 first), adds it to the pool once ComfyUI answers, and powers it off when idle and at the end of the
run (also after a crash). It never touches other instances. Start with `dry_run = true`.
`python -m sheelshort fleet <project>` lists the managed servers; `... fleet <project> off` powers them all off.
Token: `$AUTODL_TOKEN` or `~/.config/sheelshort/autodl_token` (chmod 600).

## Layout
| path | what |
|---|---|
| `sheelshort/` | the engine: `cli.py` entry; `cast`, `perform`, `render`, `assemble`, `review`, `evaluate` stages; `comfy` and `flows` drive the servers |
| `sheelshort/workflows/` | the servers' own workflows in API format (Z-Image C01, Qwen edit Y01, Qwen3-TTS N06, H3 U22, U24-T8) |
| `skills/sheelshort/` | the agent playbook and file schema |
| `projects/<name>/` | one series. Only `bible.json` and `episodes/` are committed; generated media stays local |
| `tests/test_offline.py` | offline tests (no GPU) |
| `docs/DESIGN.md` | design concept, M0 spike and dialogue A/B results |
| `spike/` | M0 spike (variants A–G) |

## Setup
Python 3.12: `uv venv && uv pip install -r requirements.txt`. The `imageio-ffmpeg` binary (with libass) is used when
there is no system ffmpeg. Copy `sheelshort.example.toml` to `sheelshort.toml` and set the servers. They need MiniMax
H3, the T8 H3 audio nodes, ComfyUI-Drama3, TD-Qwen3TTS, MMAudio, ACE-Step, Z-Image and Qwen-Image-Edit;
`python -m sheelshort doctor <project>` checks them.
