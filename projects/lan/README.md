# 蓝城雲境: location pack

The short drama is set in a high-end residential development, 蓝城雲境. The property's three official films are the
source for every location in it:

| film | what it shows |
|---|---|
| `films/flat170.mp4` | the 170 m² show flat, with an 8.4 m-wide living room (vertical, daylight, 1:24) |
| `films/clubhouse.mp4` | the clubhouse: entrance, lobby, corridors, function rooms, pool and gym (landscape, day into evening, 4:12) |
| `films/garage.mp4` | the underground garage and its lift lobby (landscape, 0:30) |

This project lives on the `lan` branch only. Everything for this drama (films, location pack, plates, scripts) is
committed here; `main` is untouched.

## Locations

`locations/locations.json` is the plan: the locations picked from the films, which frames to use, the crop, and the
description of each place (look, keep, time, light) that is copied into `bible.json`.

| id | place | angles | story use |
|---|---|---|---|
| `flat_foyer` | 样板间·玄关 (show-flat entrance foyer) | 1 | arrivals and exits, a visitor at the door, the owner coming home |
| `flat_living` | 样板间·客厅 (show-flat living room) | 3 | the main stage: viewings, family confrontations, negotiations over the sale, a party |
| `flat_kitchen` | 样板间·西厨岛台 (show-flat open kitchen island) | 2 | late-night talks over a drink, a couple's quarrel, a breakfast scene |
| `flat_master` | 样板间·主卧 (show-flat master bedroom) | 2 | private moments, a secret discovered, a night-time phone call |
| `flat_closet` | 样板间·衣帽间 (show-flat walk-in closet) | 1 | dressing for the big night, finding a hidden bag or document |
| `flat_guestroom` | 样板间·次卧 (show-flat second bedroom) | 1 | a guest who stays too long, a family member's room |
| `flat_bath` | 样板间·主卫 (show-flat master bathroom) | 1 | crying alone, washing off a slap, overheard phone call |
| `club_entrance` | 会所·入口 (clubhouse main entrance) | 2 | arriving in a luxury car, a dramatic entrance, waiting outside in the rain |
| `club_lobby` | 会所·大堂 (clubhouse grand lobby) | 2 | the property's showpiece: the sales launch, a public humiliation, a reunion across the hall |
| `club_lounge` | 会所·大堂洽谈区 (lobby lounge) | 2 | the sales consultant's table: price negotiations, signing the contract, a rival buyer |
| `club_corridor` | 会所·电梯厅走廊 (lift lobby corridor) | 2 | a chance meeting at the lift, a chase, a whispered threat |
| `club_library` | 会所·书吧茶室 (clubhouse tea and reading room) | 2 | a quiet business deal over tea, an elder's summons, a blackmail meeting |
| `club_vip` | 会所·VIP会客室 (VIP reception room) | 1 | a private meeting with the developer's boss, an engagement proposal |
| `club_stairs` | 会所·楼梯间 (clubhouse stairwell) | 1 | a secret phone call, hiding from someone, a confession away from the crowd |
| `club_hall` | 会所·大厅吧台 (clubhouse great hall and bar) | 3 | the owners' evening party, a toast gone wrong, two rivals at the bar |
| `club_billiards` | 会所·台球室 (billiards room) | 1 | rich men talking business over a game, a bet |
| `club_banquet` | 会所·宴会包间 (private banquet room) | 1 | the family banquet: seating fights, an announcement, a slap at the table |
| `club_media` | 会所·影音室 (private media lounge) | 1 | a hidden recording played on the big screen, friends' night out |
| `club_pool` | 会所·室内泳池 (indoor swimming pool) | 2 | a confrontation by the water, someone pushed in, a quiet night swim |
| `club_gym` | 会所·瑜伽健身房 (yoga and fitness studio) | 1 | the rich wives' yoga class, gossip that sets off the plot |
| `garage_drive` | 地库·车道 (underground garage driveway) | 2 | an ambush beside the car, a tail, a confrontation in the car park |
| `garage_lobby` | 地库·入户大堂 (garage lift lobby) | 2 | waiting for the lift, someone blocking the way home |

Skipped: the kids' room, the window views (building sites), the floor plan card, the hallway between rooms and the
garage's flooring display board.

## What each location has

```
locations/<id>/src_<n>.jpg        the frame from the film (landscape films also have src_<n>_v.jpg, the vertical 9:16 crop)
locations/<id>/ref_<n>.png        reference plate: a clean 9:16 film still of the real place (720x1280)
locations/<id>/whitebox_<n>.png   white-box blockout: the same camera, every surface untextured white clay
locations/<id>/sheet.jpg          contact sheet, one row per angle: frame | plate | white box
locations/overview.jpg            every main plate next to its white box
inputs/loc_<id>.png, loc_<id>_<n>.png   the plates the engine sends to H3 as the location pictures for clips there
```

Plates: people, signs, exit lights, construction views, cables and the cameraman's reflection are removed, the
action-cam lens distortion is straightened, and the window views are replaced (the films show building sites outside).
Architecture, furniture, materials and colours are kept as filmed.

White boxes: the same camera, layout and furniture outlines with all materials stripped. They are references for
staging and camera (where people can stand, what is in frame) and for a later depth or line ControlNet, not pictures
for H3 to copy.

## Regenerating

```bash
.venv/bin/python projects/lan/make_locations.py all                      # frames, plates, white boxes, publish
.venv/bin/python projects/lan/make_locations.py refs --only club_lobby   # one location's plates again (after a "rev" bump)
.venv/bin/python projects/lan/make_locations.py publish                  # inputs/, bible.json, contact sheets
```

Jobs go through the servers like every sheelshort job and resume after a crash. To make a new take of one plate, add
`"rev": 2` to that shot in `locations.json` and run `refs` (then `whitebox`) for that location.

## Next

Characters, props and episodes are not written yet. When the story is written, the episode clips use these location
ids (`"location": "club_lobby"`), and the bible's characters and props are added next to the locations already there.
