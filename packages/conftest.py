import sys
from pathlib import Path

_PACKAGES = Path(__file__).resolve().parent
_PLATFORM_SRC = _PACKAGES.parent / "person2_platform" / "src"
for path in (_PACKAGES, _PLATFORM_SRC):
    if path.exists():
        text = str(path)
        if text not in sys.path:
            sys.path.insert(0, text)
