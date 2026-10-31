"""sheelshort: script -> finished vertical short-drama episodes (see cli.py)."""
import os
import shutil
from pathlib import Path

if not shutil.which("ffmpeg"):
    # Fall back to the ffmpeg binary that ships with imageio-ffmpeg (built with libass, x264), and to ~/.local/bin.
    _dirs = [str(Path.home() / ".local" / "bin")]
    try:
        import imageio_ffmpeg
        _exe = Path(imageio_ffmpeg.get_ffmpeg_exe())
        _link = Path(__file__).resolve().parent.parent / ".cache" / "bin" / "ffmpeg"
        _link.parent.mkdir(parents=True, exist_ok=True)
        if not _link.exists():
            _link.symlink_to(_exe)
        _dirs.append(str(_link.parent))
    except ImportError:
        pass
    os.environ["PATH"] = os.pathsep.join(_dirs + [os.environ.get("PATH", "")])
