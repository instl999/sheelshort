"""Build the white-mesh test set for spike/scene.json (the penthouse of 归来). Runs inside Blender:

    blender -b --factory-startup -P make_test_set.py -- out.blend

Layout (metres, +Y north): 10 x 8 room, floor-to-ceiling window wall on the north side with a skyline of boxes
outside, doorway at the north end of the east wall, sofa and two lamps in the middle of the room.
Blocking: K1 has Lin Xia by the window and Lu Chen in the doorway; K2 (`@K2` objects) has them face to face
in front of the window. Characters face local -Y.
"""
import math
import sys

import bpy
from mathutils import Vector


def box(name, center, size, parent=None):
    bpy.ops.mesh.primitive_cube_add(size=1, location=center)
    ob = bpy.context.object
    ob.name = name
    ob.scale = size
    bpy.ops.object.transform_apply(scale=True)
    if parent:
        ob.parent = parent
    return ob


def cyl(name, center, r, h, parent=None, verts=24):
    bpy.ops.mesh.primitive_cylinder_add(vertices=verts, radius=r, depth=h, location=center)
    ob = bpy.context.object
    ob.name = name
    if parent:
        ob.parent = parent
    return ob


def sphere(name, center, r, parent=None):
    bpy.ops.mesh.primitive_uv_sphere_add(radius=r, location=center, segments=24, ring_count=12)
    ob = bpy.context.object
    ob.name = name
    if parent:
        ob.parent = parent
    return ob


def mannequin(name, at, facing_to, height):
    """Grey figure, feet at `at`, facing the point `facing_to` (local -Y)."""
    root = bpy.data.objects.new(name, None)
    bpy.context.scene.collection.objects.link(root)
    root["height"] = height
    s = height / 1.75
    parts = [
        cyl(f"{name}.leg_l", (-0.09 * s, 0, 0.43 * s), 0.065 * s, 0.86 * s),
        cyl(f"{name}.leg_r", (0.09 * s, 0, 0.43 * s), 0.065 * s, 0.86 * s),
        cyl(f"{name}.torso", (0, 0, 1.15 * s), 0.17 * s, 0.6 * s),
        cyl(f"{name}.arm_l", (-0.23 * s, 0, 1.12 * s), 0.045 * s, 0.6 * s),
        cyl(f"{name}.arm_r", (0.23 * s, 0, 1.12 * s), 0.045 * s, 0.6 * s),
        cyl(f"{name}.neck", (0, 0, 1.5 * s), 0.05 * s, 0.12 * s),
        sphere(f"{name}.head", (0, 0, 1.63 * s), 0.11 * s),
    ]
    bpy.ops.mesh.primitive_cone_add(radius1=0.03 * s, depth=0.06 * s, location=(0, -0.12 * s, 1.63 * s), rotation=(math.radians(90), 0, 0))
    nose = bpy.context.object
    nose.name = f"{name}.nose"
    parts.append(nose)
    for p in parts:
        p.parent = root
    d = Vector(facing_to) - Vector(at)
    root.location = (at[0], at[1], 0)
    root.rotation_euler = (0, 0, math.atan2(d.x, -d.y))
    return root


def main():
    out = sys.argv[sys.argv.index("--") + 1]
    for ob in list(bpy.data.objects):
        bpy.data.objects.remove(ob, do_unlink=True)

    # room shell
    box("floor", (0, 0, -0.05), (10, 8, 0.1))
    box("ceiling", (0, 0, 3.25), (10, 8, 0.1))
    box("wall_south", (0, -4.05, 1.6), (10, 0.1, 3.2))
    box("wall_west", (-5.05, 0, 1.6), (0.1, 8, 3.2))
    box("wall_east_a", (5.05, -1.0, 1.6), (0.1, 6.0, 3.2))        # y -4 .. 2
    box("wall_east_b", (5.05, 3.6, 1.6), (0.1, 0.8, 3.2))         # y 3.2 .. 4
    box("wall_east_lintel", (5.05, 2.6, 2.75), (0.1, 1.2, 0.9))   # doorway y 2.0 .. 3.2, 2.3 m high
    box("door_frame_l", (4.98, 1.97, 1.15), (0.06, 0.06, 2.3))
    box("door_frame_r", (4.98, 3.23, 1.15), (0.06, 0.06, 2.3))
    box("corridor_floor", (6.5, 2.6, -0.05), (3, 1.2, 0.1))
    box("corridor_back", (8.0, 2.6, 1.6), (0.1, 1.2, 3.2))
    # window wall: mullions, sill and head, no glass
    for i in range(9):
        box(f"mullion_{i}", (-5 + i * 1.25, 3.98, 1.6), (0.06, 0.08, 3.2))
    box("window_sill", (0, 3.98, 0.04), (10, 0.12, 0.08))
    box("window_head", (0, 3.98, 3.16), (10, 0.12, 0.08))
    # furniture
    box("sofa_seat", (-1.0, 0.2, 0.22), (2.4, 0.9, 0.44))
    box("sofa_back", (-1.0, -0.2, 0.6), (2.4, 0.2, 0.75))
    box("sofa_arm_l", (-2.25, 0.2, 0.4), (0.18, 0.9, 0.6))
    box("sofa_arm_r", (0.25, 0.2, 0.4), (0.18, 0.9, 0.6))
    box("coffee_table", (-1.0, 1.5, 0.2), (1.2, 0.7, 0.4))
    for x, n in ((-2.75, "l"), (0.75, "r")):
        box(f"side_table_{n}", (x, 0.2, 0.28), (0.5, 0.5, 0.56))
        cyl(f"lamp_{n}_base", (x, 0.2, 0.78), 0.03, 0.45)
        bpy.ops.mesh.primitive_cone_add(radius1=0.2, radius2=0.13, depth=0.28, location=(x, 0.2, 1.08))
        bpy.context.object.name = f"lamp_{n}_shade"
    # skyline across the river (Lujiazui stand-in)
    import random
    rnd = random.Random(7)
    for i in range(26):
        x = -70 + i * 5.6 + rnd.uniform(-1, 1)
        h = rnd.uniform(18, 70) * (1.8 if abs(x - 12) < 12 else 1)
        box(f"tower_{i}", (x, 90 + rnd.uniform(0, 25), h / 2 - 20), (rnd.uniform(3, 5), 4, h))
    cyl("pearl_mast", (-14, 85, 40), 0.9, 140, verts=12)
    sphere("pearl_ball_low", (-14, 85, 30), 6)
    sphere("pearl_ball_high", (-14, 85, 75), 4)

    # cast and props
    lx = mannequin("linxia", (0.3, 3.1), (4.3, 2.6), 1.70)
    mannequin("luchen", (4.4, 2.6), (0.3, 3.1), 1.82)
    glass = cyl("wine_glass", (0.0, 0.0, 0.0), 0.04, 0.2, verts=16)
    glass.parent = lx
    glass.location = (0.24, -0.08, 0.95)
    mannequin("linxia@K2", (1.5, 2.9), (2.3, 2.9), 1.70)
    mannequin("luchen@K2", (2.3, 2.9), (1.5, 2.9), 1.82)

    # prompt names for set pieces (render_shots reads the "label" property)
    labels = {"tower": "city skyline", "pearl": "city skyline", "mullion": "floor-to-ceiling window",
              "window": "floor-to-ceiling window", "door_frame": "doorway", "sofa": "sofa", "lamp": "table lamp",
              "side_table": "side table", "coffee_table": "coffee table"}
    for ob in bpy.data.objects:
        for prefix, label in labels.items():
            if ob.parent is None and ob.name.startswith(prefix):
                ob["label"] = label
    bpy.ops.wm.save_as_mainfile(filepath=out)
    print("make_test_set: saved", out)


main()
