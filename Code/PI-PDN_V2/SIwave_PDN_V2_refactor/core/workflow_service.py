from __future__ import annotations

import runpy
import sys
from pathlib import Path


def run_pipeline(*, input_json: Path, stage: str, working_dir: Path) -> None:
    """Run the preserved pipeline behind the small operational entry point."""
    legacy_entry = Path(working_dir) / "legacy_pipeline.py"
    original_argv = list(sys.argv)
    try:
        sys.argv = [str(legacy_entry), str(Path(input_json).resolve()), "--stage", stage]
        runpy.run_path(str(legacy_entry), run_name="__main__")
    finally:
        sys.argv = original_argv

