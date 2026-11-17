from __future__ import annotations

import json
import re
from pathlib import Path


def extract_solver_root_cause(output: str) -> str:
    lines = [line.strip() for line in str(output or "").splitlines() if line.strip()]
    for line in lines:
        match = re.search(r"error\s*0?504\s*(.*)", line, re.IGNORECASE)
        if match:
            detail = match.group(1).strip().rstrip(":")
            return (f"{detail} (0504)" if detail else
                    "No valid terminals, ports, current or voltage sources were detected (0504)")
    for line in lines:
        match = re.search(r"error\s*(\d{3,4})\s*(.*)", line, re.IGNORECASE)
        if match:
            detail = match.group(2).strip().rstrip(":")
            return f"{detail} ({match.group(1)})" if detail else f"Simulation error ({match.group(1)})"
    return lines[-1] if lines else ""


def append_warning_json(output_dir: Path, total_cases: int, warnings, phase: str) -> Path:
    path = Path(output_dir) / "warning.json"
    existing = []
    if path.is_file():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            existing = payload.get("warnings", []) if isinstance(payload, dict) else []
        except (OSError, ValueError):
            existing = []
    tagged = []
    for warning in warnings:
        item = dict(warning)
        item.setdefault("phase", phase)
        tagged.append(item)
    merged = list(existing or []) + tagged
    payload = {"summary": {"totalCases": int(total_cases), "warningCases": len(merged)},
               "warnings": merged}
    path.write_text(json.dumps(payload, indent=4, ensure_ascii=False, default=str), encoding="utf-8")
    return path


def prepare_solver_exec(template_path: Path, output_dir: Path, cpu_count: int) -> Path:
    text = Path(template_path).read_text(encoding="utf-8")
    cpu_line = f"SetNumCpus {max(1, int(cpu_count))}"
    updated, count = re.subn(r"(?im)^\s*SetNumCpus\s+\d+\s*$", cpu_line, text, count=1)
    if not count:
        updated = f"{text.rstrip()}\n{cpu_line}\n"
    path = Path(output_dir) / ".PDN_runtime.exec"
    path.write_text(updated, encoding="utf-8")
    return path
