"""Root conftest — ensures the project root is importable in tests.

pytest normally inserts the rootdir into sys.path when a conftest.py exists
there; this also makes the `data` and `execution` packages importable from any
working directory.
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))