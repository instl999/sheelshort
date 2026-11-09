# Example projects

Each folder is one sheelshort project: `bible.json` (cast, locations, look) and `episodes/<EP>.json`, written by the
agent from a script (`skills/sheelshort/SKILL.md`), plus what the engine made from them. They are examples: to demo the
engine, and to copy from when a real series starts. A real series gets its own project:
`python -m sheelshort new <name> --script <file>`.

| project | what it shows |
|---|---|
| `lan/` | **蓝城Yunjing**: a location pack built from a real property's three films (22 rooms, day and night), with the 58 s showcase episode Yunjing (D01) and its cast. Every asset is committed: the best place to start a demo or a series set at the property. |
| `waimai2/` | 《雨夜外卖》: two episodes written by a fresh agent from the skill alone (the end-to-end test): costume change, phone call, 60-100 s episodes. |
| `quancun/` | 全村抢: an 82-集 village comedy script; episodes E01-E04 adapted one 集 each. |
| `guilai/` | 归来: the first worked example (one scene). |

Only `bible.json` and `episodes/` are kept in git for most projects; `lan/` keeps all of its assets.
