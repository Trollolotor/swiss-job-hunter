"""Keep the test suite completely isolated from the user's SQLite database."""
from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

_test_dir: Path | None = None


def pytest_configure(config) -> None:
    global _test_dir
    del config
    _test_dir = Path(tempfile.mkdtemp(prefix="swiss-job-hunter-tests-"))
    os.environ["DATABASE_URL"] = f"sqlite:///{_test_dir / 'tests.db'}"


def pytest_unconfigure(config) -> None:
    del config
    if _test_dir:
        shutil.rmtree(_test_dir, ignore_errors=True)
