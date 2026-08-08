import sys
from pathlib import Path

# Tests import `make_sample` (the fixture generator) and the package from the
# repo root, without requiring an install.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
