"""Render shot references from a white-mesh (blockout) set. Runs inside Blender:

    blender -b set.blend --factory-startup -P render_shots.py -- job.json
    blender -b --factory-startup -P render_shots.py -- job.json      # job["import"] = .glb/.gltf/.fbx/.obj

For every cut in the job it writes, under job["out"]/<clip>/:
  cut<N>_clay.png    first frame, grey clay (Workbench), keyframe/layout guide
  cut<N>_depth.png   first frame, depth (near = white), for depth ControlNets
  frames/####.png    the whole clip, cut after cut, camera moves included -> animatic
and a manifest.json with each cut's camera, frame range and depth range.

Conventions for the set file:
  - Characters and props are objects named by their scene id (`linxia`, `wine_glass`). Characters face local -Y.
    A character's height is its "height" custom property, else the height of its meshes.
  - Per-clip or per-cut blocking: `linxia@K2` or `linxia@K2.3` replaces `linxia` for clip K2 / cut 3 of K2.
  - A camera named `CAM_<clip>_<cut>` (e.g. CAM_K1_2) is used as-is for that cut. Its animation, if any, uses the
    clip's frame numbers: cut 1 starts at frame 1, at job["fps"].
  - Any other cut gets an automatic camera from the house shot grammar: MS / CU / ECU / OTS / VO, the 180-degree
    rule kept per clip, and the cut's camera move (push_in, pull_out, truck_left/right, pan_left/right,
    orbit_left/right, tilt_up/down, handheld, static).
"""
from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Matrix, Vector

UP = Vector((0, 0, 1))
SENSOR_H = 36.0          # vertical sensor fit: lens values read like full-frame lenses on a 9:16 frame
SHOT = {                 # lens mm, frame height in metres at the subject, aim height as a fraction of body height
    "MS": (35, 1.25, 0.70),
    "CU": (85, 0.55, 0.93),
    "ECU": (100, 0.24, 0.94),
    "OTS": (50, None, 0.93),
    "VO": (85, 0.55, 0.93),
}
MOVE_SCALE = {"small": 0.5, "normal": 1.0, "large": 1.6}


def args() -> dict:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    return json.loads(Path(argv[0]).read_text(encoding="utf-8"))


# ---------- scene setup ----------

def import_set(path: str) -> None:
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)
    ext = Path(path).suffix.lower()
    if ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path)
    elif ext == ".obj":
        bpy.ops.wm.obj_import(filepath=path)
    else:
        raise SystemExit(f"unsupported set format {ext}")


def roots() -> list[bpy.types.Object]:
    return [ob for ob in bpy.data.objects if ob.parent is None]


def descendants(ob: bpy.types.Object) -> list[bpy.types.Object]:
    out = [ob]
    for ch in ob.children:
        out += descendants(ch)
    return out


def variants() -> dict[str, dict[str | None, bpy.types.Object]]:
    """base id -> {None: default object, 'K2': per-clip, 'K2.3': per-cut}"""
    table: dict[str, dict[str | None, bpy.types.Object]] = {}
    for ob in roots():
        base, _, scope = ob.name.partition("@")
        table.setdefault(base, {})[scope or None] = ob
    return table


def apply_blocking(table: dict, clip: str, cut: int) -> dict[str, bpy.types.Object]:
    """Show the most specific variant of every object for this cut and hide the others."""
    active = {}
    for base, vs in table.items():
        pick = vs.get(f"{clip}.{cut}") or vs.get(clip) or vs.get(None)
        for ob in vs.values():
            for d in descendants(ob):
                d.hide_render = ob is not pick
        if pick is not None:
            active[base] = pick
    bpy.context.view_layer.update()
    return active


def meshes_of(ob) -> list:
    return [d for d in descendants(ob) if d.type == "MESH"]


def world_bbox(obs) -> tuple[Vector, Vector]:
    pts = [d.matrix_world @ Vector(c) for d in obs for c in d.bound_box]
    if not pts:
        return Vector(), Vector()
    return (Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts))),
            Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts))))


class Char:
    def __init__(self, ob):
        self.ob = ob
        self.feet = ob.matrix_world.translation.copy()
        lo, hi = world_bbox(meshes_of(ob))
        self.height = float(ob.get("height", 0) or (hi.z - lo.z) or 1.7)
        if meshes_of(ob):
            self.feet.z = lo.z
        f = ob.matrix_world.to_3x3() @ Vector((0, -1, 0))
        f.z = 0
        self.fwd = f.normalized() if f.length > 1e-6 else Vector((0, -1, 0))

    def at(self, frac: float) -> Vector:
        return self.feet + UP * (self.height * frac)


# ---------- automatic cameras ----------

def free_side(origin: Vector, perp: Vector, reach: float = 2.5) -> int:
    """+1 or -1: the side of the action line where a camera can stand, i.e. open space with floor under it
    (an unglazed window wall looks open, but there is no floor outside it)."""
    dg = bpy.context.evaluated_depsgraph_get()
    scene = bpy.context.scene
    score = []
    for s in (1, -1):
        d = perp * s
        hit, loc, *_ = scene.ray_cast(dg, origin + d * 0.45, d, distance=30)
        free = (loc - origin).length if hit else 30.0
        spot = origin + d * min(reach, free - 0.3)
        floor, *_ = scene.ray_cast(dg, spot, Vector((0, 0, -1)), distance=3.0)
        score.append((floor and free > 1.0, free))
    return 1 if score[0] >= score[1] else -1


def distance_for(lens: float, frame_h: float) -> float:
    return (frame_h / 2) / math.tan(math.atan(SENSOR_H / 2 / lens))


def look(pos: Vector, target: Vector) -> Matrix:
    rot = (target - pos).to_track_quat("-Z", "Y").to_matrix().to_4x4()
    return Matrix.Translation(pos) @ rot


def auto_camera(cut: dict, chars: dict[str, Char], side_ref: dict) -> tuple[float, Matrix, Vector, str]:
    """Return lens, camera matrix at the cut's first frame, aim point, and a short description."""
    t = cut["type"]
    lens, frame_h, aim = SHOT[t]
    subj = [chars[c] for c in cut.get("subjects", []) if c in chars]
    perp, side = side_ref["perp"], side_ref["side"]
    if t == "OTS":
        a, b = chars[cut["over"]], subj[0]
        back = (a.feet - b.feet)
        back.z = 0
        back.normalize()
        pos = a.at(0.92) + back * 0.8 + perp * side * 0.38
        target = b.at(aim)
        return lens, look(pos, target), target, f"over {cut['over']}'s shoulder onto {cut['subjects'][0]}"
    if t == "MS" and len(subj) >= 2:
        a, b = subj[0], subj[1]
        ab = b.feet - a.feet
        ab.z = 0
        spread = ab.length
        if spread > 2.0:   # staged in depth: first subject near, the other deep in frame
            target = a.at(0.65).lerp(b.at(0.65), 0.55)
            pos = a.at(0.0) - ab.normalized() * 2.4 + perp * side * 1.3
            pos.z = 1.45
            return 28, look(pos, target), target, "medium shot staged in depth"
        mid = (a.at(aim) + b.at(aim)) / 2
        frame_h = max(frame_h, (spread + 1.1) * 16 / 9)
        pos = mid + perp * side * distance_for(lens, frame_h)
        pos.z = mid.z
        return lens, look(pos, mid), mid, "medium two-shot, line of action across frame"
    c = subj[0]
    target = c.at(aim)
    if t == "MS":
        direction = (c.fwd + perp * side * 0.5).normalized()
    else:
        direction = (c.fwd + perp * side * math.tan(math.radians(22))).normalized()
    pos = target + direction * distance_for(lens, frame_h)
    return lens, look(pos, target), target, f"{t} of {cut['subjects'][0]}"


def move_keys(m0: Matrix, target: Vector, move: dict, cut_type: str, lens: float, frames: int) -> list[tuple[int, Matrix]]:
    """Camera matrices at frame offsets 0..frames-1 for the cut's move."""
    kind, scale = move.get("kind", "static"), MOVE_SCALE.get(move.get("amount", "small"), 0.5)
    pos0 = m0.translation.copy()
    to_t = target - pos0
    right = (m0.to_3x3() @ Vector((1, 0, 0))).normalized()
    end = frames - 1
    if cut_type == "VO":   # house rule: close-up slowly pushing in to an extreme close-up
        d_end = distance_for(lens, SHOT["ECU"][1] * 1.1)
        return [(0, m0), (end, look(target - to_t.normalized() * d_end, target))]
    if kind == "push_in":
        return [(0, m0), (end, look(pos0 + to_t * (0.22 * scale), target))]
    if kind == "pull_out":
        return [(0, m0), (end, look(pos0 - to_t * (0.25 * scale), target))]
    if kind in ("truck_left", "truck_right"):
        s = 1 if kind == "truck_right" else -1
        off = right * s * 0.5 * scale
        return [(0, m0), (end, look(pos0 + off, target + off))]
    if kind in ("pan_left", "pan_right", "tilt_up", "tilt_down"):
        axis = UP if kind.startswith("pan") else right
        s = (1 if kind in ("pan_left", "tilt_up") else -1)
        r = Matrix.Rotation(math.radians(10 * scale) * s, 3, axis)
        return [(0, m0), (end, Matrix.Translation(pos0) @ (r @ m0.to_3x3()).to_4x4())]
    if kind in ("orbit_left", "orbit_right"):
        s = 1 if kind == "orbit_left" else -1
        r = Matrix.Rotation(math.radians(14 * scale) * s, 3, UP)
        return [(0, m0), (end, look(target + r @ (pos0 - target), target))]
    if kind == "handheld":
        keys = []
        for i, f in enumerate(range(0, frames, 6)):
            jitter = Vector((math.sin(i * 1.7), math.cos(i * 2.3), math.sin(i * 1.1))) * 0.006
            keys.append((f, look(pos0 + jitter, target + jitter * 0.5)))
        return keys + [(end, look(pos0, target))]
    return [(0, m0), (end, m0)]


def keyed_camera(name: str, lens: float, keys: list[tuple[int, Matrix]], start: int) -> bpy.types.Object:
    data = bpy.data.cameras.new(name)
    data.lens = lens
    data.sensor_fit = "VERTICAL"
    data.sensor_height = SENSOR_H
    data.clip_start, data.clip_end = 0.05, 2000
    cam = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(cam)
    cam.rotation_mode = "QUATERNION"
    for off, m in keys:
        cam.matrix_world = m
        cam.keyframe_insert("location", frame=start + off)
        cam.keyframe_insert("rotation_quaternion", frame=start + off)
    return cam


# ---------- rendering ----------

def node_material(name: str) -> bpy.types.Material:
    mat = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    mat.use_nodes = True
    mat.node_tree.nodes.clear()
    return mat


def clay_material() -> bpy.types.Material:
    mat = node_material("_sheel_clay")
    nt = mat.node_tree
    bsdf = nt.nodes.new("ShaderNodeBsdfDiffuse")
    bsdf.inputs["Color"].default_value = (0.62, 0.62, 0.62, 1)
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(bsdf.outputs[0], out.inputs["Surface"])
    return mat


def clay_world(scene) -> None:
    """Dark backdrop seen through windows, soft grey fill for lighting (Cycles)."""
    w = bpy.data.worlds.get("_sheel_world") or bpy.data.worlds.new("_sheel_world")
    w.use_nodes = True
    nt = w.node_tree
    nt.nodes.clear()
    path = nt.nodes.new("ShaderNodeLightPath")
    seen = nt.nodes.new("ShaderNodeBackground")
    seen.inputs["Color"].default_value = (0.02, 0.025, 0.04, 1)
    fill = nt.nodes.new("ShaderNodeBackground")
    fill.inputs["Color"].default_value = (1, 1, 1, 1)
    fill.inputs["Strength"].default_value = 0.45
    mix = nt.nodes.new("ShaderNodeMixShader")
    out = nt.nodes.new("ShaderNodeOutputWorld")
    nt.links.new(path.outputs["Is Camera Ray"], mix.inputs[0])
    nt.links.new(fill.outputs[0], mix.inputs[1])
    nt.links.new(seen.outputs[0], mix.inputs[2])
    nt.links.new(mix.outputs[0], out.inputs["Surface"])
    scene.world = w
    if "_sheel_key" not in bpy.data.objects:
        sun = bpy.data.objects.new("_sheel_key", bpy.data.lights.new("_sheel_key", "SUN"))
        sun.data.energy = 2.5
        sun.data.angle = math.radians(8)
        sun.rotation_euler = (math.radians(50), 0, math.radians(35))
        scene.collection.objects.link(sun)


def setup_clay(scene, engine: str) -> None:
    """Grey clay look. `workbench` is fastest but needs a GPU; `cycles` renders on the CPU anywhere."""
    vl = bpy.context.view_layer
    if engine == "workbench":
        scene.render.engine = "BLENDER_WORKBENCH"
        sh = scene.display.shading
        sh.light, sh.color_type, sh.single_color = "STUDIO", "SINGLE", (0.78, 0.78, 0.78)
        sh.show_cavity = sh.show_shadows = True
        if scene.world is None:
            scene.world = bpy.data.worlds.new("World")
        scene.world.color = (0.03, 0.035, 0.05)
        vl.material_override = None
    else:
        scene.render.engine = "CYCLES"
        scene.cycles.device = "CPU"
        scene.cycles.samples = 12
        scene.cycles.use_denoising = True
        scene.cycles.max_bounces = 2
        scene.cycles.caustics_reflective = scene.cycles.caustics_refractive = False
        scene.render.use_persistent_data = True
        clay_world(scene)
        vl.material_override = clay_material()
    scene.view_settings.view_transform = "Standard"
    scene.render.film_transparent = False
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGB"


def depth_material(near: float, far: float) -> bpy.types.Material:
    mat = node_material("_sheel_depth")
    nt = mat.node_tree
    cam = nt.nodes.new("ShaderNodeCameraData")
    rng = nt.nodes.new("ShaderNodeMapRange")
    rng.clamp = True
    rng.inputs["From Min"].default_value, rng.inputs["From Max"].default_value = near, far
    rng.inputs["To Min"].default_value, rng.inputs["To Max"].default_value = 1.0, 0.0
    gamma = nt.nodes.new("ShaderNodeMath")
    gamma.operation = "POWER"
    gamma.inputs[1].default_value = 2.2       # cancel the sRGB display transform so the PNG is linear in depth
    emit = nt.nodes.new("ShaderNodeEmission")
    out = nt.nodes.new("ShaderNodeOutputMaterial")
    nt.links.new(cam.outputs["View Z Depth"], rng.inputs["Value"])
    nt.links.new(rng.outputs["Result"], gamma.inputs[0])
    nt.links.new(gamma.outputs[0], emit.inputs["Color"])
    nt.links.new(emit.outputs["Emission"], out.inputs["Surface"])
    return mat


def depth_range(cam_m: Matrix) -> tuple[float, float]:
    inv = cam_m.inverted()
    zs = []
    for ob in bpy.data.objects:
        if ob.type == "MESH" and not ob.hide_render:
            for c in ob.bound_box:
                z = -(inv @ (ob.matrix_world @ Vector(c))).z
                if 0 < z < 40:
                    zs.append(z)
    if not zs:
        return 0.1, 20.0
    return max(0.05, min(zs) * 0.9), max(zs)


def render_still(scene, path: Path, depth: tuple[float, float] | None, res: tuple[int, int], engine: str) -> None:
    scene.render.resolution_x, scene.render.resolution_y = res
    scene.render.resolution_percentage = 100
    scene.render.filepath = str(path)
    if depth is None:
        setup_clay(scene, engine)
        bpy.ops.render.render(write_still=True)
        return
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 4
    scene.cycles.use_denoising = False
    black = bpy.data.worlds.get("_sheel_black") or bpy.data.worlds.new("_sheel_black")
    black.use_nodes = False
    black.color = (0, 0, 0)
    scene.world = black
    key = bpy.data.objects.get("_sheel_key")
    if key:
        key.hide_render = True
    bpy.context.view_layer.material_override = depth_material(*depth)
    try:
        bpy.ops.render.render(write_still=True)
    finally:
        bpy.context.view_layer.material_override = None
        if key:
            key.hide_render = False


# ---------- measuring what a camera sees (for prompt text) ----------

SKIP_LABELS = ("floor", "ceiling", "wall", "corridor")


def label_of(ob) -> str | None:
    """A set piece's name for prompts: its (or its root's) "label" property, else its root name cleaned up."""
    root = ob
    while root.parent is not None:
        if root.get("label"):
            return root["label"]
        root = root.parent
    if root.get("label") or ob.get("label"):
        return root.get("label") or ob.get("label")
    name = re.sub(r"(@.*|_\d+|_[lrab])$", "", root.name.split(".")[0])
    name = re.sub(r"_[lrab]$", "", name).replace("_", " ").strip()
    return None if name.startswith(SKIP_LABELS) or not name else name


def screen(scene, cam, co: Vector) -> Vector:
    return world_to_camera_view(scene, cam, co)


def camera_angles(m: Matrix) -> tuple[float, float]:
    """Pitch (+ up) and roll (+ counter-clockwise) of a camera matrix, in degrees."""
    fwd = (m.to_3x3() @ Vector((0, 0, -1))).normalized()
    right = (m.to_3x3() @ Vector((1, 0, 0))).normalized()
    up = (m.to_3x3() @ Vector((0, 1, 0))).normalized()
    pitch = math.degrees(math.asin(max(-1, min(1, fwd.z))))
    flat_right = fwd.cross(UP)
    roll = math.degrees(math.atan2(right.dot(UP), up.dot(UP))) if flat_right.length > 1e-6 else 0.0
    return pitch, roll


def measure_move(m0: Matrix, m1: Matrix, dist: float) -> dict:
    """Camera move between a cut's first and last frame, in the camera's own frame of reference."""
    d = m0.to_3x3().inverted() @ (m1.translation - m0.translation)   # x right, y up, -z forward
    p0, r0 = camera_angles(m0)
    p1, r1 = camera_angles(m1)
    f0 = m0.to_3x3() @ Vector((0, 0, -1))
    f1 = m1.to_3x3() @ Vector((0, 0, -1))
    yaw = math.degrees(math.atan2(f0.x * f1.y - f0.y * f1.x, f0.x * f1.x + f0.y * f1.y))
    return {"forward_m": round(-d.z, 3), "right_m": round(d.x, 3), "up_m": round(d.y, 3), "pan_deg": round(-yaw, 1),
            "tilt_deg": round(p1 - p0, 1), "roll_deg": round(r1 - r0, 1), "subject_distance_m": round(dist, 2)}


def measure_view(scene, cam, chars: dict[str, Char], frame0: int, frame1: int) -> dict:
    """What the camera shows at the cut's first frame (and how it moves until the last): for prompt text."""
    scene.frame_set(frame1)
    m1 = cam.matrix_world.copy()
    scene.frame_set(frame0)
    m0 = cam.matrix_world.copy()
    pitch, roll = camera_angles(m0)
    cam_pos = m0.translation
    right = (m0.to_3x3() @ Vector((1, 0, 0)))
    vfov = 2 * math.atan(cam.data.sensor_height / 2 / cam.data.lens) if cam.data.sensor_fit == "VERTICAL" \
        else cam.data.angle_y
    subjects = {}
    for cid, ch in chars.items():
        eye = ch.at(0.93)
        p = screen(scene, cam, eye)
        feet = screen(scene, cam, ch.feet)
        head_top = screen(scene, cam, ch.at(1.0))
        to_cam = cam_pos - ch.feet
        to_cam.z = 0
        ang = math.degrees(math.atan2(ch.fwd.x * to_cam.y - ch.fwd.y * to_cam.x, ch.fwd.dot(to_cam))) if to_cam.length > 1e-6 else 0
        subjects[cid] = {
            "x": round(p.x, 3), "y": round(p.y, 3), "depth_m": round(p.z, 2),
            "in_frame": bool(p.z > 0 and -0.05 <= p.x <= 1.05 and -0.05 <= p.y <= 1.05),
            "frame_height_m": round(2 * p.z * math.tan(vfov / 2), 2) if p.z > 0 else None,
            "head_y": round(head_top.y, 3), "feet_y": round(feet.y, 3),
            "facing_deg": round(abs(ang), 1),                       # 0 = facing the camera, 180 = back to camera
            "looks_screen": "right" if ch.fwd.dot(right) > 0.15 else "left" if ch.fwd.dot(right) < -0.15 else "camera",
        }
    areas: dict[str, float] = {}
    hidden_chars = {d.name for ch in chars.values() for d in descendants(ch.ob)}
    for ob in bpy.data.objects:
        if ob.type != "MESH" or ob.hide_render or ob.name in hidden_chars or ob.name.startswith("_sheel"):
            continue
        lab = label_of(ob)
        if not lab:
            continue
        pts = [screen(scene, cam, ob.matrix_world @ Vector(c)) for c in ob.bound_box]
        pts = [q for q in pts if q.z > 0]
        if not pts:
            continue
        x0, x1 = max(0, min(q.x for q in pts)), min(1, max(q.x for q in pts))
        y0, y1 = max(0, min(q.y for q in pts)), min(1, max(q.y for q in pts))
        if x1 > x0 and y1 > y0:
            areas[lab] = areas.get(lab, 0) + (x1 - x0) * (y1 - y0)
    visible = [k for k, a in sorted(areas.items(), key=lambda kv: -kv[1]) if a >= 0.02]
    first = next(iter(subjects.values()), None)
    return {"lens": round(cam.data.lens, 1), "height_m": round(cam_pos.z, 2), "pitch_deg": round(pitch, 1),
            "roll_deg": round(roll, 1), "subjects": subjects, "set_pieces": visible[:6],
            "move": measure_move(m0, m1, first["depth_m"] if first and first["depth_m"] > 0 else 2.0)}


def main() -> None:
    job = args()
    if job.get("import"):
        import_set(job["import"])
    bpy.context.preferences.edit.keyframe_new_interpolation_type = "LINEAR"
    scene = bpy.context.scene
    fps = int(job.get("fps", 24))
    scene.render.fps, scene.render.fps_base = fps, 1.0
    out = Path(job["out"])
    engine = job.get("engine", "cycles")
    table = variants()
    manifest = {"fps": fps, "clips": {}}
    for clip in job["clips"]:
        cdir = out / clip["id"]
        (cdir / "frames").mkdir(parents=True, exist_ok=True)
        for old in (cdir / "frames").glob("*.png"):
            old.unlink()
        cuts_meta, start = [], 1
        side_ref = None
        for n, cut in enumerate(clip["cuts"], 1):
            active = apply_blocking(table, clip["id"], n)
            missing = [c for c in clip["cast"] if c not in active]
            if missing:
                raise SystemExit(f"{clip['id']} cut {n}: set has no object named {missing} (or {missing[0]}@{clip['id']})")
            chars = {c: Char(active[c]) for c in clip["cast"]}
            if side_ref is None:   # 180-degree rule: the camera side is chosen once per clip
                a = chars[clip["cast"][0]]
                if len(clip["cast"]) > 1:
                    line = chars[clip["cast"][1]].feet - a.feet
                    line.z = 0
                    perp = Vector((-line.y, line.x, 0)).normalized()
                    mid = (a.feet + chars[clip["cast"][1]].feet) / 2 + UP * 1.2
                else:
                    perp = Vector((-a.fwd.y, a.fwd.x, 0))
                    mid = a.at(0.7)
                side_ref = {"perp": perp, "side": free_side(mid, perp)}
            frames = max(2, round(cut["dur"] * fps))
            user_cam = bpy.data.objects.get(f"CAM_{clip['id']}_{n}")
            if user_cam is not None and user_cam.type == "CAMERA":
                cam, how = user_cam, "camera from the set file"
                scene.frame_set(start)
            else:
                lens, m0, target, how = auto_camera(cut, chars, side_ref)
                keys = move_keys(m0, target, cut.get("move", {}), cut["type"], lens, frames)
                cam = keyed_camera(f"AUTO_{clip['id']}_{n}", lens, keys, start)
                scene.frame_set(start)
            scene.camera = cam
            scene.render.resolution_x, scene.render.resolution_y = job["still_res"]
            view = measure_view(scene, cam, chars, start, start + frames - 1)
            m_first = cam.matrix_world.copy()
            near, far = depth_range(m_first)
            if not job.get("measure_only"):
                render_still(scene, cdir / f"cut{n}_clay.png", None, tuple(job["still_res"]), engine)
                render_still(scene, cdir / f"cut{n}_depth.png", (near, far), tuple(job["still_res"]), engine)
                setup_clay(scene, engine)
                if engine != "workbench":
                    scene.cycles.samples = int(job.get("animatic_samples", 6))
                scene.render.resolution_x, scene.render.resolution_y = job["animatic_res"]
                scene.frame_start, scene.frame_end = start, start + frames - 1
                scene.frame_step = int(job.get("animatic_step", 1))
                scene.render.filepath = str(cdir / "frames" / "####")
                bpy.ops.render.render(animation=True)
            cuts_meta.append({"cut": n, "type": cut["type"], "camera": cam.name, "how": how, "lens": cam.data.lens,
                              "set_camera": cam is user_cam, "frames": [start, start + frames - 1],
                              "position": list(m_first.translation), "depth_range": [round(near, 3), round(far, 3)],
                              "move": {"kind": "from set file"} if cam is user_cam else cut.get("move", {}), "view": view})
            start += frames
        manifest["clips"][clip["id"]] = {"cuts": cuts_meta, "frames": start - 1}
    (out / ("views.json" if job.get("measure_only") else "manifest.json")).write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    print("render_shots: done", flush=True)


main()
