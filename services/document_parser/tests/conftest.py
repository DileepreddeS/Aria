"""Make ``fixtures.py`` importable from the tests in this directory.

pytest runs with ``--import-mode=importlib`` (three workspace members each have a
``tests`` directory, and the default mode imports the first one and then cannot see
the others). That mode does not put a test file's own directory on ``sys.path``, so a
sibling helper module has to be made reachable explicitly.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
