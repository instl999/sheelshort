"""蓝城雲境 location pack: source frames from the three property films -> clean vertical reference plates -> white-box
(白模) blockout references, all through the servers' Qwen-Image-Edit 2511 (Y01).

    .venv/bin/python projects/lan/make_locations.py all                  # frames, refs, whitebox, publish
    .venv/bin/python projects/lan/make_locations.py refs --only club_lobby
    .venv/bin/python projects/lan/make_locations.py publish              # inputs/loc_*.png, bible.json, contact sheets

The plan lives in locations/locations.json (which frames, where to crop, what each place looks like). Per location:
  locations/<id>/src_<n>.jpg        the film frame (landscape films: plus src_<n>_v.jpg, the vertical 9:16 crop)
  locations/<id>/ref_<n>.png        clean 9:16 film-still plate of the real place (people, signs, clutter removed)
  locations/<id>/whitebox_<n>.png   the same camera as an untextured white clay blockout
  inputs/loc_<id>.png, loc_<id>_<n>.png   the plates the engine sends to H3 as the location pictures
Jobs are resumable (state.json) like every other sheelshort job; add "rev": 2 to a shot to make a new take.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))

from sheelshort import flows, media  # noqa: E402
from sheelshort.cast import _parallel  # noqa: E402
from sheelshort.project import Project, load_json, log, save_json  # noqa: E402

PLAN = HERE / "locations" / "locations.json"
LONG_SIDE = 1280

REF_PROMPT = (
    "把图1修整成这个真实空间的一张干净的竖构图电影剧照。保持图1中的建筑结构、墙面、天花、地面、门窗、家具、陈设、材质和颜色完全不变，"
    "机位和构图与图1一致。{fill}去掉画面中所有的人物、文字、标识牌、安全出口灯牌、海报文字、施工杂物、电线、梯子、纸箱、清洁工具和镜面里的拍摄者；"
    "校正广角镜头的畸变，竖直线条保持竖直，像用35mm电影镜头在人眼高度拍摄。必须原样保留：{keep}。{outside}光线：{light}。"
    "高级地产宣传片质感，真实材质细节，清晰锐利，没有人物，没有文字，没有水印。"
)
FILL = "图2是同一空间同一时刻的完整横向画面，只用来参考图1取景之外的空间和图1里模糊处的细节；输出保持图1的竖构图取景，画面更清晰。"
WHITEBOX_PROMPT = (   # no list of features to keep here: naming stairs made Qwen add stairs that are not there
    "把图1转换成同一场景的建筑白模渲染（白盒场景参考图）。保持完全相同的机位、镜头透视和构图，图1里的每一面墙、每一根柱子、"
    "每一处天花造型、每一扇门窗，以及每件家具和陈设的位置、大小和轮廓都保持不变；不要增加图1里没有的任何东西，也不要删掉任何东西。"
    "只改材质：所有表面都变成统一的无纹理哑光白色石膏材质，像还没有贴材质的三维白模：没有颜色、没有木纹石纹、没有地面拼花和地面标线、"
    "没有画作内容、没有花朵颜色、没有文字、没有反光。玻璃和窗户变成浅灰色的半透明平板，窗外是纯白色。"
    "柔和均匀的天光加环境光遮蔽，用干净的明暗和阴影表现体块和空间深度。没有人物。"
)


def plan() -> dict:
    return load_json(PLAN)


def shots(pl: dict, only: set[str] | None):
    for loc, v in pl["locations"].items():
        if only and loc not in only:
            continue
        for n, s in enumerate(v["shots"], start=1):
            yield loc, v, n, s


def frames(pl: dict, only: set[str] | None) -> None:
    """Grab each planned frame at full resolution; for a landscape film also cut the vertical 9:16 crop around `x`."""
    for loc, v, n, s in shots(pl, only):
        film = HERE / pl["films"][v["film"]]["file"]
        d = HERE / "locations" / loc
        d.mkdir(parents=True, exist_ok=True)
        src = d / f"src_{n}.jpg"
        media._run(["ffmpeg", "-y", "-v", "error", "-ss", str(s["t"]), "-i", str(film), "-frames:v", "1", "-q:v", "1", str(src)])
        info = media.probe(src)
        w, h = info["width"], info["height"]
        if w > h:   # landscape film: the 9:16 window a vertical camera would see there (padding a wider crop and
            cw = int(h * 9 / 16) // 2 * 2   # letting Qwen extend ceiling and floor reinvented the room; see README)
            x0 = min(max(int(s.get("x", 0.5) * w - cw / 2), 0), w - cw)
            media._run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vf", f"crop={cw}:{h}:{x0}:0", "-q:v", "1",
                        str(d / f"src_{n}_v.jpg")])
        log(f"{loc} #{n}: frame at {s['t']}s ({w}x{h})")


def _key(loc: str, n: int, s: dict, kind: str, landscape: bool) -> str:
    rev = s.get("rev", 1)
    wb = "_wb2" if kind == "whitebox" else ""   # second white-box prompt (the first added stairs)
    return f"loc/{loc}/{kind}_{n}" + ("_v916" if landscape else "") + (f"_r{rev}" if rev > 1 else "") + wb


def ref_prompt(v: dict, landscape: bool) -> str:
    outside = (f"只有图1里本来就看得到窗户或玻璃的地方，{v['outside'].rstrip('。')}；不要把墙面、拱门和走廊改成窗户或玻璃墙。"
               if v.get("outside") else "")
    return REF_PROMPT.format(fill=FILL if landscape else "", outside=outside, light=v["light"], keep=v["keep"])


def make(p: Project, loc: str, v: dict, n: int, s: dict, kinds: tuple[str, ...]) -> None:
    d = HERE / "locations" / loc
    src, vert = d / f"src_{n}.jpg", d / f"src_{n}_v.jpg"
    landscape = vert.exists()
    ref = d / f"ref_{n}.png"
    if "ref" in kinds:
        prompt = ref_prompt(v, landscape)
        (d / f"ref_{n}.prompt.txt").write_text(prompt + "\n", encoding="utf-8")
        key = _key(loc, n, s, "ref", landscape)
        images = [vert, src] if landscape else [src]
        out = p.run_job(key, lambda c: flows.edit(p.wf("edit"), prompt, [p.put(c, x) for x in images], LONG_SIDE, p.seed(key),
                                                   f"sheelshort/lan_{loc}_ref{n}"),
                        p.d("cache", "loc"), key.replace("/", "_"), ("images",), 900)
        if out:
            shutil.copy2(out[0], ref)
    if "whitebox" in kinds and ref.exists():
        key = _key(loc, n, s, "whitebox", landscape)
        out = p.run_job(key, lambda c: flows.edit(p.wf("edit"), WHITEBOX_PROMPT, [p.put(c, ref)], LONG_SIDE, p.seed(key),
                                                   f"sheelshort/lan_{loc}_wb{n}"),
                        p.d("cache", "loc"), key.replace("/", "_"), ("images",), 900)
        if out:
            shutil.copy2(out[0], d / f"whitebox_{n}.png")


def generate(p: Project, pl: dict, only: set[str] | None, kinds: tuple[str, ...]) -> None:
    def one(a):
        try:
            make(p, *a, kinds)
        except Exception as e:  # noqa: BLE001 - a dropped connection loses one plate, not the run; rerun to resume
            log(f"{a[0]} #{a[2]}: FAILED {type(e).__name__}: {str(e)[:300]}")
    _parallel(p, [lambda a=a: one(a) for a in shots(pl, only)])


def publish(p: Project, pl: dict) -> None:
    """Plates into inputs/ (what the engine sends to H3), locations into bible.json, one contact sheet per location
    (rows: film frame, plate, white box) and one overview of every main plate and its white box."""
    inp = p.d("inputs")
    bible = load_json(p.root / "bible.json")
    overview = []
    for loc, v in pl["locations"].items():
        d = HERE / "locations" / loc
        cells = []
        for n, _ in enumerate(v["shots"], start=1):
            ref, wb = d / f"ref_{n}.png", d / f"whitebox_{n}.png"
            if ref.exists():   # a link, so git holds each plate once
                link = inp / (f"loc_{loc}.png" if n == 1 else f"loc_{loc}_{n}.png")
                link.unlink(missing_ok=True)
                link.symlink_to(Path("..") / "locations" / loc / ref.name)
            cells += [x for x in (d / f"src_{n}.jpg", ref, wb) if x.exists()]
            if n == 1 and ref.exists() and wb.exists():
                overview += [ref, wb]
        if cells:
            media.tile(cells, d / "sheet.jpg", cols=3, cell=(360, 640))
        bible["locations"][loc] = {k: v[k] for k in ("name", "name_zh", "look", "keep", "time", "light")}
    save_json(p.root / "bible.json", bible)
    if overview:
        media.tile(overview, HERE / "locations" / "overview.jpg", cols=8, cell=(240, 426))
    log(f"published {len(pl['locations'])} locations to inputs/ and bible.json")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=["frames", "refs", "whitebox", "publish", "all"])
    ap.add_argument("--only", help="comma-separated location ids")
    a = ap.parse_args()
    only = set(a.only.split(",")) if a.only else None
    pl = plan()
    if a.step in ("frames", "all"):
        frames(pl, only)
    if a.step == "frames":
        return
    p = Project(HERE)
    if a.step != "publish":
        fc = p.cfg.get("fleet", {})
        if fc.get("enabled") and fc.get("core") and not fc.get("dry_run"):
            from sheelshort import fleet
            p.waker = fleet.Session(p)
        try:
            generate(p, pl, only, {"refs": ("ref",), "whitebox": ("whitebox",), "all": ("ref", "whitebox")}[a.step])
        finally:
            if p.waker:
                p.waker.release()
    if a.step in ("publish", "all"):
        publish(p, pl)


if __name__ == "__main__":
    main()
