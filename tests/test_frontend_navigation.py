import shutil
import subprocess
from pathlib import Path

import pytest


def test_navigation_javascript():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed")
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        [node, "tests/frontend_navigation.cjs"], cwd=root, check=True, timeout=10
    )
