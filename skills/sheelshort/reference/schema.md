# sheelshort project files

A complete worked example is `projects/guilai/` (bible.json, episodes/E01.json).

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
| `composition`, `action`, `camera`, `sound`, `end` | picture prompt text; `{charid}` and `{loc}` become H3 subject labels |
| `props` | props visible in the cut (must exist in bible.props) |
| `dur` | seconds (default 3); dialogue cuts are lengthened to fit the acted line |

Episode (or clip) field `blocking`: `{"<location>": {"<charid>": "left" | "right"}}` fixes which side of the line of
action each character stands on; by default the people who talk to each other most are put on opposite sides.

## Outputs
`assets/` reference pack · `voices/` cast voices · `perform/<EP>/` takes, choice.json, timing.json, one track per clip ·
`clips/<EP>/` H3 takes · `edit/<EP>/` master.mp4, subtitles.ass/.srt, edl.json, review.html · `state.json` jobs.
