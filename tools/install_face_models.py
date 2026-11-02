"""Put the two OpenCV face models drift.py uses into .cache/models/ (they are not committed).

YuNet (face detection, MIT) and SFace (face recognition, Apache-2.0), from the OpenCV model zoo. Each file is checked
against its SHA-256 before it is kept.
"""
from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path

DEST = Path(__file__).resolve().parent.parent / ".cache" / "models"
BASE = "https://github.com/opencv/opencv_zoo/raw/main/models"
MODELS = {
    "face_detection_yunet_2023mar.onnx": ("face_detection_yunet",
                                          "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"),
    "face_recognition_sface_2021dec.onnx": ("face_recognition_sface",
                                            "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79"),
}


def main() -> int:
    DEST.mkdir(parents=True, exist_ok=True)
    for name, (folder, sha) in MODELS.items():
        dest = DEST / name
        if dest.exists() and hashlib.sha256(dest.read_bytes()).hexdigest() == sha:
            print(f"{name}: present")
            continue
        data = urllib.request.urlopen(f"{BASE}/{folder}/{name}", timeout=300).read()
        if hashlib.sha256(data).hexdigest() != sha:
            print(f"{name}: download does not match its SHA-256; not kept", file=sys.stderr)
            return 1
        dest.write_bytes(data)
        print(f"{name}: {len(data) / 1e6:.1f} MB -> {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
