"""The secret scan `make scan` runs over tracked files catches a planted key.

The scan is detect-secrets' pre-commit hook, pinned in the lock, so a plain clone runs it with
nothing outside `uv sync`. A false positive takes `# pragma: allowlist secret` on its line.
"""

import subprocess
import sys
from pathlib import Path

HOOK = Path(sys.executable).with_name("detect-secrets-hook")


def scan(*files: Path) -> subprocess.CompletedProcess[str]:
    assert HOOK.is_file(), "detect-secrets is missing: run `uv sync`"
    return subprocess.run(
        [str(HOOK), *map(str, files)], capture_output=True, text=True, check=False
    )


def test_a_planted_key_fails_the_scan(tmp_path: Path) -> None:
    planted = tmp_path / "settings.py"
    # Built from pieces so this file's own lines never match the key pattern.
    planted.write_text('aws_key = "' + "AK" + "IA" + "Z7Q3M9XK2PL8V4RT" + '"\n')
    result = scan(planted)
    assert result.returncode == 1
    assert "AWS Access Key" in result.stdout


def test_a_clean_file_passes_the_scan(tmp_path: Path) -> None:
    clean = tmp_path / "notes.md"
    clean.write_text("Figures are public records.\n")
    assert scan(clean).returncode == 0
