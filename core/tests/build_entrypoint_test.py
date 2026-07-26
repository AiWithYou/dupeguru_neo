import subprocess
import sys
from pathlib import Path


def test_build_help_does_not_require_optional_build_dependencies_at_import_time():
    repository_root = Path(__file__).parents[2]

    result = subprocess.run(
        [sys.executable, "-S", "build.py", "--help"],
        cwd=repository_root,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "--modules" in result.stdout
