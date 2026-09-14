#!/usr/bin/env python3
"""Project-level PDN automation launcher.

This wrapper keeps stage execution deterministic:
- Validates key input artifacts from JSON.
- Auto-fixes BOM path when only a root-level BOM file exists.
- Applies runtime env flags in one place.
- Launches SIwave_PDN_V1_setting_VRM/main.py with selected stage.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, Tuple


ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_APP_DIR = ROOT_DIR / "SIwave_PDN_V1_setting_VRM"
DEFAULT_INPUT_JSON = ROOT_DIR / "65MRGB82.json"
DEFAULT_ZUKEN = Path(r"C:\Program Files\Zuken\CR-8000\Design Force\bin\DFevolv.cr5.exe")


def _bool_env(enabled: bool) -> str:
    return "1" if enabled else "0"


def _load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _find_preconverted(root: Path) -> Tuple[bool, bool]:
    has_dsgn = any(root.glob("*.dsgn"))
    has_aedb = any(root.glob("*.aedb"))
    return has_dsgn, has_aedb


def _resolve_required_inputs(root: Path, data: dict) -> Dict[str, Path]:
    cae = data.get("CAE", {})
    soc = cae.get("SOC", {})
    pcb = cae.get("PCB", {})
    return {
        "spec": root / str(soc.get("Spec", "")),
        "cad": root / str(pcb.get("cadFile", "")),
        "stackup": root / str(pcb.get("Stackup", "")),
        "bom": root / str(pcb.get("BOM", "")),
        "inner_cap": root / str(soc.get("Inner_cap", "")),
        "pmap": root / str(pcb.get("Pmap", "")),
    }


def _autofix_bom_if_needed(root: Path, input_json: Path, data: dict) -> Tuple[Path, bool]:
    pcb = data.setdefault("CAE", {}).setdefault("PCB", {})
    bom_raw = str(pcb.get("BOM", "")).strip()
    bom_path = root / bom_raw
    if not bom_raw:
        raise FileNotFoundError("CAE.PCB.BOM is empty in input JSON.")
    if bom_path.exists():
        return input_json, False

    fallback = root / Path(bom_raw).name
    if not fallback.exists():
        raise FileNotFoundError(
            f"BOM path not found in JSON and fallback not found:\n"
            f"- json path: {bom_path}\n"
            f"- fallback : {fallback}"
        )

    patched = dict(data)
    patched["CAE"] = dict(data.get("CAE", {}))
    patched["CAE"]["PCB"] = dict(data.get("CAE", {}).get("PCB", {}))
    patched["CAE"]["PCB"]["BOM"] = fallback.name

    out_path = input_json.with_name(f"{input_json.stem}.autofix.json")
    with out_path.open("w", encoding="utf-8") as fh:
        json.dump(patched, fh, ensure_ascii=False, indent=2)
    return out_path, True


def _print_check(label: str, path: Path, required: bool) -> bool:
    exists = path.exists()
    state = "OK" if exists else ("MISSING" if required else "WARN")
    print(f"[{state}] {label:<10} {path}")
    return exists


def _run(main_py: Path, run_json: Path, stage: str, env: dict, python_exec: str) -> int:
    cmd = [python_exec, str(main_py), str(run_json), "--stage", stage]
    print("\n[RUN]", " ".join(cmd))
    proc = subprocess.run(cmd, cwd=str(main_py.parent), env=env)
    return int(proc.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(description="PDN automation launcher")
    parser.add_argument("--stage", choices=["pre", "full", "post"], default="full")
    parser.add_argument("--input-json", type=Path, default=DEFAULT_INPUT_JSON)
    parser.add_argument("--app-dir", type=Path, default=DEFAULT_APP_DIR)
    parser.add_argument("--python", dest="python_exec", default=sys.executable)
    parser.add_argument("--use-preconverted", action="store_true", default=True)
    parser.add_argument("--no-use-preconverted", action="store_false", dest="use_preconverted")
    parser.add_argument("--enable-vrm-setup", action="store_true", default=True)
    parser.add_argument("--no-enable-vrm-setup", action="store_false", dest="enable_vrm_setup")
    parser.add_argument("--force-kill-siwave", action="store_true", default=True)
    parser.add_argument("--no-force-kill-siwave", action="store_false", dest="force_kill_siwave")
    parser.add_argument("--force-admin", action="store_true", default=False)
    args = parser.parse_args()

    input_json = args.input_json.resolve()
    app_dir = args.app_dir.resolve()
    main_py = app_dir / "main.py"

    print("=" * 72)
    print("PDN Automation Launcher")
    print(f"Root  : {ROOT_DIR}")
    print(f"App   : {app_dir}")
    print(f"Stage : {args.stage}")
    print(f"Input : {input_json}")
    print("=" * 72)

    if not main_py.exists():
        print(f"[ERROR] main.py not found: {main_py}")
        return 1
    if not input_json.exists():
        print(f"[ERROR] input json not found: {input_json}")
        return 1

    data = _load_json(input_json)
    required = _resolve_required_inputs(ROOT_DIR, data)

    ok = True
    ok &= _print_check("spec", required["spec"], required=True)
    ok &= _print_check("cad", required["cad"], required=args.stage in {"pre", "full"})
    ok &= _print_check("stackup", required["stackup"], required=args.stage in {"pre", "full"})
    ok &= _print_check("inner_cap", required["inner_cap"], required=args.stage in {"pre", "full"})
    _print_check("pmap", required["pmap"], required=False)
    if not ok:
        print("[ERROR] Missing required inputs.")
        return 1

    try:
        run_json, patched = _autofix_bom_if_needed(ROOT_DIR, input_json, data)
    except FileNotFoundError as exc:
        print(f"[ERROR] {exc}")
        return 1
    if patched:
        print(f"[INFO] BOM path auto-fixed in temporary JSON: {run_json}")

    if args.stage in {"pre", "full"}:
        has_dsgn, has_aedb = _find_preconverted(ROOT_DIR)
        effective_preconverted = bool(args.use_preconverted)
        if effective_preconverted and not (has_dsgn or has_aedb):
            print("[WARN] Preconverted requested but no .dsgn/.aedb found. Falling back to normal conversion.")
            effective_preconverted = False
        if not effective_preconverted and not DEFAULT_ZUKEN.exists():
            print(f"[ERROR] Zuken converter not found: {DEFAULT_ZUKEN}")
            return 1
    else:
        effective_preconverted = bool(args.use_preconverted)

    env = os.environ.copy()
    env["PDN_USE_PRECONVERTED"] = _bool_env(effective_preconverted)
    env["PDN_ENABLE_VRM_SETUP"] = _bool_env(args.enable_vrm_setup)
    env["PDN_FORCE_KILL_SIWAVE"] = _bool_env(args.force_kill_siwave)
    env["PDN_FORCE_ADMIN"] = _bool_env(args.force_admin)

    print("[ENV] PDN_USE_PRECONVERTED =", env["PDN_USE_PRECONVERTED"])
    print("[ENV] PDN_ENABLE_VRM_SETUP =", env["PDN_ENABLE_VRM_SETUP"])
    print("[ENV] PDN_FORCE_KILL_SIWAVE =", env["PDN_FORCE_KILL_SIWAVE"])
    print("[ENV] PDN_FORCE_ADMIN      =", env["PDN_FORCE_ADMIN"])

    rc = _run(main_py, run_json, args.stage, env, args.python_exec)
    if rc == 0:
        print("[DONE] Stage completed successfully.")
    else:
        print(f"[FAIL] Stage failed with exit code {rc}.")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
