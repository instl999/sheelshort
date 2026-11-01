"""Editorial sound effects: the boom on a reveal, the whoosh into a time jump, the ding of a message the viewer must
notice. Non-diegetic accents laid on story beats by the edit, on top of the dialogue, foley and music.

A cut asks for one with `"sfx": "hit"` (or a list, or `{"cue": "ding", "at": 1.2}` for a moment inside the cut). The cue
lands on the cut's first frame as detected in the render (a riser ends on it, a whoosh peaks on it). The library is
a fixed set of CC0 Freesound clips (no credit needed), picked 2026-10-31 by downloads and ratings (sound skill,
github.com/instl999/sound) and measured with ffmpeg for where the hit sits in each file. They are fetched once into
.cache/sfx/. A bible may add its own cues: `"sfx": {"name": {"file": "inputs/x.mp3", "sync": 0.1, "level": -4}}`.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from . import h3prompt, media
from .project import log

REPO = Path(__file__).resolve().parent.parent
CACHE = REPO / ".cache" / "sfx"

# sync: seconds into the file that land on the cue time; len: seconds used; level: dB against the dialogue;
# bed: lasts as long as its cut (fades out at the cut's end)
CUES = {
    "hit": {"fs": 816376, "url": "816/816376_9839964", "sync": 0.0, "len": 2.8, "level": 0,
            "what": "deep trailer boom: a reveal, an identity exposed, the cliffhanger face"},
    "drop": {"fs": 428073, "url": "428/428073_4067257", "sync": 0.08, "len": 3.6, "level": -3,
             "what": "sub-bass drop: dread, the floor falling away"},
    "sting": {"fs": 183500, "url": "183/183500_2337290", "sync": 0.0, "len": 1.7, "level": -4,
              "what": "minor piano chord hit: a sad or bitter realisation"},
    "riser": {"fs": 840719, "url": "840/840719_17848526", "sync": 2.95, "len": 3.2, "level": -6,
              "what": "tension riser that ends on the cut it is on: lead into a reveal"},
    "whoosh": {"fs": 683101, "url": "683/683101_6253486", "sync": 0.22, "len": 1.0, "level": -6,
               "what": "quick whoosh peaking on the cut: a time jump, flashback, fast push-in"},
    "heartbeat": {"fs": 688735, "url": "688/688735_10403690", "sync": 0.0, "len": 9.9, "level": -10, "bed": True,
                  "what": "heartbeat under the whole cut: suspense, someone about to be caught"},
    "slap": {"fs": 564230, "url": "564/564230_10435241", "sync": 0.48, "len": 1.0, "level": 0,
             "what": "hard face slap (put it at the frame the hand lands with `at`)"},
    "gasp": {"fs": 324898, "url": "324/324898_2104797", "sync": 0.82, "len": 3.0, "level": -6,
             "what": "a crowd gasping in shock: onlookers at a reveal"},
    "ding": {"fs": 760369, "url": "760/760369_8331855", "sync": 0.03, "len": 0.6, "level": -4,
             "what": "message received: a text or notification the viewer must notice"},
    "buzz": {"fs": 476836, "url": "476/476836_1481531", "sync": 0.09, "len": 0.66, "level": -6,
             "what": "phone vibrating"},
    "cash": {"fs": 209578, "url": "209/209578_2558531", "sync": 0.09, "len": 1.6, "level": -4,
             "what": "cash register: money arriving, a transfer, a payout"},
    "shutter": {"fs": 520684, "url": "520/520684_10067806", "sync": 0.84, "len": 1.2, "level": -6,
                "what": "camera shutter: a photo taken as evidence, paparazzi"},
    "glass": {"fs": 221528, "url": "221/221528_1038806", "sync": 0.34, "len": 1.5, "level": -2,
              "what": "glass shattering"},
    "thunder": {"fs": 399656, "url": "399/399656_2341984", "sync": 0.19, "len": 6.0, "level": -6,
                "what": "close thunder clap: a storm night, a shock"},
    "boing": {"fs": 731262, "url": "731/731262_15869491", "sync": 0.0, "len": 1.7, "level": -4,
              "what": "cartoon boing: a comic beat (bright comedies only)"},
}
SNAP_S = 0.5            # a cue on a cut's first frame moves to the render's actual cut within this distance
PER_SECONDS = 10.0      # more than one cue every this many seconds and nothing stands out
_lock = threading.Lock()


def own(scene: dict) -> dict:
    """The bible's own cues."""
    return scene["sfx"] if isinstance(scene.get("sfx"), dict) else {}


def library(scene: dict) -> dict:
    return {**CUES, **own(scene)}


def specs(cut: dict) -> list[dict]:
    """The cues a cut asks for, as {"cue", "at"} (at: seconds into the cut, None = its first frame)."""
    raw = cut.get("sfx")
    raw = raw if isinstance(raw, list) else [raw] if raw else []
    return [{"cue": r, "at": None} if isinstance(r, str) else {"cue": r.get("cue"), "at": r.get("at")} for r in raw]


def check(scene: dict) -> list[str]:
    errs, lib = [], library(scene)
    for name, v in own(scene).items():
        if not v.get("file"):
            errs.append(f"sfx {name}: a bible cue needs `file` (a sound in the project, e.g. inputs/{name}.mp3)")
    for clip in scene["clips"]:
        for i, cut in enumerate(clip["cuts"], 1):
            for s in specs(cut):
                if s["cue"] not in lib:
                    errs.append(f"{clip['id']} cut {i}: sound effect {s['cue']!r} not one of {sorted(lib)}")
                if s["at"] is not None and not 0 <= s["at"] < h3prompt.cut_len(cut):
                    errs.append(f"{clip['id']} cut {i}: sfx at {s['at']} s is outside the cut")
    return errs


def note(scene: dict) -> list[str]:
    n = sum(len(specs(c)) for cl in scene["clips"] for c in cl["cuts"])
    dur = sum(h3prompt.clip_len(cl) for cl in scene["clips"])
    if n > max(1, dur / PER_SECONDS):
        return [f"{n} sound effects in {dur:.0f} s; keep them for the beats that turn the story (at most one every "
                f"~{PER_SECONDS:.0f} s), or none of them lands"]
    return []


def events_in(p, clip: dict, t0: float, head: float, tail: float, video: Path) -> list[dict]:
    """Master-timeline cues of one clip; t0 is the master time of the clip's (untrimmed) frame 0."""
    out, at, cuts = [], 0.0, None
    for cut in clip["cuts"]:
        dur = h3prompt.cut_len(cut)
        for s in specs(cut):
            if s["at"] is None:
                cuts = media.shot_cuts(video) if cuts is None else cuts
                near = [c for c in cuts if abs(c - at) <= SNAP_S]
                x = min(near, key=lambda c: abs(c - at)) if near and at > 0 else at
            else:
                x = at + s["at"]
            x = max(x, head)
            if x < tail:
                out.append({"cue": s["cue"], "start": round(t0 + x, 3), "dur": round(min(dur, tail - x), 3)})
        at += dur
    return out


def _download(url: str, f: Path) -> bool:
    for n in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "sheelshort"})
            with urllib.request.urlopen(req, timeout=60) as r:
                f.with_suffix(".part").write_bytes(r.read())
            f.with_suffix(".part").replace(f)
            return True
        except OSError as e:
            if n == 2:
                log(f"sfx: {url} download failed ({e})")
                return False
            time.sleep(2 + 2 * n)
    return False


def path(p, cue: str) -> Path | None:
    """The cue's sound file: a bible cue from the project (fetched again from its source if the file is missing, since
    project media are not committed), a library cue fetched once from Freesound."""
    v = library(p.bible)[cue]
    f = p.root / v["file"] if v.get("file") else CACHE / f"{cue}_{v['fs']}.mp3"
    url = v.get("url", "")
    url = url if url.startswith("http") else f"https://cdn.freesound.org/previews/{url}-hq.mp3" if url else ""
    with _lock:
        if not f.exists():
            f.parent.mkdir(parents=True, exist_ok=True)
            if not url or not _download(url, f):
                log(f"sfx {cue}: no sound file; left out")
                return None
    return f


# --- choosing new cues with the sound skill (tools/install_sound_skill.py) ---

SKILL = REPO / ".claude" / "skills" / "sound" / "scripts" / "sfx.py"
SKILL_INDEX = Path.home() / ".cache" / "sfx-sources" / "index.json"
CANDIDATES = CACHE / "candidates"


def measure(f: Path) -> dict:
    """Where the sound sits in its file, for a cue that cannot be listened to: the onset (first sound within 20 dB of
    the peak), the peak, the end (last sound within 30 dB of the peak) and the loudness."""
    import numpy as np
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", str(f), "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
                         capture_output=True).stdout
    a = np.frombuffer(raw, np.int16).astype(np.float32) / 32768
    if not len(a):
        return {"dur": 0.0, "onset": 0.0, "peak": 0.0, "end": 0.0, "lufs": None}
    db = 20 * np.log10(np.sqrt(np.convolve(a ** 2, np.ones(160) / 160, "same")[::160] + 1e-9))   # 10 ms frames
    pk = int(db.argmax())
    loud = np.where(db > db[pk] - 30)[0]
    return {"dur": round(len(a) / 16000, 2), "onset": round(int(np.argmax(db > db[pk] - 20)) / 100, 2),
            "peak": round(pk / 100, 2), "end": round(int(loud[-1]) / 100, 2), "lufs": loudness(f)}


def _skill(*args: str) -> str:
    if not SKILL.exists():
        raise SystemExit("the sound skill is not installed: python tools/install_sound_skill.py")
    r = subprocess.run([sys.executable, "-I", str(SKILL), *args], capture_output=True, text=True)
    if r.returncode:
        raise SystemExit((r.stderr or r.stdout).strip() + "\n(Freesound rate-limits bursts: search one query at a time)")
    return r.stdout


def _index() -> dict:
    try:
        return json.loads(SKILL_INDEX.read_text())
    except (OSError, ValueError):
        return {}


def search(query: str, license: str = "cc0", n: int = 6) -> list[dict]:
    """Freesound candidates for a cue, most downloaded first, each downloaded and measured."""
    keys = [ln.split("\t", 1)[0] for ln in _skill("freesound", query, "--license", license, "-n", str(n)).splitlines()
            if ln.startswith("fs:")]
    if not keys:
        return []
    CANDIDATES.mkdir(parents=True, exist_ok=True)
    _skill("get", str(CANDIDATES), *keys)
    idx, out = _index(), []
    for k in keys:
        files = sorted(CANDIDATES.glob(f"fs_{k[3:]}*"))
        files = [f for f in files if not f.name.endswith(".part")]
        out.append({**idx.get(k, {"key": k}), "file": str(files[0]) if files else None,
                    **(measure(files[0]) if files else {})})
    return out


def add(p, name: str, key: str, what: str, sync: str = "onset", level: float = -4.0) -> dict:
    """Make a searched Freesound clip one of the project's cues: copied to inputs/sfx/, measured, and written into the
    bible's `sfx` with its source and licence (an Attribution clip is credited in the delivery)."""
    import shutil
    r = _index().get(key)
    if not r:
        raise SystemExit(f"{key}: not in the search results; search for it first")
    lic = r.get("license", "?")
    if lic not in ("CC0", "BY"):
        raise SystemExit(f"{key} is {lic}; only CC0 and Attribution clips may go into a drama that is sold")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", name) or name in CUES:
        raise SystemExit(f"cue name {name!r}: use a new lower-case name (the library already has {sorted(CUES)})")
    files = [f for f in sorted(CANDIDATES.glob(f"fs_{key[3:]}*")) if not f.name.endswith(".part")]
    if not files:
        CANDIDATES.mkdir(parents=True, exist_ok=True)
        _skill("get", str(CANDIDATES), key)
        files = [f for f in sorted(CANDIDATES.glob(f"fs_{key[3:]}*")) if not f.name.endswith(".part")]
    dest = p.d("inputs", "sfx") / f"{name}{files[0].suffix}"
    shutil.copyfile(files[0], dest)
    m = measure(dest)
    at = {"onset": m["onset"], "peak": m["peak"], "end": m["end"]}.get(sync)
    at = float(sync) if at is None else at
    cue = {"file": str(dest.relative_to(p.root)), "url": r["url"], "source": f"freesound.org/s/{key[3:]}",
           "title": r.get("title", ""), "author": r.get("author", ""), "license": lic, "what": what,
           "sync": at, "len": round(min(m["dur"], max(m["end"], at) + 0.3), 2), "level": level}
    b = p.bible
    b.setdefault("sfx", {})[name] = cue
    from .project import save_json
    save_json(p.root / "bible.json", b)
    return cue


def credits(p) -> list[str]:
    """Credit lines for the Attribution-licensed cues the episodes use."""
    used = {s["cue"] for ep in p.episodes() for cl in p.episode(ep)["clips"] for c in cl["cuts"] for s in specs(c)}
    return [f"\"{v.get('title') or n}\" by {v.get('author', '?')} ({v.get('source', '')}), CC BY 4.0"
            for n, v in own(p.bible).items() if n in used and v.get("license") == "BY"]


def loudness(f: Path) -> float | None:
    """Integrated loudness (LUFS) of a sound file; None when it is silent."""
    r = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(f), "-vn", "-af", "ebur128", "-f", "null", "-"],
                       capture_output=True, text=True).stderr
    m = re.findall(r"I:\s+(-?[0-9.]+) LUFS", r)
    return float(m[-1]) if m and float(m[-1]) > -69 else None


def _cached_loudness(f: Path) -> float | None:
    db, key = CACHE / "loudness.json", f"{f.resolve()}:{f.stat().st_mtime_ns}"
    with _lock:
        known = json.loads(db.read_text()) if db.exists() else {}
        if key not in known:
            known[key] = loudness(f)
            CACHE.mkdir(parents=True, exist_ok=True)
            db.write_text(json.dumps(known))
        return known[key]


def program_loudness(sources: list[tuple[Path, float]]) -> float:
    """The dialogue's loudness over the episode (an energy average of each clip's track, weighted by its length)."""
    import math
    num = den = 0.0
    for f, secs in sources:
        lv = loudness(f)
        if lv is not None:
            num, den = num + secs * 10 ** (lv / 10), den + secs
    return 10 * math.log10(num / den) if den else -23.0


def mix(p, plan: dict, ins: list[str], filt: list[str], main: str, sources: list[tuple[Path, float]]) -> str:
    """Add the plan's cues to the filter graph on top of `main`; returns the label of the result."""
    lib, ref, labels = library(p.bible), None, []
    for j, ev in enumerate(plan.get("sfx", [])):
        v, f = lib[ev["cue"]], path(p, ev["cue"])
        lv = _cached_loudness(f) if f else None
        if lv is None:
            continue
        ref = program_loudness(sources) if ref is None else ref
        sync = float(v.get("sync", 0.0))
        size = media.probe(f)["duration"]
        length = min(size, ev["dur"] + sync) if v.get("bed") else min(size, float(v.get("len", size)))
        skip = max(0.0, sync - ev["start"])          # a riser that would start before the episode does
        length -= skip
        if length <= 0.1:
            continue
        fade = min(0.4, length / 3)
        gain = ref + float(v.get("level", -4)) - lv
        k = ins.count("-i")
        ins += ["-i", str(f)]
        filt.append(f"[{k}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=start={skip:.3f}:duration={length:.3f},"
                    f"asetpts=PTS-STARTPTS,afade=t=in:d=0.01,afade=t=out:st={length - fade:.3f}:d={fade:.3f},volume={gain:.1f}dB,"
                    f"adelay=delays={round(1000 * max(0.0, ev['start'] - sync))}:all=1[fx{j}]")
        labels.append(f"[fx{j}]")
    if not labels:
        return main
    filt.append(f"{main}{''.join(labels)}amix=inputs={len(labels) + 1}:normalize=0:duration=first[withfx]")
    return "[withfx]"
