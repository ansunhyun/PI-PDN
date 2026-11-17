# coding=utf-8
"""AutoPDN V2 operating entry point."""
from __future__ import annotations

import argparse
from pathlib import Path

from core.workflow_service import run_pipeline


WORKING_DIR = Path(__file__).resolve().parent


def parse_args():
    parser = argparse.ArgumentParser(description="AutoPDN V2 refactored runner")
    parser.add_argument("json", type=Path, help="Path to input JSON file")
    parser.add_argument("--stage", choices=("pre", "post", "full"), default="full")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_pipeline(input_json=args.json, stage=args.stage, working_dir=WORKING_DIR)


if __name__ == "__main__":
    main()
