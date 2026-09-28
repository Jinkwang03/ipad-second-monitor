"""Write packaging/icon.ico (the .exe and shortcut icon) from the app's own icon drawing."""
import io
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import make_icon  # noqa: E402

out = ROOT / "packaging" / "icon.ico"
Image.open(io.BytesIO(make_icon(256))).save(out, sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
print(f"wrote {out}")
