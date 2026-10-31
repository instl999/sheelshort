"""Add deliberately unusual cameras to the test set, for checking that H3 follows the blockout rather than the
prompt text. Each one contradicts what the cut's composition text implies. Runs inside Blender:

    blender -b penthouse.blend --factory-startup -P add_test_cams.py -- penthouse_cams.blend

  CAM_K1_1  high angle from just under the ceiling, looking steeply down (text: eye-level rule-of-thirds MS)
  CAM_K1_2  low angle from chest height looking up at Lin Xia (text: tight close-up)
  CAM_K2_1  reverse angle from the window looking into the room: no skyline, Lin Xia on the right
            (text: profile two-shot in front of the window, Lin Xia left, skyline between them)
  CAM_K2_3  25-degree Dutch tilt on Lu Chen's close-up (text: level close-up)
"""
import math
import sys

import bpy
from mathutils import Matrix, Vector

CAMS = {
    "CAM_K1_1": ((1.5, -1.5, 3.12), (2.2, 2.85, 0.8), 16, 0),
    "CAM_K1_2": ((1.2, 2.75, 0.85), (0.3, 3.1, 1.55), 50, 0),
    "CAM_K2_1": ((1.9, 3.88, 1.45), (1.9, 0.5, 1.0), 20, 0),
    "CAM_K2_3": ((1.15, 2.5, 1.71), (2.3, 2.9, 1.71), 85, 25),
}


def main():
    out = sys.argv[sys.argv.index("--") + 1]
    for name, (pos, target, lens, roll) in CAMS.items():
        data = bpy.data.cameras.new(name)
        data.lens, data.sensor_fit, data.sensor_height = lens, "VERTICAL", 36.0
        data.clip_start, data.clip_end = 0.05, 2000
        cam = bpy.data.objects.new(name, data)
        bpy.context.scene.collection.objects.link(cam)
        p, t = Vector(pos), Vector(target)
        rot = (t - p).to_track_quat("-Z", "Y").to_matrix().to_4x4() @ Matrix.Rotation(math.radians(roll), 4, "Z")
        cam.matrix_world = Matrix.Translation(p) @ rot
    bpy.ops.wm.save_as_mainfile(filepath=out)
    print("add_test_cams: saved", out)


main()
