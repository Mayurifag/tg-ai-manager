from pathlib import Path
import subprocess


def test_emoji_mapping_against_bundled_parser():
    subprocess.run(
        ["node", "tests/frontend_emoji.cjs"],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        timeout=10,
    )
