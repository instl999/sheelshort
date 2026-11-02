# Owner audio library: BGM and editorial effects

Repository: **[instl999/short-drama-audio-library](https://github.com/instl999/short-drama-audio-library)** (private).
Use it first when selecting existing BGM or editorial effects, unless the user requests another source. The library
is separate from sheelshort: reference it, search it, and download selected recordings without copying the full
collection into sheelshort's tracked files. Git LFS audio downloads use the owner's storage/bandwidth quota.

## 1. Open the catalog without downloading the whole library

Use an existing library checkout when available. Otherwise authenticate with an account that can access the private
repository, install Git LFS, and clone with audio downloads disabled. These examples run from the sheelshort root
and put the library next to it; substitute the actual checkout location in later commands.

POSIX shell:

```sh
git lfs install
GIT_LFS_SKIP_SMUDGE=1 git clone https://github.com/instl999/short-drama-audio-library.git ../short-drama-audio-library
```

PowerShell (restore the prior setting after the clone):

```powershell
git lfs install
$audioPreviousSmudge = $env:GIT_LFS_SKIP_SMUDGE
try {
    $env:GIT_LFS_SKIP_SMUDGE = '1'
    git clone https://github.com/instl999/short-drama-audio-library.git ../short-drama-audio-library
} finally {
    $env:GIT_LFS_SKIP_SMUDGE = $audioPreviousSmudge
}
```

`catalog.json`, `catalog.csv`, and `tools/find_audio.py` are ordinary Git files, available before any audio is fetched.
If access fails, report the authentication/access problem; do not treat the library as public or invent an asset URL.

## 2. Search by English mood and sound function

The catalog search requires Python 3.11+ and no additional Python packages:

```sh
python ../short-drama-audio-library/tools/find_audio.py --kind bgm --mood suspense --limit 5 --json
python ../short-drama-audio-library/tools/find_audio.py --kind bgm --mood warm --limit 5 --json
python ../short-drama-audio-library/tools/find_audio.py --kind sfx --mood suspense --limit 5 --json
python ../short-drama-audio-library/tools/find_audio.py --kind sfx --tag whoosh --limit 5 --json
```

Searches combine filters. If a narrow search returns nothing, broaden it or inspect the catalog's available tags.
Select from actual returned records, using `id`, `path`, `title`, `moods`, `tags`, `duration_seconds`, `sha256`,
`sources` and license information. Never invent a filename or assume every possible effect is present.

| Story beat | Starting mood search |
| --- | --- |
| A secret, a reveal, someone about to be caught | `suspense`, `tense`, `mysterious` |
| Loss, regret, isolation | `sad`, `negative`, `nostalgic` |
| Reconciliation or an intimate moment | `warm`, `healing`, `romantic` |
| A comic or playful beat | `funny`, `playful`, `cheerful` |
| A heroic entrance or climactic action | `epic`, `heroic`, `grand` |

For effects, also match the actual sound source/function (`riser`, `whoosh`, `impact`, `transition`, etc.) and the
duration. Mood alone is insufficient: a horror sting cannot stand in for a door closing. Multiple moods can belong
to one asset; the physical directory uses only the alphabetically first mood. Search the catalog rather than
guessing its folder. Labels come from the source collection and are not proof that a track has no vocals or that
its mood was auditioned. Check the chosen audio before final assembly when playback is available.

## 3. Fetch and verify only selected files

Replace both placeholders below with values from the same returned record. `path` is relative to the library root,
for example a path beginning with `bgm/` or `sfx/`. Use the exact path, not an inferred mood folder.

```sh
git -C ../short-drama-audio-library lfs pull --include="<catalog-path>" --exclude=""
python ../short-drama-audio-library/tools/find_audio.py "<catalog-id>" --verify
```

Require a real result and `Verified 1/1 assets`. `Verified 0/0` is not success for a selected asset. A file containing
`version https://git-lfs.github.com/spec/v1` is an unhydrated pointer, not playable audio; fetch it before use.
Avoid a bare `git lfs pull`, which downloads the entire collection. Keep the original extension and recording bytes.

## 4. Connect BGM to the project

Resolve the selected file to its absolute local path. Merge this setting into `projects/<project>/sheelshort.toml`,
preserving other sections and values. On Windows, use forward slashes in a TOML double-quoted path:

```toml
[edit]
music = "C:/Media/short-drama-audio-library/bgm/<mood>/<chosen-file>.mp3"
```

Use the real catalog file and its actual extension in place of the placeholders. `assemble.run` reads
`Project.cfg["edit"]["music"]`; the episode JSON's `music` field holds an ACE-Step generation prompt, not this path.
`[edit].music` is project-wide, so when episodes need different recordings, set the appropriate path before each
episode's assembly and record that selection per episode. Keep the existing dialogue ducking and `music_under_lu`
settings. If no owner-library track fits, use the existing `music = "auto"` workflow instead of forcing a poor match.

## 5. Connect an editorial effect to a cut

Copy the downloaded file into `projects/<project>/inputs/sfx/`, retaining its extension. Measure it from the sheelshort
root (normal project dependencies and FFmpeg must be installed):

```sh
python -c "import json; from pathlib import Path; from sheelshort.sfx import measure; print(json.dumps(measure(Path('<local-effect-file>'))))"
```

Use the measured onset for an impact, the peak for a whoosh, or the end for a riser that builds into the cue time.
Merge an entry into the bible's top-level `sfx` dictionary, preserving existing cues. This example is illustrative;
replace its file, timing and purpose with the selected recording and measured values:

```json
{
  "sfx": {
    "owner_reveal": {
      "file": "inputs/sfx/chosen-reveal.wav",
      "sync": 0.0,
      "level": -4,
      "what": "Suspense accent on an identity reveal"
    }
  }
}
```

The `file` path is relative to the project root. Then place `"sfx": "owner_reveal"` on a cut, or
`"sfx": {"cue": "owner_reveal", "at": 0.8}` for a moment inside the cut. Keep the existing sound-pass guidance:
editorial accents belong on story turns, not every cut; rendering already supplies diegetic sounds described by `sound`.
The `sfx ... add ... fs:<id>` command is for Freesound candidates and does not import owner-library recordings.

Record the library repository/commit, catalog ID, original title, SHA-256, project destination, selected cue timing
and episode usage in project notes. Preserve the catalog's unknown license status; do not relabel these recordings
as CC0 or licensed Freesound assets. If no suitable recording exists in this library, use the built-in cues or the
existing Freesound search/add workflow described in [SKILL.md](../SKILL.md).
