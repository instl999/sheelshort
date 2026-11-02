# sheelshort project files

A complete worked example is `projects/waimai2/` (bible.json, episodes/E01.json, E02.json).

## bible.json
```json
{
  "title": "归来 (The Return)",
  "language": "zh-CN",
  "style": "The target video is a photorealistic live-action Chinese vertical short drama in 9:16 with cinematic composition, ...",
  "look_preset": "cinematic",
  "characters": {
    "linxia": {
      "name": "Lin Xia", "role": "woman",
      "look": "a 28-year-old Chinese woman with an oval face, ..., a small beauty mark under her left eye, ..., wearing a deep-red satin slip evening gown ...",
      "keep": "beauty mark under the left eye, long wavy dark-brown hair with the right side part, rose-red lip makeup, deep-red satin slip gown and pearl stud earrings",
      "face_detail": "long curled eyelashes, a small beauty mark under the left eye, fine skin texture ...",
      "hair": "long wavy dark-brown hair falling past the shoulders with a side part on her right, worn loose",
      "costume": "a deep-red satin slip evening gown with thin straps, ankle length, black pointed-toe high heels, small pearl stud earrings",
      "voice_en": "in a warm, slightly low female voice",
      "demeanor": "composed and warm, attentive eyes, a quiet strength; never sarcastic, never contemptuous",
      "voice_design": "二十八岁左右的成年女性，音色温暖、略偏低沉，普通话标准，语速中等",
      "voice_sample": "我等了你整整三年，每天晚上都站在这扇窗前，看着这座城市的灯一盏一盏地熄灭。"
    }
  },
  "locations": {
    "penthouse": {"name": "penthouse living room", "name_zh": "顶层公寓", "look": "...", "keep": "...", "time": "night",
                  "light": "night; warm brass table lamps as the key light against the cool blue night skyline"}
  },
  "props": {
    "wine_glass": {"name": "crystal wine glass", "look": "...", "keep": "..."}
  }
}
```

A character may have `outfits` for costume changes: `"outfits": {"suit": {"look": "...the whole look sentence in this
outfit...", "keep": "...anchors + this outfit...", "costume": "a charcoal three-piece suit, white shirt, black tie"}}`.
The outfit is the same person (name, voice, face): `cast` re-dresses the character's close-up with the image editor
and makes a sheet from it. Never make a second character for a change of clothes.

`sfx` (optional) holds the project's own sound-effect cues, written by `python -m sheelshort sfx <project> add`:
`{"door_slam": {"file": "inputs/sfx/door_slam.mp3", "url": "...", "source": "freesound.org/s/571797", "license": "CC0",
"what": "...", "sync": 1.01, "len": 1.67, "level": -4}}`. `sync` is the second of the file that lands on the cue time.

A location may have `crowd: true` (a restaurant, a lobby, a street: passers-by may fill the background) and `lights`
for other times of day: `"lights": {"night": "night; warm floor lamps as key, the city glowing blue through the
window"}`. A clip (or episode) chooses one with `"time": "night"`; `cast` relights the location's plates with the
image editor, so it stays the same room. Without `crowd`, a shot holds only its written people (and `extras`).

`look_preset` (optional, default `cinematic`) sets the light, colour and acting of every prompt, the location plates
and the finishing grade: `cinematic` is moody and directional (rural, period, thriller); `bright` is high-key and
clean (comedy, bright urban stories); `glossy` is warm and glowing with soft bokeh (romance). A location's `name_zh`
(optional) is the vertical title shown as a scene there opens.

## episodes/E01.json
```json
{
  "id": "E01", "title": "归来", "logline": "...",
  "clips": [
    {
      "id": "K1", "location": "penthouse",
      "summary": "At night in {loc}, {linxia} turns from the window to face {luchen} ...",
      "soundscape": "Quiet penthouse room tone continues throughout, ...",
      "cuts": [
        {"type": "MS", "subjects": ["linxia", "luchen"], "composition": "...", "action": "...", "camera": "Slow push in, ...",
         "sound": "...", "end": "...", "props": ["wine_glass"]},
        {"type": "CU", "subjects": ["linxia"], "speaker": "linxia",
         "line": "整整三年，我以为你死了。",
         "say": "整整三年……我以为你——死了。",
         "act": "压着哭腔，声音发抖，又恨又委屈，最后两个字几乎说不出口",
         "emotion": "broken",
         "delivery": "low and trembling, holding back anger and tears",
         "composition": "...", "action": "...", "camera": "Static camera ...", "end": "..."}
      ]
    }
  ]
}
```

| cut field | meaning |
|---|---|
| `type` | `WS` wide (geography), `MS` medium, `MCU` medium close-up (mid-chest up, may carry a line), `CU` close-up, `ECU`, `OTS` over-the-shoulder, `VO` inner voice (close-up pushing in), `RX` reaction while the previous line continues |
| `subjects` | characters (and only they) visible in the cut |
| `to` | optional: whom the subject looks at or addresses (default: the nearest other speaker in the clip) |
| `angle` | optional camera angle: `front` (default), `three-quarter`, `profile`, `high`, `low`. A changed angle makes a cut to the same person at the same size a real cut |
| `speaker` / `over` | who speaks; whose shoulder is in the foreground of an OTS |
| `line` / `say` / `act` / `emotion` / `delivery` | subtitle text / acted wording / Chinese voice direction / scoring class / English face direction |
| `composition`, `action`, `camera`, `sound`, `end` | picture prompt text; `{charid}` and `{loc}` become H3 subject labels. All but `sound` are required and checked for completeness (SKILL.md: every cut written out in full) |
| `props` | props visible in the cut (must exist in bible.props) |
| `dur` | seconds (default 3); dialogue cuts are lengthened to fit the acted line |
| `via` | `"phone"`: the line is heard through a phone from the other end; the shot shows the listener (the speaker is not in `subjects`), and the edit puts the line in the telephone band |
| `extras` | optional background people who never speak (`"two lawyers in black suits behind {chenmo}"`); not in close-ups. Without it a shot holds only its `subjects`, and clip QC re-takes a close or medium shot with a crowd in it |
| `sfx` | optional sound effect(s) on the cut's first frame: a cue name (`"hit"`), a list, or `{"cue": "slap", "at": 0.8}` for a moment inside the cut |
| `knows` | optional: facts (the bible's `facts`) the speaker's line depends on; the speaker must have learned them (ledger) |

Episode (or clip) field `outfits`: `{"<charid>": "<outfit>"}` dresses a character in one of their bible `outfits` for the
episode (a clip's entry overrides it; `"default"` is the character's own costume).

Episode fields for the season ledger (`ledger.py`; SKILL.md §3):

| episode field | meaning |
|---|---|
| `when` | the story day and time the episode starts ("第3天 傍晚") |
| `jump` | time has passed since the episode before ("三天后"); without it the episode continues straight on |
| `picks_up` | how the episode opens on the previous episode's cliffhanger, when its first clip shows other people |
| `changes` | what the story changes, each after a clip (`"after": "K3"`) or at the end: `{"who", "injury": {part: what is seen}}`, `{"who", "heals": [part]}`, `{"who", "learns": [fact]}`, `{"who", "carries" / "drops": [prop]}`, `{"prop", "where", "holder"}`, `{"prop", "gone": "how"}`, `{"prop", "back": "how"}` |

Bible field `facts`: `{"<fact>": "what it is"}`, the secrets characters learn. Project file `arc.json`:
`{"episodes": [{"ep", "hook", "conflict", "shape", "turn", "cliffhanger", "paywall"}]}`, one row per episode.

Episode (or clip) field `blocking`: `{"<location>": {"<charid>": "left" | "right"}}` fixes which side of the line of
action each character stands on; by default the people who talk to each other most are put on opposite sides.

## Outputs
`assets/` reference pack · `voices/` cast voices · `perform/<EP>/` takes, choice.json, timing.json, one track per clip ·
`clips/<EP>/` H3 takes · `edit/<EP>/` master.mp4, subtitles.ass/.srt, edl.json, review.html · `state.json` jobs.
