# coding=utf-8
# <2025> ANSYS, Inc. Unauthorized use, distribution, or duplication is prohibited

import json
import csv
import traceback
import time
import os
import argparse
import zipfile
import subprocess
import shutil
import atexit
import re
import glob  
import math
import psutil
import logging
import ctypes
import sys
from collections import defaultdict
from importlib import metadata as importlib_metadata
from pathlib import Path

from core.SettingsManager import SettingsManager, MAX_RETRIES, RETRY_DELAY
from core.logger import Logger, LogLevel
from core.database import PDNSessionException, ErrorCode, InValChk, Database, PDNCase, extract_voltage, sanitize_str
from core.post_processing import PostProcessing
from core.post_stage import PostStageError, append_post_detail, prepare_post_settings, reconstruct_post_state
from core import pdn_setup_utils
from core import vrm_setup

MODE = 0  # Generic Mode

if not MODE == 2:
    from EBU_lib.SIwave import SIwave
    from EBU_lib.PDN import PDN
    from EBU_lib.AEDT import AEDT


# region Global Variables
logger = Logger(name="PDN")
step = 0
UTILITY_NAME = "AutoPDN"
VERSION = "1.4"
START_TIME = None
END_TIME = None
RUN_TAG = None
WORKING_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
INPUT_JSON = Path()
INPUT_DIR = Path()
db = None  
bom_info = {} 
pdn_cases_info = []
inner_cap_audit = []
PRE_EDB_FILE_PATH = None
STACKUP_LAYER_COUNT = None
EDB_SETUP_APP = None
STACKUP_INPUT_FILE = None
STACKUP_EFFECTIVE_FILE = None
STACKUP_APPLIED_AT_PROJECT_CREATION = False
FINAL_EDB_FILE_PATH = None
CMP_FILE_PATH = None
SOLVER_BACKEND_USED = "siwave"
# endregion

# region Terminate & Save Log (atexit 등록)
def terminate_and_save_log():
    try:
        logger.log("#" * 100, level=LogLevel.SECTION, line_change=False)
        logger.log("|", level=LogLevel.SECTION, line_change=False)
        logger.log(f"| Terminate Ansys {UTILITY_NAME} v{VERSION} on {time.strftime('%Y.%m.%d, %H:%M:%S')}", level=LogLevel.SECTION, line_change=False)
        logger.log("|", level=LogLevel.SECTION, line_change=False)
        logger.log("#" * 100, level=LogLevel.SECTION, line_change=False)
        
        target_process_name = "siwave_ng.exe"
        for proc in psutil.process_iter(['pid', 'name']):
            try:
                proc_name = proc.info['name']
                if proc_name and target_process_name in proc_name.lower():
                    logger.log(f"좀비 프로세스 발견: {proc_name} (PID: {proc.info['pid']}). 강제 종료를 시도합니다.", level=LogLevel.WARNING)
                    proc.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
        
        save_dir = str(INPUT_DIR) if str(INPUT_DIR) != "." else str(WORKING_DIR)
        logger.save(save_dir)
    except Exception as e:
        print(f"Failed to save log during termination: {e}")

atexit.register(terminate_and_save_log)
# endregion

# region Functions
def apply_dynamic_frequency_setup(app, layer_count, conf_data, logger):
    try:
        current_profile = "2L" if (layer_count is None or layer_count <= 2) else "4L_plus"
        layer_cfg = conf_data.get("PDN", {}).get("zParamSetup", {}).get("profiles", {}).get(current_profile, {})
        setup_name = layer_cfg.get("setup_name", f"SYZ_Setup_{current_profile}")
        
        setup = app.create_syz_setup(name=setup_name)
        sweep_data = layer_cfg.get("sweep_data", "")
        if not sweep_data:
            logger.log(f"[PRE] sweep_data가 비어있습니다. Config 파일을 확인해주세요.", level=LogLevel.WARNING)
            return

        tokens = sweep_data.split()
        for i in range(0, len(tokens), 4):
            if i + 3 < len(tokens):
                s_type = tokens[i]
                start_f = tokens[i+1]
                stop_f = tokens[i+2]
                count = int(tokens[i+3])
                
                setup.add_sweep(
                    name=f"Sweep_{i//4 + 1}",
                    start_freq=start_f,
                    stop_freq=stop_f,
                    count=count,
                    freq_sweep_type="kDecadeCount" if s_type == "DEC" else "kLinearCount",
                    sweep_type="Interpolating"
                )
        logger.log(f"[PRE] {layer_count} Layer 감지됨. Config 문자열 기반 주파수 셋업 완료 ({setup_name}).", level=LogLevel.DETAIL1)
    except Exception as e:
        logger.log(f"[PRE] 주파수 셋업 생성 실패: {e}", level=LogLevel.WARNING)

def safe_close_edb_session(edb_app, logger, context=""):
    if not edb_app:
        return
    try:
        if hasattr(edb_app, "close_edb"):
            edb_app.close_edb()
    except Exception as e:
        ctx = f" ({context})" if context else ""
        logger.log(f"[WARNING] Failed to close EDB session{ctx}: {e}", level=LogLevel.WARNING)

def is_running_as_admin() -> bool:
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False

def ensure_admin_if_requested(logger: Logger) -> None:
    force_admin = os.environ.get("PDN_FORCE_ADMIN", "").strip().lower() in {"1", "true", "y", "yes", "on"}
    if not force_admin:
        return
    if is_running_as_admin():
        logger.log("[PREFLIGHT] Running with administrator privileges.", level=LogLevel.DETAIL1)
        return
    if os.name != "nt":
        logger.log("[PREFLIGHT][WARNING] PDN_FORCE_ADMIN is enabled but current OS is not Windows.", level=LogLevel.WARNING)
        return

    script_args = " ".join(f"\"{arg}\"" for arg in sys.argv)
    try:
        logger.log("[PREFLIGHT] PDN_FORCE_ADMIN=1 detected. Relaunching as Administrator...", level=LogLevel.WARNING)
        result = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, script_args, None, 1)
        if int(result) <= 32:
            logger.log(f"[PREFLIGHT][WARNING] Administrator relaunch failed. ShellExecuteW code={result}", level=LogLevel.WARNING)
            return
        logger.log("[PREFLIGHT] Elevated process started successfully. Current process exits.", level=LogLevel.WARNING)
        raise SystemExit(0)
    except SystemExit:
        raise
    except Exception as e:
        logger.log(f"[PREFLIGHT][WARNING] Administrator relaunch error: {e}", level=LogLevel.WARNING)

def cleanup_siwave_background_processes(logger: Logger) -> None:
    enabled = os.environ.get("PDN_FORCE_KILL_SIWAVE", "").strip().lower() in {"1", "true", "y", "yes", "on"}
    if not enabled:
        logger.log("[PREFLIGHT] Background SIwave cleanup disabled (PDN_FORCE_KILL_SIWAVE=0).", level=LogLevel.DETAIL1)
        return

    target_names = {"siwave_ng.exe", "siwave.exe"}
    found = []
    for proc in psutil.process_iter(['pid', 'name']):
        try:
            name = (proc.info.get('name') or "").lower()
            if name in target_names:
                found.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue

    if not found:
        logger.log("[PREFLIGHT] No existing SIwave background process found.", level=LogLevel.DETAIL1)
        return

    logger.log(f"[PREFLIGHT] Found {len(found)} SIwave process(es). Terminating before run.", level=LogLevel.WARNING)
    for proc in found:
        try:
            proc.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    _, alive = psutil.wait_procs(found, timeout=5)
    for proc in alive:
        try:
            proc.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
            continue
    logger.log(f"[PREFLIGHT] SIwave process cleanup completed. Remaining={len(alive)}", level=LogLevel.DETAIL1)

def log_runtime_preflight(logger: Logger, aedt_version: str) -> None:
    logger.log(f"[PREFLIGHT] Python: {sys.version.split()[0]} ({sys.executable})", level=LogLevel.DETAIL1)
    logger.log(f"[PREFLIGHT] Admin privileges: {is_running_as_admin()}", level=LogLevel.DETAIL1)
    if not is_running_as_admin():
        logger.log("[PREFLIGHT][WARNING] Not running as Administrator. Some COM APIs can be blocked by Windows policy.", level=LogLevel.WARNING)

    clean_version = aedt_version.replace('20', '', 1).replace('.', '')
    selected_env = f"ANSYSEM_ROOT{clean_version}"
    selected_root = os.environ.get(selected_env)
    all_roots = sorted([k for k in os.environ.keys() if k.startswith("ANSYSEM_ROOT")])
    logger.log(f"[PREFLIGHT] AEDT target version: {aedt_version} | expected env: {selected_env}", level=LogLevel.DETAIL1)
    logger.log(f"[PREFLIGHT] Detected ANSYSEM_ROOT vars: {all_roots}", level=LogLevel.DETAIL1)
    if not selected_root:
        logger.log(f"[PREFLIGHT][WARNING] {selected_env} is not set.", level=LogLevel.WARNING)
    else:
        siwave_path = Path(selected_root) / "siwave_ng.exe"
        logger.log(f"[PREFLIGHT] {selected_env}={selected_root}", level=LogLevel.DETAIL1)
        if siwave_path.exists():
            logger.log(f"[PREFLIGHT] SIwave executable found: {siwave_path}", level=LogLevel.DETAIL1)
        else:
            logger.log(f"[PREFLIGHT][WARNING] SIwave executable missing at: {siwave_path}", level=LogLevel.WARNING)

    for pkg_name in ("pyaedt", "pyedb"):
        try:
            logger.log(f"[PREFLIGHT] {pkg_name} version: {importlib_metadata.version(pkg_name)}", level=LogLevel.DETAIL1)
        except Exception:
            logger.log(f"[PREFLIGHT][WARNING] Could not read package version: {pkg_name}", level=LogLevel.WARNING)

def cleanup_failed_files(target_dir):
    patterns_to_delete = [
        "*.crdb", "outputs", "*.coh", "Design data*", 
        "*.ndf", "PCBFile*", "*.ruf", "*.rul", "*.txt"
    ]
    for pattern in patterns_to_delete:
        matched_paths = glob.glob(os.path.join(target_dir, pattern))
        for path in matched_paths:
            try:
                if os.path.isfile(path):
                    os.remove(path)
                elif os.path.isdir(path):
                    shutil.rmtree(path)
            except Exception as e:
                logger.log(f"삭제 실패: {path} - {e}", level=LogLevel.WARNING)

def resolve_temp_preconverted_files(input_cad_file: Path, logger: Logger):
    use_preconverted = os.environ.get("PDN_USE_PRECONVERTED", "").strip().lower() in {"1", "true", "y", "yes", "on"}
    if not use_preconverted:
        return None

    work_dir = input_cad_file.parent
    base_stem = input_cad_file.stem.split('-')[0]

    preferred_dsgn = work_dir / f"{input_cad_file.stem}.dsgn"
    dsgn_file = preferred_dsgn if preferred_dsgn.exists() else next(iter(sorted(work_dir.glob("*.dsgn"), key=lambda p: p.stat().st_mtime, reverse=True)), None)

    preferred_edb = work_dir / f"{base_stem}.aedb"
    edb_file = preferred_edb if preferred_edb.exists() else next(iter(sorted(work_dir.glob("*.aedb"), key=lambda p: p.stat().st_mtime, reverse=True)), None)

    if not dsgn_file and not edb_file:
        return None

    logger.log("[TEMP] Preconverted artifacts detected. Zuken conversion will be skipped.", level=LogLevel.WARNING)
    return {"DSGN_FILE": dsgn_file, "EDB_FILE_PATH": edb_file, "SKIP_ZUKEN_CONVERT": True}

def _is_edb_ready(edb_path: Path) -> bool:
    try:
        edb_def = edb_path / "edb.def"
        return edb_path.exists() and edb_def.exists() and edb_def.stat().st_size > 0
    except OSError:
        return False

def wait_for_edb_ready(edb_path: Path, timeout: float = 300.0, check_interval: float = 3.0, stable_checks: int = 2) -> bool:
    start = time.monotonic()
    stable_count = 0
    while time.monotonic() - start < timeout:
        if _is_edb_ready(edb_path):
            stable_count += 1
            if stable_count >= stable_checks:
                return True
        else:
            stable_count = 0
        time.sleep(check_interval)
    return False

def run_external_tool(cmd, tool_name: str):
    result = subprocess.run(cmd, capture_output=True, text=True)
    logger.log(f"[TOOL] {tool_name} rc={result.returncode}", level=LogLevel.DETAIL1)
    if result.stderr and result.stderr.strip():
        logger.log(f"[TOOL][{tool_name}][stderr]\n{result.stderr.strip()}", level=LogLevel.WARNING)
    return result

def ensure_pre_edb_saved(app, source_edb_path: Path, pre_edb_path: Path, max_retries: int = 2, timeout: float = 300.0):
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            if pre_edb_path.exists():
                shutil.rmtree(pre_edb_path, ignore_errors=True)
            app.edb.save_as(str(pre_edb_path))
            app.close_edb()
            if wait_for_edb_ready(pre_edb_path, timeout=timeout):
                return
            raise TimeoutError(f"edb.def not ready within {timeout:.1f}s after save_as: {pre_edb_path}")
        except Exception as exc:
            last_error = exc
            if attempt < max_retries:
                try:
                    app.set_cad_file(source_edb_path)
                except Exception:
                    pass
                time.sleep(5.0)
    raise FileNotFoundError(f"Failed to create a ready PRE EDB after {max_retries} attempts. Last error: {last_error}")

def incubator():
    OUTPUT_DIR = Path(r"D:\2_QC\LGE_MS_PDN\v0p99\Zuken_to_ODB")
    DSGN_FILE = Path(r"D:\2_QC\LGE_MS_PDN\v0p99\Zuken_to_ODB\test_design.dsgn")
    if OUTPUT_DIR.exists() and DSGN_FILE.exists():
        shutil.make_archive(OUTPUT_DIR / DSGN_FILE.stem, 'zip', OUTPUT_DIR, DSGN_FILE.stem)
        ZIP_FILE = OUTPUT_DIR / f"{DSGN_FILE.stem}.zip"
        shutil.move(ZIP_FILE, ZIP_FILE.with_suffix('.tgz'))

def trace_pdn_power_path(edb, net_inst, designator_list, bom_info, gndNet, pre_bead=None, net_chain=None, comp_chain=None, target_ic=None):
    net_chain = net_chain or [net_inst.name]
    comp_chain = comp_chain or []
    LDO_list, inductors, fet_list, switch_list, source_list = find_power_components_in_net(net_inst, designator_list, bom_info)
    found_sources = []
    for src in source_list: found_sources.append({'type': 'SOURCE', 'inst': src, 'chain': net_chain, 'comp_chain': comp_chain})
    for ldo in LDO_list: found_sources.append({'type': 'LDO', 'inst': ldo, 'chain': net_chain, 'comp_chain': comp_chain})
    
    def add_to_other_net(current_net, next_net):
        if next_net not in db.other_nets.setdefault(current_net, []):
            db.other_nets[current_net].append(next_net)
            
    for ind_inst in inductors["bulk"]:
        other_net_name = next((x for x in ind_inst.nets if x != net_inst.name), None)
        if other_net_name and other_net_name in edb._nets.nets and other_net_name not in net_chain:
            add_to_other_net(net_inst.name, other_net_name)
            upstream_sources = trace_pdn_power_path(edb, edb._nets.nets[other_net_name], designator_list, bom_info, gndNet, pre_bead=None, net_chain=net_chain + [other_net_name], comp_chain=comp_chain + [f"{ind_inst.name}(BulkInd)"], target_ic=target_ic)
            found_sources.extend(upstream_sources)
            
    for switch_inst in switch_list:
        candidate_nets = {}
        analog_config = bom_info.get('config', {}).get('analogSwitch', {})
        raw_connect_pins = analog_config.get('connectPin', [])
        total_pins = len(switch_inst.pins)
        target_pair = next(([str(p).strip() for p in pair] for pair in raw_connect_pins if isinstance(pair, list) and total_pins in pair), [str(p).strip() for p in raw_connect_pins])
        connect_types_upper = {str(t).strip().upper() for t in analog_config.get('connectType', ['IN', 'OUT'])}
        matched_pins, pin_match_pins = {}, []
        for pin_name, pin_inst in switch_inst.pins.items():
            clean_pin_name = str(pin_name).strip()
            pin_upper, net_upper = clean_pin_name.upper(), (str(pin_inst.net_name).upper() if pin_inst.net_name else "")
            for c_type in connect_types_upper:
                if c_type not in matched_pins and (c_type in pin_upper or c_type in net_upper):
                    matched_pins[c_type] = pin_inst
                    break
            if clean_pin_name in target_pair: pin_match_pins.append(pin_inst)
        jump_pins = None
        if connect_types_upper and len(matched_pins) >= 2:
            base_type = next((c_type for c_type, p_inst in matched_pins.items() if p_inst.net_name == net_inst.name), None)
            if base_type: jump_pins = [p_inst for c_type, p_inst in matched_pins.items() if c_type != base_type]
        elif len(pin_match_pins) >= len(target_pair) and target_pair:
            base_pin_inst = next((p_inst for p_inst in pin_match_pins if p_inst.net_name == net_inst.name), None)
            if base_pin_inst: jump_pins = [p_inst for p_inst in pin_match_pins if p_inst != base_pin_inst]
        if jump_pins:
            for other_pin_inst in jump_pins:
                other_net_name = other_pin_inst.net_name
                if other_net_name and other_net_name not in (net_inst.name, gndNet) and other_net_name in edb._nets.nets and other_net_name not in net_chain:
                    candidate_nets[other_net_name] = edb._nets.nets[other_net_name]
        for other_net_name, other_net_inst in candidate_nets.items():
            add_to_other_net(net_inst.name, other_net_name)
            upstream_sources = trace_pdn_power_path(edb, other_net_inst, designator_list, bom_info, gndNet, pre_bead=None, net_chain=net_chain + [other_net_name], comp_chain=comp_chain + [f"{switch_inst.name}(Switch)"], target_ic=target_ic)
            found_sources.extend(upstream_sources)
            
    for fet_inst in fet_list:
        candidate_nets = {}
        comp_type = 'FET' if fet_inst.name in bom_info.get('FET', []) else 'TR'
        connect_pins_str = [str(p).strip() for p in bom_info.get('config', {}).get(comp_type, {}).get('connectPin', [1, 3])]
        base_pin = next((str(p_name).strip() for p_name, p_inst in fet_inst.pins.items() if p_inst.net_name == net_inst.name and str(p_name).strip() in connect_pins_str), None)
        if base_pin:
            for other_pin in [p for p in connect_pins_str if p != base_pin]:
                other_pin_inst = fet_inst.pins.get(other_pin)
                if other_pin_inst and other_pin_inst.net_name and other_pin_inst.net_name not in (net_inst.name, gndNet) and other_pin_inst.net_name in edb._nets.nets and other_pin_inst.net_name not in net_chain:
                    candidate_nets[other_pin_inst.net_name] = edb._nets.nets[other_pin_inst.net_name]
        for other_net_name, other_net_inst in candidate_nets.items():
            add_to_other_net(net_inst.name, other_net_name)
            upstream_sources = trace_pdn_power_path(edb, other_net_inst, designator_list, bom_info, gndNet, pre_bead=None, net_chain=net_chain + [other_net_name], comp_chain=comp_chain + [f"{fet_inst.name}(FET)"], target_ic=target_ic)
            found_sources.extend(upstream_sources)
            
    if pre_bead: inductors["bead"] = [bead for bead in inductors["bead"] if bead.name != pre_bead.name]
    for bead_inst in inductors["bead"]:
        other_net_inst = find_other_net_ind(edb, bead_inst, net_inst.name)
        if other_net_inst and other_net_inst.name not in net_chain:
            add_to_other_net(net_inst.name, other_net_inst.name)
            upstream_sources = trace_pdn_power_path(edb, other_net_inst, designator_list, bom_info, gndNet, pre_bead=bead_inst, net_chain=net_chain + [other_net_inst.name], comp_chain=comp_chain + [f"{bead_inst.name}(Bead)"], target_ic=target_ic)
            found_sources.extend(upstream_sources)
    return found_sources

def filter_real_source(candidates, start_net_name):
    target_voltage = extract_voltage(start_net_name)
    matched_candidates, unmatched_candidates = [], []
    for src in candidates:
        src_voltage = extract_voltage(src['chain'][-1])
        if target_voltage is not None and src_voltage is not None:
            (matched_candidates if target_voltage == src_voltage else unmatched_candidates).append(src)
        else:
            matched_candidates.append(src)
    target_group = matched_candidates or unmatched_candidates
    if not target_group: return None
    bulk_candidates = [src for src in target_group if 'BulkInd' in str(src['comp_chain'])]
    fet_candidates = [src for src in target_group if 'BulkInd' not in str(src['comp_chain'])]
    if bulk_candidates: return min(bulk_candidates, key=lambda x: len(x['comp_chain']))
    if fet_candidates: return min(fet_candidates, key=lambda x: len(x['comp_chain']))
    return None

def find_power_source(edb, net_inst, designator_list, bom_info, gndNet, pre_bead=None, net_chain=None, target_ic=None):
    initial_chain = net_chain or [net_inst.name]
    all_sources = trace_pdn_power_path(edb, net_inst, designator_list, bom_info, gndNet, pre_bead=pre_bead, net_chain=net_chain, comp_chain=None, target_ic=target_ic)
    if not all_sources: return ErrorCode.NO_SOURCE_FOUND, initial_chain 
    unique_sources = {'SOURCE': {}, 'LDO': {}}
    for src in all_sources: unique_sources[src['type']][src['inst'].name] = src
    for src_type, err_code in [('SOURCE', ErrorCode.INVALID_SOURCE_NUM), ('LDO', ErrorCode.INVALID_LDO_NUM)]:
        src_list = list(unique_sources[src_type].values())
        if src_list:
            if len(src_list) == 1: return src_list[0]['inst'], src_list[0]['chain']
            else:
                selected = filter_real_source(src_list, initial_chain[0])
                if selected: return selected['inst'], selected['chain']
                else: return err_code, initial_chain
    return ErrorCode.NO_SOURCE_FOUND, initial_chain 

def find_other_net_ind(edb, inductor, net_name):
    other_net = next((x for x in inductor.nets if x != net_name), None)
    return edb._nets.nets[other_net] if other_net else None

def find_power_components_in_net(net_inst, designator_list, bom):
    LDO_list, inductors, fet_list, switch_list, source_list = [], {"bulk": [], "bead": []}, [], [], []
    for name, inst in net_inst.components.items():
        if name in bom.get('bulkInd', []): inductors['bulk'].append(inst)
        elif name in bom.get('beadInd', []): inductors['bead'].append(inst)
        elif name not in designator_list and name in bom.get('Designators', []):
            if name in bom.get('sourceComp', []): source_list.append(inst)
            elif name in bom.get('LDO', []): LDO_list.append(inst)
            elif name in bom.get('FET', []) or name in bom.get('TR', []): fet_list.append(inst)
            elif name in bom.get('analogSwitch', []): switch_list.append(inst)
    return LDO_list, inductors, fet_list, switch_list, source_list

def make_tgz(source_dir: Path, logger: Logger | None = None):
    try:
        parent_dir = source_dir.parent
        target_folder_name = source_dir.name.split('-')[0]
        actual_source_dir = parent_dir / target_folder_name
        if not actual_source_dir.exists():
            found = False
            for child in parent_dir.iterdir():
                if child.is_dir() and child.name.lower() == target_folder_name.lower():
                    actual_source_dir = child
                    found = True
                    break
            if not found: raise FileNotFoundError(f"ODB++ directory not found: {target_folder_name}")
        dest_dir = parent_dir / 'outputs'
        dest_dir.mkdir(exist_ok=True)
        archive_base = str(dest_dir / source_dir.name)
        shutil.make_archive(archive_base, 'gztar', root_dir=actual_source_dir.parent, base_dir=actual_source_dir.name)
        tar_gz_file = Path(f"{archive_base}.tar.gz")
        tgz_file = Path(f"{archive_base}.tgz")
        if tar_gz_file.exists():
            shutil.move(str(tar_gz_file), str(tgz_file))
            return True
    except Exception as e:
        if logger: logger.log(f"[ERROR] Failed to create TGZ: {e}", level=LogLevel.ERROR)
    return False

def set_case_error_defaults(case):
    case['is_done'] = False
    case['Result'] = 0.0
    case['Drop Voltage'] = 0.0
    case['Drop Rate'] = 0.0
    case['Pass/Fail'] = 'Error'
    case['FitView'] = ""
    case['ZoomView'] = ""
    case['Field_Case'] = ""
    case['Mesh_Case'] = ""

def parse_numeric(value, default=None):
    if value is None: return default
    if isinstance(value, (int, float)): return float(value)
    text = str(value).strip()
    if not text: return default
    if text[0] in ("<", ">"): text = text[1:].strip()
    text = text.replace("V", "").replace("A", "").strip()
    try: return float(text)
    except Exception: return default

def evaluate_pin_mapping_quality(mode: str, trace_meta=None):
    mode_u = str(mode or "").strip().lower()
    trace_meta = trace_meta or {}
    trace_status = str(trace_meta.get("status", "")).strip().lower()
    trace_reason = str(trace_meta.get("reason", "")).strip()

    hard_skip_tokens = (
        "none",
        "no_net_candidate",
        "coord_rejected",
        "invalid_input",
    )
    if trace_status == "skip" or any(tok in mode_u for tok in hard_skip_tokens):
        note = trace_reason or mode or "unresolved"
        return "SKIP", "LOW", note

    if mode_u in {
        "exact",
        "pin_override",
        "designator_pin_exact",
        "designator_pin_display",
        "designator_pin_ui_api_exact",
        "designator_pin_ui_api_token",
        "designator_pin_crosswalk",
        "designator_pin_dotnet_siw_name_net",
        "designator_pin_dotnet_siw_name_softnet",
        "designator_pin_dotnet_siw_coord_net",
        "designator_pin_dotnet_siw_coord_softnet",
        "designator_pin_coord_cmp",
        "designator_pin_coord_siw",
        "crosswalk_pin",
        "spec_api_gui",
        "spec_api_ui_relation",
        "strict_net_unique",
        "strict_net_multi_coord",
        "net_fix_crosswalk",
        "net_reverse_ui_verified",
        "coord_net_validated",
        "net_exact",
        "net_multi_coord",
        "net_multi_cmp_coord",
        "global_padstack_scan_recover",
        "spatial_query_recover",
    }:
        return "PASS", "HIGH", mode or "resolved"

    if mode_u == "net_only_fallback":
        if trace_status == "ok":
            note = f"{mode} ({trace_reason or 'trace_ok'})"
            return "REVIEW", "MEDIUM", note
        note = trace_reason or mode or "trace_failed"
        return "SKIP", "LOW", note

    if "coord_only" in mode_u or "fallback" in mode_u:
        return "REVIEW", "LOW", mode or "coord/fallback mapping"

    if mode_u:
        return "REVIEW", "MEDIUM", mode
    return "SKIP", "LOW", "empty_mode"

def find_nearest_gnd_pin(edb, ref_coord, gnd_net):
    if not ref_coord or (math.isclose(ref_coord[0], 0.0, abs_tol=1e-9) and math.isclose(ref_coord[1], 0.0, abs_tol=1e-9)):
        components_dict = getattr(edb, 'core_components', getattr(edb, 'components', getattr(edb, '_components', None)))
        if components_dict and hasattr(components_dict, 'components'):
            for comp in components_dict.components.values():
                for pin in comp.pins.values():
                    if pin.net_name == gnd_net: return pin
        return None

    min_dist = float("inf")
    best_pin = None
    components_dict = getattr(edb, 'core_components', getattr(edb, 'components', getattr(edb, '_components', None)))
    if components_dict and hasattr(components_dict, 'components'):
        for comp in components_dict.components.values():
            for pin in comp.pins.values():
                if pin.net_name != gnd_net: continue
                dist = (pin.position[0] - ref_coord[0]) ** 2 + (pin.position[1] - ref_coord[1]) ** 2
                if dist < min_dist:
                    min_dist = dist
                    best_pin = pin
    return best_pin

def build_pre_stage_case_record(case, index):
    target_net_display = case.get("Display_Net", case.get("Spec_Net", case.get("Net", "")))
    return {
        "Schema_Version": 1,
        "Generated_From": "Spec",
        "Case_Index": index + 1,
        "IC_Designator": case.get("IC", ""),
        "Spec_Pin": case.get("Spec_Pin", ""),
        "IC_Pin": case.get("IC_pin", ""),
        "Target_Net": target_net_display,
        "PCB_Target_Net": case.get("Net", ""),
        "Spec_Target_Net": case.get("Spec_Net", ""),
        "Source_Component": case.get("Source_name", ""),
        "Source_Pin": case.get("Source_pin", ""),
        "Net_Chain": case.get("Source_net_chain", []),
        "Full_Net_Chain": case.get("Full_Net_Chain", []),
        "Voltage_V": case.get("Vmag", 0.0),
        "Current_A": case.get("Imag", 0.0),
        "Min_Spec_V": case.get("MinSpec", 0.0),
        "Max_Spec_V": case.get("MaxSpec", 0.0),
        "Mapping_Mode": case.get("Mapping_Mode", ""),
        "Mapping_Trace": case.get("Mapping_Trace", {}),
        "Mapping_Status": case.get("Mapping_Status", ""),
        "Mapping_Confidence": case.get("Mapping_Confidence", ""),
        "Mapping_Note": case.get("Mapping_Note", ""),
    }

def export_pre_stage_reports(
    output_dir: Path,
    spec_file: Path,
    pre_edb_path: Path,
    cases,
    inner_caps,
    logger: Logger,
    s2p_dir: Path | None = None,
):
    output_dir.mkdir(parents=True, exist_ok=True)
    pre_records = [build_pre_stage_case_record(case, idx) for idx, case in enumerate(cases)]

    pre_file = output_dir / "preprocessing_result.json"
    with open(pre_file, "w", encoding="utf-8") as f:
        json.dump(pre_records, f, indent=4, ensure_ascii=False)

    success_count = sum(1 for item in inner_caps if item.get("status") == "created")
    fail_count = sum(1 for item in inner_caps if item.get("status") != "created")
    innercap_report = {
        "Schema_Version": 1,
        "Mode": "pre",
        "Spec_File": str(spec_file),
        "Pre_Edb_Path": str(pre_edb_path) if pre_edb_path else "",
        "Expected_InnerCap_Count": len(inner_caps),
        "Created_InnerCap_Count": success_count,
        "Failed_InnerCap_Count": fail_count,
        "All_InnerCaps_Created": (len(inner_caps) > 0 and fail_count == 0),
        "Details": inner_caps,
    }

    inner_file = output_dir / "innercap_verification.json"
    with open(inner_file, "w", encoding="utf-8") as f:
        json.dump(innercap_report, f, indent=4, ensure_ascii=False)

    logger.log(f"[PRE] Exported net search report: {pre_file}", level=LogLevel.DETAIL1)
    logger.log(f"[PRE] Exported inner cap verification report: {inner_file}", level=LogLevel.DETAIL1)
    # PDN 해석용이므로 pmap_file 인자는 None으로 고정 전달
    export_innercap_s2p_registry(output_dir, inner_caps, None, logger, s2p_dir=s2p_dir)

def export_innercap_s2p_registry(output_dir: Path, inner_caps, pmap_file: Path | None, logger: Logger, s2p_dir: Path | None = None):
    return pdn_setup_utils.export_innercap_s2p_registry(output_dir, inner_caps, pmap_file, logger, s2p_dir=s2p_dir)

def assign_sparameter_models(
    app,
    bom_info,
    inner_cap_audit,
    pmap_file: Path | None,
    s2p_dir: Path | None,
    gnd_net: str,
    output_dir: Path,
    logger: Logger,
    innercap_csv_path: Path | None = None,
    bom_file_path: Path | None = None,
    search_roots=None,
):
    return pdn_setup_utils.assign_sparameter_models(
        app=app,
        bom_info=bom_info,
        inner_cap_audit=inner_cap_audit,
        pmap_file=pmap_file,
        s2p_dir=s2p_dir,
        gnd_net=gnd_net,
        output_dir=output_dir,
        logger=logger,
        innercap_csv_path=innercap_csv_path,
        bom_file_path=bom_file_path,
        search_roots=search_roots,
    )

def resolve_zparam_profile(conf_data, stackup_file: Path | None, layer_count: int | None):
    return pdn_setup_utils.resolve_zparam_profile(conf_data, stackup_file, layer_count)

def build_pre_stage_siw_snapshot(
    *,
    aedt_version: str,
    pre_edb_path: Path,
    siw_output_path: Path,
    conf_data,
    settings_data,
    input_dir: Path,
    working_dir: Path,
    stackup_input: Path | None,
    layer_count: int | None,
    bom_info,
    inner_cap_audit,
    gnd_net: str,
    output_dir: Path,
    logger: Logger,
    import_stackup_to_snapshot: bool = True,
):
    if not _is_edb_ready(pre_edb_path):
        raise FileNotFoundError(f"PRE EDB is not ready for SIW snapshot: {pre_edb_path}")

    app = None
    try:
        app = SIwave(version=aedt_version, logger=logger)
        app.import_edb(str(pre_edb_path))
        app.set_cad_file(pre_edb_path)

        profile_key, sws_name, sfsdf_name = resolve_zparam_profile(
            conf_data,
            stackup_input,
            layer_count,
        )
        if stackup_input and import_stackup_to_snapshot:
            raw_stackup = Path(stackup_input)
            if app.import_layer_stackup(raw_stackup):
                logger.log(f"[PRE] Raw stackup imported to SIW project: {raw_stackup}", level=LogLevel.DETAIL1)
            else:
                raise RuntimeError(f"[PRE] Failed to import raw stackup file: {raw_stackup}")
        
        sws_file = working_dir / 'core' / sws_name
        
        s2p_dir_conf = conf_data.get('PDN', {}).get('sParameter', {}).get('s2pDirectory', '')
        s2p_dir = None
        if s2p_dir_conf:
            candidate_dir = Path(s2p_dir_conf)
            s2p_dir = candidate_dir if candidate_dir.is_absolute() else (input_dir / candidate_dir)

        if conf_data.get('PDN', {}).get('sParameter', {}).get('enableAssign', True):
            innercap_name = settings_data.get('CAE', {}).get('SOC', {}).get('Inner_cap')
            innercap_csv_path = (input_dir / innercap_name) if innercap_name else None
            bom_name = settings_data.get('CAE', {}).get('PCB', {}).get('BOM')
            bom_file_path = (input_dir / bom_name) if bom_name else None
            model_lib_candidates = conf_data.get('PDN', {}).get('sParameter', {}).get('model_library_dir_candidates', [])
            resolved_candidates = [Path(cand) if Path(cand).is_absolute() else (input_dir / Path(cand)) for cand in model_lib_candidates if cand]
            assign_sparameter_models(
                app=app,
                bom_info=bom_info,
                inner_cap_audit=inner_cap_audit,
                pmap_file=None, 
                s2p_dir=s2p_dir,
                gnd_net=gnd_net,
                output_dir=output_dir,
                logger=logger,
                innercap_csv_path=innercap_csv_path,
                bom_file_path=bom_file_path,
                search_roots=[input_dir, working_dir, output_dir] + resolved_candidates,
            )

        # Apply SIwave setup only when solver backend is SIwave.
        pre_backend = resolve_solver_backend(layer_count, conf_data, logger)
        if pre_backend == "siwave":
            app.setup_simulation(None, sws_file, None)
            apply_dynamic_frequency_setup(app, layer_count, conf_data, logger)
        else:
            logger.log(
                "[PRE] Skip SIwave setup import for AEDT cutout backend.",
                level=LogLevel.INFO,
            )
        
        app.save_project_as(siw_output_path)
        
        # 파일 생성 검증 로직 추가
        if not siw_output_path.exists():
            raise FileNotFoundError(f"SIwave file was not created at {siw_output_path}")
            
        logger.log(f"[PRE] Pre-ready SIW generated: {siw_output_path}", level=LogLevel.INFO)
        return siw_output_path
    finally:
        if app:
            safe_close_edb_session(app, logger, "build_pre_stage_siw_snapshot")
            app.quit_application()


def create_siw_snapshot_from_edb(
    *,
    aedt_version: str,
    source_edb_path: Path,
    siw_output_path: Path,
    logger: Logger,
):
    """Create a SIW snapshot from an already-prepared EDB without applying SIwave simulation setup."""
    if not _is_edb_ready(source_edb_path):
        raise FileNotFoundError(f"Source EDB is not ready for SIW snapshot: {source_edb_path}")
    app = None
    try:
        app = SIwave(version=aedt_version, logger=logger)
        app.import_edb(str(source_edb_path))
        app.save_project_as(siw_output_path)
        if not siw_output_path.exists():
            raise FileNotFoundError(f"SIwave file was not created at {siw_output_path}")
        logger.log(f"[PRE] SIW snapshot generated from EDB: {siw_output_path}", level=LogLevel.INFO)
        return siw_output_path
    finally:
        if app:
            safe_close_edb_session(app, logger, "create_siw_snapshot_from_edb")
            app.quit_application()

def prepare_stackup_for_project(app, stackup_path: Path | None, work_dir: Path, logger: Logger):
    return pdn_setup_utils.prepare_stackup_for_project(app, stackup_path, work_dir, logger)

def sync_edb_changes_to_siw_project(source_app, target_app, sync_edb_path: Path, logger: Logger):
    """
    When EDB edits were made from a different app/session, export those edits and
    re-import into the SIwave project session before setup/save.
    """
    if not source_app or not target_app or source_app is target_app:
        return sync_edb_path
    if not getattr(source_app, "edb", None):
        raise RuntimeError("Source EDB app has no open EDB for synchronization.")

    if sync_edb_path.exists():
        shutil.rmtree(sync_edb_path, ignore_errors=True)

    source_app.edb.save_as(str(sync_edb_path))
    if not wait_for_edb_ready(sync_edb_path, timeout=300.0, check_interval=3.0):
        raise FileNotFoundError(f"Synchronized EDB is not ready: {sync_edb_path}")

    target_app.import_edb(str(sync_edb_path))
    target_app.set_cad_file(str(sync_edb_path))
    logger.log(f"[SYNC] Re-imported edited EDB into SIwave project session: {sync_edb_path}", level=LogLevel.DETAIL1)
    return sync_edb_path

def collect_analysis_nets(cases, gnd_net: str, exclude_tokens=None):
    nets = set()
    if gnd_net:
        nets.add(str(gnd_net))
    for case in cases or []:
        tgt = str(case.get("Net", "")).strip()
        if tgt:
            nets.add(tgt)
        chain = case.get("Full_Net_Chain", case.get("Source_net_chain", [])) or []
        for n in chain:
            nn = str(n).strip()
            if nn:
                nets.add(nn)
    # remove obvious dummy/invalid placeholders
    filtered = []
    excl = [str(t).strip().upper() for t in (exclude_tokens or []) if str(t).strip()]
    for n in nets:
        up = n.upper()
        if up in {"", "NULL", "(NULL)"}:
            continue
        if any(tok == up or tok in up for tok in excl):
            continue
        filtered.append(n)
    return sorted(filtered)

def classify_and_audit_analysis_nets(edb_app, analysis_nets, gnd_net, logger: Logger, exclude_tokens=None):
    if not edb_app or not getattr(edb_app, "edb", None):
        return {"classified_power": 0, "available": 0, "missing": len(analysis_nets), "plane_area": {}}

    nets_db = edb_app.edb.nets.nets if hasattr(edb_app.edb, "nets") else {}
    available = [n for n in analysis_nets if n in nets_db]
    missing = [n for n in analysis_nets if n not in nets_db]

    # Force requested analysis nets to power/ground classification.
    signal_demote = []
    excl = [str(t).strip().upper() for t in (exclude_tokens or []) if str(t).strip()]
    if excl:
        signal_demote = [
            n for n in nets_db.keys()
            if any(tok == str(n).upper() or tok in str(n).upper() for tok in excl)
        ]

    try:
        edb_app.edb.nets.classify_nets(power_nets=available, signal_nets=signal_demote)
    except Exception as e:
        logger.log(f"[NET][WARNING] classify_nets failed: {e}", level=LogLevel.WARNING)

    # Audit copper polygon area for each selected net.
    plane_area = {}
    for net_name in available:
        total_area = 0.0
        try:
            net_obj = nets_db[net_name]
            for prim in getattr(net_obj, "primitives", []):
                try:
                    p = getattr(prim, "_edb_object", None)
                    if p is None:
                        continue
                    ptype = p.GetPrimitiveType()
                    # Non-path primitives are treated as plane-like in pyedb eligible_power_nets logic.
                    if "Path" in str(ptype):
                        continue
                    total_area += float(p.GetPolygonData().Area())
                except Exception:
                    continue
        except Exception:
            pass
        plane_area[net_name] = total_area

    logger.log(
        f"[NET] Analysis nets: requested={len(analysis_nets)}, available={len(available)}, "
        f"missing={len(missing)}, signal_demote={len(signal_demote)}",
        level=LogLevel.INFO,
    )
    if missing:
        logger.log(f"[NET][WARNING] Missing analysis nets in EDB: {missing}", level=LogLevel.WARNING)
    logger.log(f"[NET] Plane-area audit (non-path area): {plane_area}", level=LogLevel.DETAIL1)
    if gnd_net and gnd_net in plane_area and plane_area[gnd_net] <= 0.0:
        logger.log(f"[NET][WARNING] GND plane area is zero for {gnd_net}.", level=LogLevel.WARNING)

    return {
        "classified_power": len(available),
        "available": len(available),
        "missing": len(missing),
        "plane_area": plane_area,
    }

def apply_selected_nets_to_siw_file(siw_path: Path, selected_nets, logger: Logger):
    if not siw_path or not siw_path.exists():
        return False
    nets = []
    seen = set()
    invalid_tokens = []
    valid_token = re.compile(r"^[A-Za-z0-9_\+\-\.\/:$]+$")
    for n in (selected_nets or []):
        name = str(n).strip()
        if not name:
            continue
        # SIW parser safety: reject net names that can break quoted token format.
        if any(ch in name for ch in ('"', '\n', '\r', '\t')):
            logger.log(f"[NET][WARNING] Skip invalid net token for SIW injection: {name!r}", level=LogLevel.WARNING)
            continue
        if not valid_token.match(name):
            invalid_tokens.append(name)
            continue
        k = name.upper()
        if k in seen:
            continue
        seen.add(k)
        nets.append(name)
    if invalid_tokens:
        logger.log(
            f"[NET][WARNING] Skip unsupported net token(s) for SIW injection: {invalid_tokens}",
            level=LogLevel.WARNING,
        )
    if not nets:
        return False

    try:
        text = siw_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        logger.log(f"[NET][WARNING] Failed to read SIW for net injection: {e}", level=LogLevel.WARNING)
        return False

    def _replace_block(src, begin_token, end_token, payload_lines):
        lines = src.splitlines()
        begin_idx = -1
        end_idx = -1
        for i, line in enumerate(lines):
            if line.strip() == begin_token:
                begin_idx = i
                break
        if begin_idx >= 0:
            for j in range(begin_idx + 1, len(lines)):
                if lines[j].strip() == end_token:
                    end_idx = j
                    break
        if begin_idx < 0 or end_idx < 0 or end_idx <= begin_idx:
            return src, False
        new_lines = lines[: begin_idx + 1] + payload_lines + lines[end_idx:]
        out = "\n".join(new_lines)
        if src.endswith("\n"):
            out += "\n"
        return out, True

    def _extract_block(src, begin_token, end_token):
        lines = src.splitlines()
        begin_idx = next((i for i, line in enumerate(lines) if line.strip() == begin_token), -1)
        if begin_idx < 0:
            return []
        end_idx = next((j for j in range(begin_idx + 1, len(lines)) if lines[j].strip() == end_token), -1)
        if end_idx < 0:
            return []
        return lines[begin_idx + 1 : end_idx]

    quoted_payload = [f"\"{n}\"" for n in nets]
    counted_payload = [str(len(nets))] + quoted_payload

    new_text, ok_sim = _replace_block(
        text,
        "B_USER_SELECTED_NETS_FOR_SIMULATION",
        "E_USER_SELECTED_NETS_FOR_SIMULATION",
        counted_payload,
    )
    new_text, ok_cpl = _replace_block(
        new_text,
        "B_USER_SELECTED_NETS_FOR_COUPLING",
        "E_USER_SELECTED_NETS_FOR_COUPLING",
        counted_payload,
    )
    new_text, ok_mod = _replace_block(
        new_text,
        "B_SELECTED_NETS_MOD_GEN",
        "E_SELECTED_NETS_MOD_GEN",
        counted_payload,
    )

    if new_text == text:
        logger.log("[NET][WARNING] SIW selected-net block injection made no changes.", level=LogLevel.WARNING)
        return False

    try:
        siw_path.write_text(new_text, encoding="utf-8")
        verify = siw_path.read_text(encoding="utf-8", errors="ignore")
        sim_block = _extract_block(verify, "B_USER_SELECTED_NETS_FOR_SIMULATION", "E_USER_SELECTED_NETS_FOR_SIMULATION")
        mod_block = _extract_block(verify, "B_SELECTED_NETS_MOD_GEN", "E_SELECTED_NETS_MOD_GEN")
        logger.log(
            f"[NET] Injected selected nets into SIW: count={len(nets)}, blocks(sim/coupling/mod)=({ok_sim}/{ok_cpl}/{ok_mod}), "
            f"verify_sizes(sim={len(sim_block)}, mod={len(mod_block)})",
            level=LogLevel.INFO,
        )
        # Minimal verification: at least one net token must remain in simulation block.
        return any(line.strip().startswith('"') for line in sim_block)
    except Exception as e:
        logger.log(f"[NET][WARNING] Failed to write SIW net injection: {e}", level=LogLevel.WARNING)
        return False


def verify_selected_nets_block_in_siw(siw_path: Path):
    if not siw_path or not siw_path.exists():
        return {"ok": False, "reason": "siw_missing"}
    text = siw_path.read_text(encoding="utf-8", errors="ignore")
    lines = text.splitlines()

    def _extract(begin_token, end_token):
        begin_idx = next((i for i, line in enumerate(lines) if line.strip() == begin_token), -1)
        if begin_idx < 0:
            return []
        end_idx = next((j for j in range(begin_idx + 1, len(lines)) if lines[j].strip() == end_token), -1)
        if end_idx < 0:
            return []
        return lines[begin_idx + 1 : end_idx]

    sim = _extract("B_USER_SELECTED_NETS_FOR_SIMULATION", "E_USER_SELECTED_NETS_FOR_SIMULATION")
    cpl = _extract("B_USER_SELECTED_NETS_FOR_COUPLING", "E_USER_SELECTED_NETS_FOR_COUPLING")
    mod = _extract("B_SELECTED_NETS_MOD_GEN", "E_SELECTED_NETS_MOD_GEN")
    sim_count = int(sim[0].strip()) if sim and sim[0].strip().isdigit() else None
    sim_tokens = [line.strip() for line in sim[1:] if line.strip().startswith('"') and line.strip().endswith('"')]
    return {
        "ok": bool(sim_tokens) and (sim_count is None or sim_count == len(sim_tokens)),
        "sim_size": len(sim),
        "cpl_size": len(cpl),
        "mod_size": len(mod),
        "sim_count": sim_count,
        "sim_tokens": len(sim_tokens),
    }


def relax_siw_plane_filters_for_pdn(siw_path: Path, conf_data: dict, logger: Logger):
    """
    Relax overly strict SIW plane filtering that can cancel SYZ solve with
    'no significant planes detected' on compact PDN regions.
    Configurable via:
      PDN.setup.syzPlaneFilter.ignore_small_planes (default: false)
      PDN.setup.syzPlaneFilter.min_plane_area_mm2 (default: 0.0)
      PDN.setup.syzPlaneFilter.min_plane_area_to_mesh_mm2 (default: 0.0)
      PDN.setup.syzPlaneFilter.min_dc_plane_area_to_mesh_mm2 (default: 0.0)
    """
    if not siw_path or not siw_path.exists():
        return False
    setup = (conf_data or {}).get("PDN", {}).get("setup", {})
    pf = setup.get("syzPlaneFilter", {}) if isinstance(setup, dict) else {}
    ignore_small_planes = 1 if bool(pf.get("ignore_small_planes", False)) else 0
    min_plane_area = float(pf.get("min_plane_area_mm2", 0.0))
    min_plane_mesh = float(pf.get("min_plane_area_to_mesh_mm2", 0.0))
    min_dc_plane_mesh = float(pf.get("min_dc_plane_area_to_mesh_mm2", 0.0))

    try:
        text = siw_path.read_text(encoding="utf-8", errors="ignore")
    except Exception as e:
        logger.log(f"[NET][WARNING] Failed to read SIW for plane-filter patch: {e}", level=LogLevel.WARNING)
        return False

    replacements = [
        (r"(?im)^(\s*IGNORE_PLANES_WITH_AREA_LESS_THAN_THRESOLD\s+).*$", rf"\g<1>{ignore_small_planes}"),
        (r"(?im)^(\s*MIN_PLANE_AREA\s+).*$", rf"\g<1>{min_plane_area:.12g}"),
        (r"(?im)^(\s*MIN_PLANE_AREA_TO_MESH\s+).*$", rf"\g<1>{min_plane_mesh:.12g}"),
        (r"(?im)^(\s*MIN_DC_PLANE_AREA_TO_MESH\s+).*$", rf"\g<1>{min_dc_plane_mesh:.12g}"),
    ]

    patched = text
    total_hits = 0
    for pattern, repl in replacements:
        patched, n = re.subn(pattern, repl, patched)
        total_hits += n

    if patched == text:
        logger.log("[NET][WARNING] Plane-filter patch made no SIW changes.", level=LogLevel.WARNING)
        return False

    try:
        siw_path.write_text(patched, encoding="utf-8")
        logger.log(
            "[NET] Relaxed SIW plane filters for PDN: "
            f"ignore_small_planes={ignore_small_planes}, min_plane_area={min_plane_area}, "
            f"min_plane_area_to_mesh={min_plane_mesh}, min_dc_plane_area_to_mesh={min_dc_plane_mesh}, "
            f"updated_tokens={total_hits}",
            level=LogLevel.INFO,
        )
        return True
    except Exception as e:
        logger.log(f"[NET][WARNING] Failed to write SIW plane-filter patch: {e}", level=LogLevel.WARNING)
        return False


def resolve_stackup_for_project(input_valchk, input_dir: Path, working_dir: Path, logger: Logger) -> Path | None:
    stackup_input = input_valchk._default_inputFiles.get('Stackup') if input_valchk else None
    if not stackup_input:
        return None
    stackup_path = Path(stackup_input)
    if not stackup_path.is_absolute():
        stackup_path = input_dir / stackup_path
    if not stackup_path.exists():
        logger.log(f"[WARNING] Stackup file not found: {stackup_path}", level=LogLevel.WARNING)
        return None
    prepared = prepare_stackup_for_project(None, stackup_path, working_dir, logger)
    prepared_path = Path(prepared) if prepared else stackup_path
    logger.log(f"[PRE] Effective stackup selected: {prepared_path}", level=LogLevel.DETAIL1)
    return prepared_path


def _pick_best_generated_file(directory: Path, extension: str, base_stem: str) -> Path | None:
    candidates = list(directory.glob(f"*{extension}"))
    if not candidates:
        return None
    exact = [p for p in candidates if p.stem == base_stem]
    if exact:
        return exact[0]
    stem_match = [p for p in candidates if p.stem.split('-')[0] == base_stem]
    if stem_match:
        return sorted(stem_match, key=lambda x: x.stat().st_mtime, reverse=True)[0]
    return sorted(candidates, key=lambda x: x.stat().st_mtime, reverse=True)[0]


def resolve_or_create_anf_cmp(dsgn_file: Path, zuken_bin_dir: Path, logger: Logger):
    dsgn_file = Path(dsgn_file)
    if not dsgn_file.exists() or not dsgn_file.is_file():
        raise PDNSessionException(ErrorCode.CONVERT_DSGN_TO_ANF_FAIL, f"Invalid DSGN path: {dsgn_file}")

    base_stem = dsgn_file.stem.split('-')[0]
    anf_file = _pick_best_generated_file(dsgn_file.parent, ".anf", base_stem)
    cmp_file = _pick_best_generated_file(dsgn_file.parent, ".cmp", base_stem)
    if anf_file and cmp_file:
        logger.log(f"[PRE] Reuse existing ANF/CMP: {anf_file.name}, {cmp_file.name}", level=LogLevel.DETAIL1)
        return anf_file, cmp_file

    dsgn2anf_exec = zuken_bin_dir / 'DFdsgn2anf.exe'
    if not dsgn2anf_exec.exists():
        raise FileNotFoundError(f"DFdsgn2anf.exe not found: {dsgn2anf_exec}")

    last_rc = None
    for attempt in range(1, MAX_RETRIES + 1):
        expected_anf = dsgn_file.with_suffix(".anf")
        # Primary: pass output ANF file path (more stable than directory output).
        cmd_variants = [
            [str(dsgn2anf_exec), '-r', str(dsgn_file), '-o', str(expected_anf)],
            [str(dsgn2anf_exec), '-r', str(dsgn_file), '-o', str(dsgn_file.parent)],
        ]
        result = None
        for cmd_idx, cmd in enumerate(cmd_variants, start=1):
            logger.log(
                f"[PRE][ANF] Attempt {attempt}/{MAX_RETRIES}, cmd {cmd_idx}/{len(cmd_variants)}: {' '.join(cmd)}",
                level=LogLevel.DETAIL1,
            )
            result = run_external_tool(cmd, "DFdsgn2anf.exe")
            if result.returncode == 0:
                break
        last_rc = result.returncode if result else -1
        anf_file = _pick_best_generated_file(dsgn_file.parent, ".anf", base_stem)
        cmp_file = _pick_best_generated_file(dsgn_file.parent, ".cmp", base_stem)
        if last_rc == 0 and anf_file and cmp_file:
            logger.log(f"[PRE] DSGN->ANF/CMP success (attempt {attempt}/{MAX_RETRIES}).", level=LogLevel.DETAIL1)
            return anf_file, cmp_file
        if attempt < MAX_RETRIES:
            logger.log(f"[WARNING] DSGN->ANF/CMP failed (attempt {attempt}/{MAX_RETRIES}), retry after {RETRY_DELAY}s.", level=LogLevel.WARNING)
            time.sleep(RETRY_DELAY)

    raise PDNSessionException(ErrorCode.CONVERT_DSGN_TO_ANF_FAIL, last_rc)


def build_edb_via_create_project(
    *,
    aedt_version: str,
    anf_file: Path,
    cmp_file: Path,
    stackup_file: Path,
    output_dir: Path,
    base_name: str,
    logger: Logger,
):
    bootstrap_siw = output_dir / f"{base_name}_stackup_bootstrap.siw"
    bootstrap_edb = output_dir / f"{base_name}_stackup_bootstrap.aedb"
    app = None
    try:
        if bootstrap_edb.exists():
            shutil.rmtree(bootstrap_edb, ignore_errors=True)
        app = SIwave(version=aedt_version, logger=logger)
        app.create_project(anf_file, cmp_file, stackup_file, bootstrap_siw, bootstrap_edb)
    finally:
        if app:
            safe_close_edb_session(app, logger, "build_edb_via_create_project")
            app.quit_application()
    if not wait_for_edb_ready(bootstrap_edb, timeout=300.0, check_interval=3.0):
        raise FileNotFoundError(f"Bootstrap EDB is not ready after create_project: {bootstrap_edb}")
    logger.log(f"[PRE] Bootstrap SIW/EDB created via create_project: {bootstrap_siw}, {bootstrap_edb}", level=LogLevel.INFO)
    return bootstrap_siw, bootstrap_edb


def log_signal_layer_thicknesses(app, logger: Logger, tag: str = "[STACKUP]"):
    try:
        signal_layers = app.edb.stackup.signal_layers
    except Exception as exc:
        logger.log(f"{tag} Failed to read signal layers: {exc}", level=LogLevel.WARNING)
        return
    for layer_name, layer_obj in signal_layers.items():
        thickness = None
        try:
            thickness = getattr(layer_obj, "thickness", None)
        except Exception:
            thickness = None
        logger.log(f"{tag} {layer_name} thickness={thickness}", level=LogLevel.DETAIL1)


def get_edb_signal_layer_thickness_map(edb_path: Path, aedt_version: str, logger: Logger):
    app = None
    result = {}
    try:
        if not edb_path or not Path(edb_path).exists():
            return result
        app = SIwave(version=aedt_version, logger=logger)
        app.set_cad_file(str(edb_path))
        signal_layers = app.edb.stackup.signal_layers
        for layer_name, layer_obj in signal_layers.items():
            try:
                result[str(layer_name)] = float(getattr(layer_obj, "thickness", 0.0) or 0.0)
            except Exception:
                result[str(layer_name)] = 0.0
    except Exception as exc:
        logger.log(f"[STACKUP][CHECK][WARNING] Failed to inspect EDB stackup ({edb_path}): {exc}", level=LogLevel.WARNING)
    finally:
        if app:
            safe_close_edb_session(app, logger, "stackup-check")
            app.quit_application()
    return result


def is_edb_stackup_valid_for_solve(edb_path: Path, aedt_version: str, logger: Logger, min_thickness_mm: float = 1e-4):
    thickness_map = get_edb_signal_layer_thickness_map(edb_path, aedt_version, logger)
    if not thickness_map:
        return False, thickness_map
    for layer_name, thickness in thickness_map.items():
        if thickness < min_thickness_mm:
            logger.log(
                f"[STACKUP][CHECK][WARNING] Invalid thickness detected: {layer_name}={thickness} mm (< {min_thickness_mm})",
                level=LogLevel.WARNING,
            )
            return False, thickness_map
    return True, thickness_map

def sanitize_name_token(value):
    return re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "").strip()).strip("_")


def _spec_net_candidates(case_dict):
    cands = []
    for key in ("Net", "Net_name", "Net Name", "PCB_Net"):
        val = str(case_dict.get(key, "")).strip()
        if val:
            cands.append(val)
    return cands


def _normalize_net_token(net_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(net_name or "").upper())


def _voltage_tokens_from_net(net_name: str):
    """Build strict tokens (e.g. 1V8, 1P8, 18V) for relaxed voltage-family matching."""
    tokens = set()
    try:
        v = extract_voltage(str(net_name or ""))
    except Exception:
        v = None
    if v is None:
        return tokens
    whole = int(v)
    dec = int(round((v - whole) * 10))
    tokens.add(f"{whole}V{dec}")
    tokens.add(f"{whole}P{dec}")
    tokens.add(f"{whole}{dec}V")
    return {t for t in tokens if t}


def _is_generic_voltage_alias_name(norm_name: str) -> bool:
    """
    Guard relaxed voltage matching to generic power alias names.
    This avoids mapping to unrelated rails like D1V8 when spec expects +1.8V.
    """
    n = str(norm_name or "").upper()
    keywords = ("VCC", "VDD", "POWER", "PWR", "EMMC", "VTERM")
    return any(k in n for k in keywords)


def _net_matches_spec(spec_net: str, cand_net: str, net_alias_map) -> bool:
    """Match spec net to candidate EDB net with exact/alias + relaxed voltage naming."""
    s = str(spec_net or "").strip()
    c = str(cand_net or "").strip()
    if not s or not c:
        return False

    # 1) exact / alias map
    if c == s:
        return True
    aliases = set(net_alias_map.get(s, set()))
    if c in aliases or s in set(net_alias_map.get(c, set())):
        return True

    # 2) normalized contains (+1.8V vs +1.8V_VDD)
    sn = _normalize_net_token(s)
    cn = _normalize_net_token(c)
    if sn and (sn in cn or cn in sn):
        return True

    # 2.5) voltage-equivalent matching (+1.8V ~= +1V8 ~= EMMC1.8V ~= EMMC1V8)
    # Guard against common false-positive rail family (e.g., +D1V8 when spec is +1.8V).
    try:
        sv = extract_voltage(s)
        cv = extract_voltage(c)
    except Exception:
        sv = cv = None
    if sv is not None and cv is not None and abs(float(sv) - float(cv)) < 1e-6:
        # reject obvious signal-like candidates
        if _is_signal_like_net(c):
            return False
        # reject digital-domain specific rails unless spec explicitly requests that domain
        spec_has_drail = bool(re.search(r"(?:^|[+_\-\s])D\d+V\d+", s.upper()))
        cand_has_drail = bool(re.search(r"(?:^|[+_\-\s])D\d+V\d+", c.upper()))
        if cand_has_drail and not spec_has_drail:
            return False
        return True

    # 3) relaxed voltage-family match, but only for generic alias-like net names.
    #    (prevents over-match such as +1.8V -> +D1V8)
    if not _is_generic_voltage_alias_name(cn):
        return False
    for tok in _voltage_tokens_from_net(s):
        if tok in cn:
            return True
    return False

def _net_domain_tokens(net_name: str):
    """
    Extract domain-like tokens used to avoid false matches between different rails
    that share same voltage (e.g., +D1V0 vs +CPU_1V0).
    """
    raw = str(net_name or "").upper()
    parts = [p for p in re.split(r"[^A-Z0-9]+", raw) if p]
    out = set()
    for p in parts:
        if re.fullmatch(r"\d+V\d+", p) or re.fullmatch(r"\d+P\d+", p):
            continue
        if p in {"VCC", "VDD", "POWER", "PWR", "NET", "SW", "SIGN", "GND"}:
            continue
        out.add(p)
    return out

def _net_matches_spec_pin_strict(spec_net: str, cand_net: str, net_alias_map) -> bool:
    """
    Strict matching for pin-resolution stage:
    - Keep exact/alias/normalized containment.
    - For voltage-equivalent fallback, require domain-token compatibility.
    """
    s = str(spec_net or "").strip()
    c = str(cand_net or "").strip()
    if not s or not c:
        return False

    # exact/alias/contains path (already strict enough)
    if c == s:
        return True
    aliases = set(net_alias_map.get(s, set()))
    if c in aliases or s in set(net_alias_map.get(c, set())):
        return True
    sn = _normalize_net_token(s)
    cn = _normalize_net_token(c)
    if sn and (sn in cn or cn in sn):
        return True

    # guarded voltage-equivalent fallback
    try:
        sv = extract_voltage(s)
        cv = extract_voltage(c)
    except Exception:
        sv = cv = None
    if sv is None or cv is None or abs(float(sv) - float(cv)) >= 1e-6:
        return False
    if _is_signal_like_net(c):
        return False

    s_dom = _net_domain_tokens(s)
    c_dom = _net_domain_tokens(c)
    if s_dom and c_dom and not s_dom.intersection(c_dom):
        return False
    return True


def _normalize_pin_token(pin_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(pin_name or "")).upper()


def _extract_pin_display_names(pin_key: str, pin_inst) -> list[str]:
    """
    Collect possible UI/display pin names from both dict key and pin object properties.
    Some EDB instances expose UI pin as pin.name while dict key can be internal token.
    """
    names = [str(pin_key or "").strip()]
    try:
        for attr in ("name", "pin_name", "component_pin", "display_name"):
            v = getattr(pin_inst, attr, None)
            if v is None:
                continue
            s = str(v).strip()
            if s:
                names.append(s)
    except Exception:
        pass
    uniq = []
    seen = set()
    for n in names:
        if not n:
            continue
        u = n.upper()
        if u in seen:
            continue
        seen.add(u)
        uniq.append(n)
    return uniq


def _token_overlap_score(a: set[str], b: set[str]) -> int:
    if not a or not b:
        return 0
    return len(a.intersection(b))


def _iter_dotnet_pins_from_component(raw_comp):
    """
    Direct .NET EDB pin iterator (bypass PyAEDT dict cache).
    """
    if raw_comp is None:
        return []
    pin_objs = []
    # Common .NET entrypoints
    for attr in ("Pins", "pins"):
        try:
            obj = getattr(raw_comp, attr, None)
            if obj is None:
                continue
            pin_objs.extend(list(obj))
        except Exception:
            pass
    for mname in ("GetPins", "GetPinsList", "GetComponentPins"):
        try:
            fn = getattr(raw_comp, mname, None)
            if callable(fn):
                obj = fn()
                if obj is not None:
                    pin_objs.extend(list(obj))
        except Exception:
            pass
    # Deduplicate by object identity
    uniq = []
    seen = set()
    for p in pin_objs:
        try:
            k = id(p)
        except Exception:
            k = None
        if k in seen:
            continue
        seen.add(k)
        uniq.append(p)
    return uniq


def _collect_dotnet_pin_tokens(pin_obj, components_api=None):
    tokens = set()
    getters = (
        lambda x: getattr(x, "name", ""),
        lambda x: getattr(x, "pin_name", ""),
        lambda x: getattr(x, "component_pin", ""),
        lambda x: getattr(x, "aedt_name", ""),
        lambda x: x.GetName() if hasattr(x, "GetName") else "",
        lambda x: x.GetPinName() if hasattr(x, "GetPinName") else "",
    )
    for g in getters:
        try:
            v = g(pin_obj)
            s = str(v or "").strip()
            if s:
                tokens.add(_normalize_pin_token(s))
        except Exception:
            pass
    if components_api is not None:
        try:
            ap = components_api.get_aedt_pin_name(pin_obj)
            if ap:
                tokens.add(_normalize_pin_token(ap))
        except Exception:
            pass
    return {t for t in tokens if t}


def _dotnet_pin_net_name(pin_obj):
    for getter in (
        lambda x: getattr(x, "net_name", ""),
        lambda x: getattr(x, "net", None),
    ):
        try:
            v = getter(pin_obj)
            if isinstance(v, str):
                s = str(v).strip()
                if s:
                    return s
            if v is not None:
                n = str(getattr(v, "name", "") or "").strip()
                if n:
                    return n
        except Exception:
            pass
    try:
        net = pin_obj.GetNet() if hasattr(pin_obj, "GetNet") else None
        if net is not None:
            n = str(net.GetName() if hasattr(net, "GetName") else "").strip()
            if n:
                return n
    except Exception:
        pass
    return ""


def _dotnet_pin_position(pin_obj):
    for getter in (
        lambda x: getattr(x, "position", None),
        lambda x: x.GetPosition() if hasattr(x, "GetPosition") else None,
    ):
        try:
            p = getter(pin_obj)
            if p is not None and len(p) >= 2:
                return float(p[0]), float(p[1])
        except Exception:
            pass
    return None


def _build_pin_rows_multisource(comp_inst, components_api=None, cmp_pin_record=None, cmp_component_record=None):
    """
    Build pin candidate rows using multiple API layers:
    1) comp_inst.pins dict keys
    2) pin object display-name properties
    3) components_api.get_pin_from_component(...)
    4) low-level LayoutObjs (core pin + net)
    5) backend-agnostic attribute probing (grpc/dotnet)
    """
    has_coord = bool(cmp_pin_record and cmp_pin_record.get("x") is not None and cmp_pin_record.get("y") is not None)
    if has_coord:
        tx, ty = float(cmp_pin_record["x"]), float(cmp_pin_record["y"])
        coord_ctx = _build_coord_context(comp_inst, cmp_component_record)
    else:
        tx = ty = None
        coord_ctx = None

    rows = []
    for p_key, p_inst in comp_inst.pins.items():
        disp_names = _extract_pin_display_names(p_key, p_inst)
        norm_tokens = {_normalize_pin_token(x) for x in disp_names if str(x).strip()}
        if has_coord:
            try:
                px, py = float(p_inst.position[0]), float(p_inst.position[1])
                d2 = _coord_distance2(tx, ty, px, py, coord_ctx)
            except Exception:
                d2 = 1e12
        else:
            d2 = 1e12
        row = {
            "key": p_key,
            "inst": p_inst,
            "tokens": norm_tokens,
            "net_primary": str(getattr(p_inst, "net_name", "") or ""),
            "net_candidates": {str(getattr(p_inst, "net_name", "") or "")},
            "d2": d2,
            "px": None,
            "py": None,
            "sources": {"dict"},
        }
        try:
            row["px"] = float(p_inst.position[0])
            row["py"] = float(p_inst.position[1])
        except Exception:
            pass
        rows.append(row)

    if not rows:
        return rows

    def _merge_external_pin(ext_tokens: set[str], ext_net: str, source_tag: str, ext_pos=None):
        if not ext_tokens:
            return
        best_idx = -1
        best_score = 0
        for idx, row in enumerate(rows):
            score = _token_overlap_score(ext_tokens, row["tokens"])
            if score > best_score:
                best_idx = idx
                best_score = score
        if best_idx >= 0 and best_score > 0:
            rows[best_idx]["tokens"].update(ext_tokens)
            if ext_net:
                rows[best_idx]["net_candidates"].add(ext_net)
            rows[best_idx]["sources"].add(source_tag)
            return

        # Token overlap이 없는 경우, 동일 net 기준으로 병합 시도 (UI pin과 내부 key 불일치 보정).
        ext_net_norm = _normalize_net_token(ext_net)
        if ext_net_norm:
            same_net_idxs = []
            for idx, row in enumerate(rows):
                row_net = str(row.get("net_primary", "") or "").strip()
                if _normalize_net_token(row_net) == ext_net_norm:
                    same_net_idxs.append(idx)
            if len(same_net_idxs) == 1:
                t_idx = same_net_idxs[0]
                rows[t_idx]["tokens"].update(ext_tokens)
                rows[t_idx]["net_candidates"].add(ext_net)
                rows[t_idx]["sources"].add(source_tag)
                return
            if ext_pos is not None and same_net_idxs:
                ex = ey = None
                try:
                    ex, ey = float(ext_pos[0]), float(ext_pos[1])
                except Exception:
                    ex = ey = None
                if ex is not None and ey is not None:
                    best = (1e30, -1)
                    for idx in same_net_idxs:
                        rx, ry = rows[idx].get("px"), rows[idx].get("py")
                        if rx is None or ry is None:
                            continue
                        d2 = (float(rx) - ex) ** 2 + (float(ry) - ey) ** 2
                        if d2 < best[0]:
                            best = (d2, idx)
                    if best[1] >= 0:
                        t_idx = best[1]
                        rows[t_idx]["tokens"].update(ext_tokens)
                        rows[t_idx]["net_candidates"].add(ext_net)
                        rows[t_idx]["sources"].add(source_tag)
                        return

    # 2/4) components helper API: full pin list + UI/AEDT names.
    if components_api is not None:
        try:
            helper_all = components_api.get_pin_from_component(comp_inst.name) or []
            for hp in helper_all:
                ht = set()
                try:
                    ht.add(_normalize_pin_token(getattr(hp, "component_pin", "")))
                except Exception:
                    pass
                try:
                    ht.add(_normalize_pin_token(getattr(hp, "aedt_name", "")))
                except Exception:
                    pass
                try:
                    ht.add(_normalize_pin_token(getattr(hp, "name", "")))
                except Exception:
                    pass
                try:
                    ht.add(_normalize_pin_token(components_api.get_aedt_pin_name(hp)))
                except Exception:
                    pass
                ht = {t for t in ht if t}
                hnet = str(getattr(hp, "net_name", "") or "")
                hpos = None
                try:
                    p = getattr(hp, "position", None)
                    if p is not None and len(p) >= 2:
                        hpos = (float(p[0]), float(p[1]))
                except Exception:
                    hpos = None
                _merge_external_pin(ht, hnet, "components_api", ext_pos=hpos)
        except Exception:
            pass

    # 3) Direct .NET component pin collection (deep access, bypass wrapper cache).
    raw_comp = getattr(comp_inst, "_edb_object", None) or getattr(comp_inst, "edbcomponent", None)
    if raw_comp is not None:
        try:
            for dp in _iter_dotnet_pins_from_component(raw_comp):
                dt = _collect_dotnet_pin_tokens(dp, components_api=components_api)
                dnet = _dotnet_pin_net_name(dp)
                dpos = _dotnet_pin_position(dp)
                _merge_external_pin(dt, dnet, "dotnet_pins", ext_pos=dpos)
        except Exception:
            pass

    # 4) low-level layout object sweep (core names + net).
    if raw_comp is not None:
        try:
            layout_objs = list(getattr(raw_comp, "LayoutObjs", []) or [])
            for robj in layout_objs:
                try:
                    if int(robj.GetObjType()) != 1 or (hasattr(robj, "IsLayoutPin") and not robj.IsLayoutPin()):
                        continue
                except Exception:
                    continue
                lt = set()
                lnet = ""
                try:
                    lt.add(_normalize_pin_token(robj.GetName()))
                except Exception:
                    pass
                try:
                    lnet = str(robj.GetNet().GetName() or "")
                except Exception:
                    lnet = ""
                if components_api is not None:
                    try:
                        lt.add(_normalize_pin_token(components_api.get_aedt_pin_name(robj)))
                    except Exception:
                        pass
                lt = {t for t in lt if t}
                lpos = None
                try:
                    lp = robj.GetPosition()
                    if lp and len(lp) >= 2:
                        lpos = (float(lp[0]), float(lp[1]))
                except Exception:
                    lpos = None
                _merge_external_pin(lt, lnet, "layoutobj", ext_pos=lpos)
        except Exception:
            pass

    return rows


def _load_pin_overrides(project_dir: Path):
    """
    Optional manual override table.
    CSV headers: Designator, Spec_Pin, EDB_Pin
    """
    candidates = [
        project_dir / "pin_override.csv",
        project_dir / "outputs" / "pin_override.csv",
    ]
    for p in candidates:
        if not p.exists():
            continue
        rows = {}
        try:
            with open(p, "r", encoding="utf-8-sig", newline="") as f:
                reader = csv.DictReader(f)
                for r in reader:
                    d = str(r.get("Designator", "")).strip().upper()
                    s = str(r.get("Spec_Pin", "")).strip().upper()
                    e = str(r.get("EDB_Pin", "")).strip()
                    if d and s and e:
                        rows[(d, s)] = e
            return rows, p
        except Exception:
            return {}, p
    return {}, None


def _find_component_pin_by_name_or_display(comp_inst, pin_name: str, excluded=None):
    excluded = {str(p) for p in (excluded or set()) if str(p)}
    pin_name = str(pin_name or "").strip()
    if not pin_name:
        return None, ""
    if pin_name in comp_inst.pins and pin_name not in excluded:
        return comp_inst.pins[pin_name], pin_name
    target = _normalize_pin_token(pin_name)
    for p_key, p_inst in comp_inst.pins.items():
        if p_key in excluded:
            continue
        disp_names = _extract_pin_display_names(p_key, p_inst)
        if any(_normalize_pin_token(nm) == target for nm in disp_names):
            return p_inst, p_key
    return None, ""


def _is_power_like_net(net_name: str) -> bool:
    n = _normalize_net_token(net_name)
    if not n:
        return False
    if extract_voltage(str(net_name or "")) is not None:
        return True
    power_keywords = ("VCC", "VDD", "POWER", "PWR", "VTERM", "VBAT", "VDDQ")
    signal_keywords = (
        "ADDR", "DATA", "CLK", "SIGN", "GPIO", "I2C", "SPI", "UART", "USB", "PCIE",
        "CMD", "RST", "DET", "PWM", "LOCK",
    )
    if any(k in n for k in signal_keywords):
        return False
    return any(k in n for k in power_keywords)


def _is_signal_like_net(net_name: str) -> bool:
    """
    Signal blacklist filter to prevent power-pin misbinding on coordinate fallback.
    """
    n = _normalize_net_token(net_name)
    if not n:
        return False
    signal_blacklist = (
        "ADDR", "DATA", "CLK", "TX", "RX", "MISO", "MOSI",
        "SCL", "SDA", "GPIO", "SIGN", "EB_", "CMD", "RST", "DET", "PWM", "LOCK",
    )
    # Since _normalize_net_token removes non-alnum, keep EB_ by checking both normalized and raw upper.
    raw = str(net_name or "").upper()
    if any(k in n for k in signal_blacklist if "_" not in k):
        return True
    if "EB_" in raw:
        return True
    return False


def _is_forbidden_trace_net(net_name: str) -> bool:
    n = _normalize_net_token(net_name)
    raw = str(net_name or "").upper()
    if not n:
        return True
    forbidden_tokens = ("DUMMY", "ZUKEN", "NC", "NOERC", "FLOAT", "UNUSED")
    if any(t in n for t in forbidden_tokens):
        return True
    if any(t in raw for t in ("ZUKEN_DUMMY", "N/C", "NO_CONNECT")):
        return True
    return False


def _resolve_board_net_by_spec(spec_nets, all_board_nets, net_alias_map):
    """
    Resolve target PCB net when pin object resolution fails.
    """
    specs = [str(n).strip() for n in (spec_nets or []) if str(n).strip()]
    board_nets = [str(n).strip() for n in (all_board_nets or []) if str(n).strip()]
    if not specs or not board_nets:
        return ""

    ranked = []
    for bnet in board_nets:
        best_score = None
        for snet in specs:
            if bnet == snet:
                score = 0
            elif bnet in set(net_alias_map.get(snet, set())) or snet in set(net_alias_map.get(bnet, set())):
                score = 1
            elif _net_matches_spec(snet, bnet, net_alias_map):
                score = 2
            else:
                continue

            if _is_signal_like_net(bnet):
                score += 50
            spec_has_drail = bool(re.search(r"(?:^|[+_\-\s])D\d+V\d+", snet.upper()))
            cand_has_drail = bool(re.search(r"(?:^|[+_\-\s])D\d+V\d+", bnet.upper()))
            if cand_has_drail and not spec_has_drail:
                score += 25

            if best_score is None or score < best_score:
                best_score = score

        if best_score is not None:
            ranked.append((best_score, bnet))

    if not ranked:
        return ""
    ranked.sort(key=lambda x: (x[0], str(x[1])))
    return ranked[0][1]


def _build_coord_context(comp_inst, cmp_component_record: dict | None):
    ctx = {"valid": False}
    if not cmp_component_record:
        return ctx
    cmp_points = []
    for rec in cmp_component_record.values():
        try:
            x = float(rec.get("x"))
            y = float(rec.get("y"))
            cmp_points.append((x, y))
        except Exception:
            continue
    edb_points = []
    for p_inst in comp_inst.pins.values():
        try:
            px, py = float(p_inst.position[0]), float(p_inst.position[1])
            edb_points.append((px, py))
        except Exception:
            continue
    if len(cmp_points) < 2 or len(edb_points) < 2:
        return ctx

    cmp_x = [p[0] for p in cmp_points]
    cmp_y = [p[1] for p in cmp_points]
    edb_x = [p[0] for p in edb_points]
    edb_y = [p[1] for p in edb_points]

    cmp_dx = max(cmp_x) - min(cmp_x)
    cmp_dy = max(cmp_y) - min(cmp_y)
    edb_dx = max(edb_x) - min(edb_x)
    edb_dy = max(edb_y) - min(edb_y)
    if cmp_dx <= 0 or cmp_dy <= 0 or edb_dx <= 0 or edb_dy <= 0:
        return ctx

    return {
        "valid": True,
        "cmp_min_x": min(cmp_x),
        "cmp_min_y": min(cmp_y),
        "cmp_dx": cmp_dx,
        "cmp_dy": cmp_dy,
        "edb_min_x": min(edb_x),
        "edb_min_y": min(edb_y),
        "edb_dx": edb_dx,
        "edb_dy": edb_dy,
    }


def _coord_distance2(tx, ty, px, py, coord_ctx):
    if coord_ctx and coord_ctx.get("valid"):
        ntx = (tx - coord_ctx["cmp_min_x"]) / coord_ctx["cmp_dx"]
        nty = (ty - coord_ctx["cmp_min_y"]) / coord_ctx["cmp_dy"]
        npx = (px - coord_ctx["edb_min_x"]) / coord_ctx["edb_dx"]
        npy = (py - coord_ctx["edb_min_y"]) / coord_ctx["edb_dy"]
        return (npx - ntx) ** 2 + (npy - nty) ** 2
    return (px - tx) ** 2 + (py - ty) ** 2


def discover_cmp_file(input_dir: Path, base_stem: str | None = None) -> Path | None:
    cmp_files = list(input_dir.glob("*.cmp"))
    if not cmp_files:
        return None
    if base_stem:
        exact = [p for p in cmp_files if p.stem == base_stem]
        if exact:
            return exact[0]
        stem_match = [p for p in cmp_files if p.stem.split('-')[0] == base_stem]
        if stem_match:
            return sorted(stem_match, key=lambda x: x.stat().st_mtime, reverse=True)[0]
    return sorted(cmp_files, key=lambda x: x.stat().st_mtime, reverse=True)[0]


def discover_ndf_file(input_dir: Path, base_stem: str | None = None) -> Path | None:
    ndf_files = list(input_dir.glob("*.ndf"))
    if not ndf_files:
        return None
    if base_stem:
        exact = [p for p in ndf_files if p.stem == base_stem]
        if exact:
            return exact[0]
        stem_match = [p for p in ndf_files if p.stem.split('-')[0] == base_stem]
        if stem_match:
            return sorted(stem_match, key=lambda x: x.stat().st_mtime, reverse=True)[0]
    return sorted(ndf_files, key=lambda x: x.stat().st_mtime, reverse=True)[0]

def discover_siw_for_ui_crosswalk(output_dir: Path, input_dir: Path, base_stem: str | None = None) -> Path | None:
    """
    Prefer bootstrap/ready SIW snapshots as UI-pin source for Spec<->EDB mapping.
    """
    candidates = []
    if output_dir and output_dir.exists():
        candidates.extend(sorted(output_dir.glob("*_stackup_bootstrap.siw"), key=lambda p: p.stat().st_mtime, reverse=True))
        candidates.extend(sorted(output_dir.glob("*_ready_for_solve.siw"), key=lambda p: p.stat().st_mtime, reverse=True))
        candidates.extend(sorted(output_dir.glob("*.siw"), key=lambda p: p.stat().st_mtime, reverse=True))
    if input_dir and input_dir.exists():
        candidates.extend(sorted(input_dir.glob("*.siw"), key=lambda p: p.stat().st_mtime, reverse=True))

    seen = set()
    ordered = []
    for p in candidates:
        if not p or not p.exists():
            continue
        rp = str(p.resolve()).upper()
        if rp in seen:
            continue
        seen.add(rp)
        ordered.append(p)
    if not ordered:
        return None
    if base_stem:
        for p in ordered:
            if str(p.stem).split("-")[0].startswith(base_stem):
                return p
    return ordered[0]

def parse_siw_pin_records(siw_path: Path, target_designators=None):
    """
    Parse SIW pin rows:
      X ... <x> <y> <z> <pkg> <Designator> <PinName> ...
    Coordinates are in mm in SIW text and converted to meters.
    """
    records = defaultdict(dict)
    if not siw_path or not siw_path.exists():
        return records
    target_set = {str(x).upper() for x in (target_designators or [])}
    line_pat = re.compile(
        r"^\s*X\s+\d+\s+\d+\s+\d+\s+\d+\s+"
        r"(?P<x>[+\-]?\d+(?:\.\d+)?(?:e[+\-]?\d+)?)\s+"
        r"(?P<y>[+\-]?\d+(?:\.\d+)?(?:e[+\-]?\d+)?)\s+"
        r"(?P<z>[+\-]?\d+(?:\.\d+)?(?:e[+\-]?\d+)?)\s+"
        r"(?P<pkg>\S+)\s+(?P<designator>\S+)\s+(?P<pin>\S+)\b",
        re.IGNORECASE,
    )
    try:
        with open(siw_path, "r", encoding="utf-8", errors="ignore") as f:
            for raw in f:
                m = line_pat.search(raw)
                if not m:
                    continue
                designator = str(m.group("designator") or "").strip()
                pin_name = str(m.group("pin") or "").strip()
                if not designator or not pin_name:
                    continue
                if target_set and designator.upper() not in target_set:
                    continue
                try:
                    x_mm = float(m.group("x"))
                    y_mm = float(m.group("y"))
                except Exception:
                    continue
                records[designator][pin_name] = {
                    "pin": pin_name,
                    "x": x_mm * 1e-3,
                    "y": y_mm * 1e-3,
                    "source": "siw",
                }
    except Exception:
        pass
    return records


def parse_ndf_pin_records(ndf_path: Path, target_designators=None):
    records = defaultdict(dict)
    if not ndf_path or not ndf_path.exists():
        return records
    target_set = {str(x).upper() for x in (target_designators or [])}
    line_pat = re.compile(
        r"^\s*(?P<net>[^:]+):\s*[^:]*:\s*[^:]*:\s*[^:]*:\s*(?P<designator>[^:]+):\s*(?P<pin>[^:]+):"
    )
    try:
        with open(ndf_path, "r", encoding="utf-8-sig", errors="ignore") as f:
            for raw in f:
                m = line_pat.search(raw)
                if not m:
                    continue
                designator = str(m.group("designator") or "").strip()
                pin_name = str(m.group("pin") or "").strip()
                net_name = str(m.group("net") or "").strip()
                if not designator or not pin_name:
                    continue
                if target_set and designator.upper() not in target_set:
                    continue
                if pin_name not in records[designator]:
                    records[designator][pin_name] = {
                        "pin": pin_name,
                        "net": net_name,
                    }
    except Exception:
        pass
    return records


def build_component_pin_crosswalk(
    comp_inst,
    cmp_component_record: dict | None,
    ndf_component_record: dict | None,
    net_alias_map,
):
    """
    Build stable UI_Pin -> EDB_Pin crosswalk with confidence.
    Priority:
    1) normalized pin-name exact
    2) net-compatible + nearest normalized coordinate
    3) nearest normalized coordinate fallback
    """
    cmp_component_record = cmp_component_record or {}
    ndf_component_record = ndf_component_record or {}
    cross = {}
    used_edb = set()

    coord_ctx = _build_coord_context(comp_inst, cmp_component_record)
    edb_items = []
    for p_name, p_inst in comp_inst.pins.items():
        px = py = None
        try:
            px, py = float(p_inst.position[0]), float(p_inst.position[1])
        except Exception:
            pass
        edb_items.append((p_name, p_inst, px, py, str(p_inst.net_name or "")))
    if not edb_items:
        return cross

    # NOTE:
    # CMP can miss some UI pins (e.g., V31) while NDF still has authoritative pin/net rows.
    # Use union(CMP, NDF) so crosswalk coverage does not silently drop NDF-only pins.
    ui_pin_names = sorted(set(cmp_component_record.keys()) | set(ndf_component_record.keys()))
    for ui_pin in ui_pin_names:
        ui_info = cmp_component_record.get(ui_pin, {})
        ui_net = str((ndf_component_record.get(ui_pin, {}) or {}).get("net", "")).strip()
        norm_ui = _normalize_pin_token(ui_pin)

        # 1) exact/normalized pin token match first
        exact = None
        for p_name, p_inst, px, py, p_net in edb_items:
            if p_name in used_edb:
                continue
            disp_names = _extract_pin_display_names(p_name, p_inst)
            if any(_normalize_pin_token(nm) == norm_ui for nm in disp_names):
                exact = (p_name, p_net)
                break
        if exact:
            p_name, p_net = exact
            net_ok = bool(ui_net) and _net_matches_spec(ui_net, p_net, net_alias_map)
            cross[ui_pin] = {
                "ui_pin": ui_pin,
                "edb_pin": p_name,
                "ui_net": ui_net,
                "edb_net": p_net,
                "net_match": net_ok,
                "method": "pin_exact",
                "confidence": 0.99 if (not ui_net or net_ok) else 0.80,
            }
            used_edb.add(p_name)
            continue

        tx = ui_info.get("x", None)
        ty = ui_info.get("y", None)
        candidates = []
        for p_name, p_inst, px, py, p_net in edb_items:
            if p_name in used_edb:
                continue
            if tx is not None and ty is not None and px is not None and py is not None:
                d2 = _coord_distance2(float(tx), float(ty), px, py, coord_ctx)
            else:
                d2 = 1e9
            net_ok = bool(ui_net) and _net_matches_spec(ui_net, p_net, net_alias_map)
            penalty = 0.0 if (not ui_net or net_ok) else 10.0
            score = d2 + penalty
            candidates.append((score, d2, net_ok, p_name, p_net))

        if not candidates:
            continue
        candidates.sort(key=lambda x: (x[0], x[1], x[3]))
        score, d2, net_ok, chosen_pin, chosen_net = candidates[0]
        conf = 0.55
        method = "coord_only"
        if ui_net and net_ok:
            method = "coord_net"
            conf = 0.92 if d2 < 0.0025 else 0.80
        elif d2 < 0.0025:
            conf = 0.70
        cross[ui_pin] = {
            "ui_pin": ui_pin,
            "edb_pin": chosen_pin,
            "ui_net": ui_net,
            "edb_net": chosen_net,
            "net_match": net_ok,
            "method": method,
            "confidence": conf,
        }
        used_edb.add(chosen_pin)
    return cross


def export_pin_crosswalk_reports(output_dir: Path, crosswalk_by_comp: dict):
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for comp_name, cmap in (crosswalk_by_comp or {}).items():
        for ui_pin, item in (cmap or {}).items():
            rows.append({
                "Designator": comp_name,
                "UI_Pin": ui_pin,
                "EDB_Pin": item.get("edb_pin", ""),
                "UI_Net": item.get("ui_net", ""),
                "EDB_Net": item.get("edb_net", ""),
                "Net_Match": item.get("net_match", False),
                "Method": item.get("method", ""),
                "Confidence": item.get("confidence", 0.0),
            })
    rows.sort(key=lambda r: (str(r["Designator"]), str(r["UI_Pin"])))

    csv_path = output_dir / "pin_crosswalk_map.csv"
    json_path = output_dir / "pin_crosswalk_map.json"
    try:
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["Designator", "UI_Pin", "EDB_Pin", "UI_Net", "EDB_Net", "Net_Match", "Method", "Confidence"],
            )
            writer.writeheader()
            for r in rows:
                writer.writerow(r)
    except Exception:
        pass
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({"Schema_Version": 1, "Records": rows}, f, indent=2, ensure_ascii=False)
    except Exception:
        pass
    return csv_path, json_path

def build_component_edb_truth_table(comp_inst):
    rows = []
    for p_key, p_inst in (getattr(comp_inst, "pins", {}) or {}).items():
        disp_names = _extract_pin_display_names(p_key, p_inst)
        tokens = sorted({_normalize_pin_token(x) for x in disp_names if str(x).strip()})
        net_name = str(getattr(p_inst, "net_name", "") or "").strip()
        x = y = None
        try:
            x = float(p_inst.position[0])
            y = float(p_inst.position[1])
        except Exception:
            pass
        rows.append(
            {
                "Designator": str(getattr(comp_inst, "name", "") or ""),
                "EDB_Pin": str(p_key),
                "UI_Pin_Candidates": [str(n) for n in disp_names if str(n).strip()],
                "Tokens": tokens,
                "Net": net_name,
                "X": x,
                "Y": y,
                "Is_Power_Like": bool(_is_power_like_net(net_name)),
                "Is_Signal_Like": bool(_is_signal_like_net(net_name)),
            }
        )
    return rows

def build_component_ui_api_crosswalk(
    comp_inst,
    components_api=None,
    siw_component_record: dict | None = None,
    ndf_component_record: dict | None = None,
    net_alias_map=None,
):
    """
    Build dedicated UI <-> API pin-name crosswalk for one component.
    Output schema:
      {
        "by_ui": { "<UI_NAME>": [ {edb_pin, net, source, confidence}, ... ] },
        "by_token": { "<NORM_TOKEN>": [ ... ] },
        "records": [ ...flat records... ]
      }
    """
    by_ui = defaultdict(list)
    by_token = defaultdict(list)
    records = []
    seen = set()

    def _add(ui_name: str, edb_pin: str, source: str, confidence: float):
        ui_name = str(ui_name or "").strip()
        edb_pin = str(edb_pin or "").strip()
        if not ui_name or not edb_pin:
            return
        if edb_pin not in comp_inst.pins:
            return
        net_name = str(getattr(comp_inst.pins[edb_pin], "net_name", "") or "").strip()
        key = (ui_name.upper(), edb_pin.upper(), source)
        if key in seen:
            return
        seen.add(key)
        rec = {
            "ui_pin": ui_name,
            "ui_token": _normalize_pin_token(ui_name),
            "edb_pin": edb_pin,
            "net": net_name,
            "source": source,
            "confidence": float(confidence),
        }
        by_ui[ui_name].append(rec)
        tok = rec["ui_token"]
        if tok:
            by_token[tok].append(rec)
        records.append(rec)

    # Runtime component pins: strongest source.
    for p_key, p_inst in (getattr(comp_inst, "pins", {}) or {}).items():
        for nm in _extract_pin_display_names(p_key, p_inst):
            _add(nm, p_key, "runtime_pin_display", 0.96)

    # Helper API names: can reveal UI labels not exposed in dict keys.
    if components_api is not None:
        try:
            helper_pins = components_api.get_pin_from_component(comp_inst.name) or []
        except Exception:
            helper_pins = []
        for hp in helper_pins:
            name_candidates = []
            for getter in (
                lambda x: getattr(x, "component_pin", ""),
                lambda x: getattr(x, "aedt_name", ""),
                lambda x: getattr(x, "name", ""),
            ):
                try:
                    v = str(getter(hp) or "").strip()
                except Exception:
                    v = ""
                if v:
                    name_candidates.append(v)
            try:
                ap = str(components_api.get_aedt_pin_name(hp) or "").strip()
            except Exception:
                ap = ""
            if ap:
                name_candidates.append(ap)

            helper_edb_pin = ""
            try:
                helper_edb_pin = str(getattr(hp, "component_pin", "") or "").strip()
            except Exception:
                helper_edb_pin = ""
            if helper_edb_pin and helper_edb_pin in comp_inst.pins:
                for nm in name_candidates:
                    _add(nm, helper_edb_pin, "components_api", 0.93)

    # SIW-assisted UI->API bridge by coordinate (+ optional net hint from NDF).
    # This is useful when UI pin names exist in SIW/CMP but are not exposed by runtime wrapper.
    siw_component_record = siw_component_record or {}
    ndf_component_record = ndf_component_record or {}
    net_alias_map = net_alias_map or {}
    if siw_component_record:
        edb_rows = []
        for p_key, p_inst in (getattr(comp_inst, "pins", {}) or {}).items():
            try:
                px, py = float(p_inst.position[0]), float(p_inst.position[1])
            except Exception:
                continue
            edb_rows.append((p_key, p_inst, px, py, str(getattr(p_inst, "net_name", "") or "").strip()))
        for ui_pin, srec in siw_component_record.items():
            ui_pin = str(ui_pin or "").strip()
            if not ui_pin:
                continue
            if ui_pin in by_ui:
                # Already resolved by runtime/helper names.
                continue
            try:
                tx, ty = float(srec.get("x")), float(srec.get("y"))
            except Exception:
                continue
            net_hint = str((ndf_component_record.get(ui_pin, {}) or {}).get("net", "")).strip()
            cands = []
            for p_key, p_inst, px, py, p_net in edb_rows:
                d2 = (px - tx) ** 2 + (py - ty) ** 2
                net_ok = True
                if net_hint:
                    net_ok = _net_matches_spec(net_hint, p_net, net_alias_map)
                cands.append((0 if net_ok else 1, d2, p_key, p_net))
            if not cands:
                continue
            cands.sort(key=lambda x: (x[0], x[1], str(x[2])))
            net_penalty, d2, best_key, best_net = cands[0]
            # Confidence policy:
            # - net+coord match => high
            # - coord-only bridge => medium (kept for investigation cases like V31)
            if net_penalty == 0:
                conf = 0.91 if d2 < 2.5e-6 else 0.86
                src = "siw_coord_net"
            else:
                conf = 0.72 if d2 < 2.5e-6 else 0.62
                src = "siw_coord_only"
            _add(ui_pin, best_key, src, conf)

    # Stable sort within each key
    for k in list(by_ui.keys()):
        by_ui[k] = sorted(by_ui[k], key=lambda r: (-float(r.get("confidence", 0.0)), str(r.get("edb_pin", ""))))
    for k in list(by_token.keys()):
        by_token[k] = sorted(by_token[k], key=lambda r: (-float(r.get("confidence", 0.0)), str(r.get("edb_pin", ""))))

    return {
        "by_ui": dict(by_ui),
        "by_token": dict(by_token),
        "records": sorted(records, key=lambda r: (str(r.get("ui_pin", "")), str(r.get("edb_pin", "")))),
    }

def export_edb_truth_table_reports(output_dir: Path, truth_by_comp: dict):
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for comp_name, comp_rows in (truth_by_comp or {}).items():
        for r in (comp_rows or []):
            rows.append(
                {
                    "Designator": str(comp_name),
                    "EDB_Pin": r.get("EDB_Pin", ""),
                    "UI_Pin_Candidates": ";".join(r.get("UI_Pin_Candidates", []) or []),
                    "Net": r.get("Net", ""),
                    "X": r.get("X", ""),
                    "Y": r.get("Y", ""),
                    "Is_Power_Like": r.get("Is_Power_Like", False),
                    "Is_Signal_Like": r.get("Is_Signal_Like", False),
                    "Tokens": ";".join(r.get("Tokens", []) or []),
                }
            )
    rows.sort(key=lambda r: (str(r["Designator"]), str(r["EDB_Pin"])))
    csv_path = output_dir / "edb_truth_table.csv"
    json_path = output_dir / "edb_truth_table.json"
    try:
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "Designator", "EDB_Pin", "UI_Pin_Candidates", "Net", "X", "Y",
                    "Is_Power_Like", "Is_Signal_Like", "Tokens",
                ],
            )
            writer.writeheader()
            for r in rows:
                writer.writerow(r)
    except Exception:
        pass
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({"Schema_Version": 1, "Records": rows}, f, indent=2, ensure_ascii=False)
    except Exception:
        pass
    return csv_path, json_path

def export_ui_api_crosswalk_reports(output_dir: Path, ui_api_crosswalk_by_comp: dict):
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for comp_name, cmap in (ui_api_crosswalk_by_comp or {}).items():
        for rec in (cmap or {}).get("records", []) or []:
            rows.append(
                {
                    "Designator": str(comp_name),
                    "UI_Pin": rec.get("ui_pin", ""),
                    "UI_Token": rec.get("ui_token", ""),
                    "EDB_Pin": rec.get("edb_pin", ""),
                    "Net": rec.get("net", ""),
                    "Source": rec.get("source", ""),
                    "Confidence": rec.get("confidence", 0.0),
                }
            )
    rows.sort(key=lambda r: (str(r["Designator"]), str(r["UI_Pin"]), str(r["EDB_Pin"])))

    csv_path = output_dir / "ui_api_crosswalk_map.csv"
    json_path = output_dir / "ui_api_crosswalk_map.json"
    try:
        with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=["Designator", "UI_Pin", "UI_Token", "EDB_Pin", "Net", "Source", "Confidence"],
            )
            writer.writeheader()
            for r in rows:
                writer.writerow(r)
    except Exception:
        pass
    try:
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({"Schema_Version": 1, "Records": rows}, f, indent=2, ensure_ascii=False)
    except Exception:
        pass
    return csv_path, json_path


def parse_cmp_pin_records(cmp_path: Path, target_designators=None):
    records = defaultdict(dict)
    if not cmp_path or not cmp_path.exists():
        return records
    target_set = {str(x).upper() for x in (target_designators or [])}
    try:
        with open(cmp_path, "r", encoding="utf-8-sig", errors="ignore") as f:
            for raw in f:
                line = raw.strip()
                if not line.startswith("X "):
                    continue
                toks = line.split()
                if len(toks) < 4:
                    continue
                designator_idx = None
                for i, tok in enumerate(toks):
                    if re.fullmatch(r"[A-Za-z]{1,4}\d{1,5}", tok):
                        if i + 1 < len(toks):
                            next_tok = toks[i + 1]
                            if re.fullmatch(r"[A-Za-z]{1,4}\d{1,5}", next_tok):
                                designator_idx = i
                                break
                if designator_idx is None:
                    continue
                designator = toks[designator_idx].strip()
                pin_name = toks[designator_idx + 1].strip()
                if target_set and designator.upper() not in target_set:
                    continue

                x_val = None
                y_val = None
                for i in range(len(toks) - 2, -1, -1):
                    try:
                        y_val = float(toks[i + 1])
                        x_val = float(toks[i])
                        break
                    except Exception:
                        continue
                records[designator][pin_name] = {
                    "pin": pin_name,
                    "x": x_val,
                    "y": y_val,
                }
    except Exception:
        pass
    return records


def _iter_all_padstack_instances(edbapp):
    try:
        inst_map = getattr(getattr(edbapp, "padstacks", None), "instances", {}) or {}
        for _, pobj in inst_map.items():
            yield pobj
    except Exception:
        return


def _pad_component_name(pad_obj) -> str:
    try:
        comp = getattr(pad_obj, "component", None)
        if comp is None:
            return ""
        return str(getattr(comp, "name", "") or "").strip()
    except Exception:
        return ""


def _pad_position(pad_obj):
    for attr in ("position", "center"):
        try:
            p = getattr(pad_obj, attr, None)
            if p is not None and len(p) >= 2:
                return float(p[0]), float(p[1])
        except Exception:
            pass
    try:
        p = pad_obj.GetPosition()
        if p is not None and len(p) >= 2:
            return float(p[0]), float(p[1])
    except Exception:
        pass
    return None


def _find_component_pin_by_global_padstack_scan(
    comp_inst,
    spec_pin: str,
    spec_nets,
    net_alias_map,
    excluded_pin_names=None,
    edbapp=None,
):
    """
    Fallback #1: scan board-global padstacks when component-local pin lookup fails.
    """
    if edbapp is None:
        return None, None
    excluded = {str(p) for p in (excluded_pin_names or set()) if str(p)}
    target_pin_tok = _normalize_pin_token(spec_pin)
    spec_nets_local = [str(n).strip() for n in (spec_nets or []) if str(n).strip()]
    comp_name_u = str(getattr(comp_inst, "name", "")).upper()

    # Build quick lookup for existing component pins.
    pin_rows = []
    for p_key, p_inst in comp_inst.pins.items():
        if p_key in excluded:
            continue
        pin_rows.append(
            {
                "key": p_key,
                "inst": p_inst,
                "tokens": {_normalize_pin_token(x) for x in _extract_pin_display_names(p_key, p_inst)},
                "net": str(getattr(p_inst, "net_name", "") or ""),
                "pos": _pad_position(p_inst),
            }
        )
    if not pin_rows:
        return None, None

    best = None
    for pad in _iter_all_padstack_instances(edbapp):
        pcomp = _pad_component_name(pad).upper()
        if pcomp and pcomp != comp_name_u:
            continue
        pad_name = str(getattr(pad, "name", "") or "").strip()
        if not pad_name:
            continue
        pad_tok = _normalize_pin_token(pad_name)
        if target_pin_tok and target_pin_tok not in pad_tok:
            continue
        pad_net = str(getattr(pad, "net_name", "") or "").strip()
        net_ok = True
        if spec_nets_local:
            net_ok = any(_net_matches_spec(snet, pad_net, net_alias_map) for snet in spec_nets_local)
        if not net_ok:
            continue
        ppos = _pad_position(pad)

        # Match found global pad to existing component pin by token/net/coord.
        for row in pin_rows:
            tok_overlap = 1 if (pad_tok and pad_tok in row["tokens"]) else 0
            row_net = row["net"]
            row_net_ok = True
            if spec_nets_local:
                row_net_ok = any(_net_matches_spec(snet, row_net, net_alias_map) for snet in spec_nets_local)
            if not row_net_ok:
                continue
            d2 = 1e12
            if ppos and row["pos"]:
                try:
                    d2 = (float(ppos[0]) - float(row["pos"][0])) ** 2 + (float(ppos[1]) - float(row["pos"][1])) ** 2
                except Exception:
                    d2 = 1e12
            score = (0 if tok_overlap else 10) + d2
            cand = (score, row["key"], row["inst"])
            if best is None or cand < best:
                best = cand

    if best is not None:
        _, p_key, p_inst = best
        return p_inst, p_key
    return None, None


def _find_component_pin_by_spatial_query(
    comp_inst,
    spec_pin: str,
    spec_nets,
    net_alias_map,
    cmp_pin_record=None,
    excluded_pin_names=None,
    edbapp=None,
):
    """
    Fallback #2: force lookup by physical location query.
    """
    if edbapp is None:
        return None, None
    excluded = {str(p) for p in (excluded_pin_names or set()) if str(p)}
    if not (cmp_pin_record and cmp_pin_record.get("x") is not None and cmp_pin_record.get("y") is not None):
        return None, None

    tx, ty = float(cmp_pin_record["x"]), float(cmp_pin_record["y"])
    spec_nets_local = [str(n).strip() for n in (spec_nets or []) if str(n).strip()]
    pad = None
    try:
        pad = edbapp.padstacks.get_padstack_instance_by_position([tx, ty])
    except Exception:
        pad = None
    if pad is None:
        return None, None

    pad_net = str(getattr(pad, "net_name", "") or "").strip()
    if spec_nets_local and not any(_net_matches_spec(snet, pad_net, net_alias_map) for snet in spec_nets_local):
        return None, None

    pad_name_tok = _normalize_pin_token(str(getattr(pad, "name", "") or ""))
    # Map queried pad to a real component pin instance.
    best = None
    for p_key, p_inst in comp_inst.pins.items():
        if p_key in excluded:
            continue
        disp_tokens = {_normalize_pin_token(x) for x in _extract_pin_display_names(p_key, p_inst)}
        tok_hit = 1 if (pad_name_tok and pad_name_tok in disp_tokens) else 0
        p_net = str(getattr(p_inst, "net_name", "") or "")
        if spec_nets_local and not any(_net_matches_spec(snet, p_net, net_alias_map) for snet in spec_nets_local):
            continue
        d2 = 1e12
        try:
            px, py = float(p_inst.position[0]), float(p_inst.position[1])
            ppos = _pad_position(pad)
            if ppos:
                d2 = (float(ppos[0]) - px) ** 2 + (float(ppos[1]) - py) ** 2
        except Exception:
            d2 = 1e12
        cand = ((0 if tok_hit else 10) + d2, p_key, p_inst)
        if best is None or cand < best:
            best = cand

    if best is not None:
        _, p_key, p_inst = best
        return p_inst, p_key
    return None, None


def resolve_spec_pin_to_edb_pin(
    comp_inst,
    spec_pin: str,
    spec_nets,
    net_alias_map,
    cmp_pin_record: dict | None,
    cmp_component_record: dict | None = None,
    crosswalk_pin_record: dict | None = None,
    components_api=None,
    excluded_pin_names=None,
    edbapp=None,
    truth_component_rows=None,
    strict_refdes_pin: bool = False,
):
    excluded = {str(p) for p in (excluded_pin_names or set()) if str(p)}
    spec_pin = str(spec_pin or "").strip()
    if not spec_pin:
        return None, "empty_spec_pin", None

    spec_nets_local = [str(n).strip() for n in (spec_nets or []) if str(n).strip()]
    pin_rows = _build_pin_rows_multisource(
        comp_inst=comp_inst,
        components_api=components_api,
        cmp_pin_record=cmp_pin_record,
        cmp_component_record=cmp_component_record,
    )
    if not pin_rows:
        return None, "no_pin_rows", None
    has_coord = any((r.get("d2", 1e12) < 1e11) for r in pin_rows)

    # ------------------------------------------------------------------
    # Pass 1: Designator + Pin Name (SI-TDR style strict-first)
    # ------------------------------------------------------------------
    if spec_pin in comp_inst.pins and spec_pin not in excluded:
        return comp_inst.pins[spec_pin], "exact", spec_pin

    if strict_refdes_pin and components_api is not None:
        try:
            helper_pins = components_api.get_pin_from_component(comp_inst.name) or []
            for hp in helper_pins:
                names = []
                for getter in (
                    lambda x: getattr(x, "component_pin", ""),
                    lambda x: getattr(x, "aedt_name", ""),
                    lambda x: getattr(x, "name", ""),
                ):
                    try:
                        val = str(getter(hp) or "").strip()
                    except Exception:
                        val = ""
                    if val:
                        names.append(val)
                try:
                    aedt_name = str(components_api.get_aedt_pin_name(hp) or "").strip()
                    if aedt_name:
                        names.append(aedt_name)
                except Exception:
                    pass

                if not any(str(spec_pin).strip() == nm for nm in names):
                    continue

                hp_net = str(getattr(hp, "net_name", "") or "").strip()
                if spec_nets_local and not any(_net_matches_spec_pin_strict(n, hp_net, net_alias_map) for n in spec_nets_local):
                    continue

                # helper pin object -> component local pin instance by exact display-name equality
                for p_key, p_inst in comp_inst.pins.items():
                    if p_key in excluded:
                        continue
                    disp_names = [str(x).strip() for x in _extract_pin_display_names(p_key, p_inst) if str(x).strip()]
                    if any(str(spec_pin).strip() == dn for dn in disp_names):
                        p_net = str(getattr(p_inst, "net_name", "") or "").strip()
                        if spec_nets_local and not any(_net_matches_spec_pin_strict(n, p_net, net_alias_map) for n in spec_nets_local):
                            continue
                        return p_inst, "strict_refdes_pin", p_key
        except Exception:
            pass

        # strict mode with bounded fallback:
        # 1) same-designator net-name match
        # 2) if multi-hit, nearest by coordinate
        # 3) if still ambiguous/no-hit, fail-close(skip)
        strict_net_candidates = []
        if spec_nets_local:
            for row in pin_rows:
                p_key, p_inst = row["key"], row["inst"]
                if p_key in excluded:
                    continue
                p_net = str(getattr(p_inst, "net_name", "") or "").strip()
                if not p_net:
                    continue
                if any(_net_matches_spec_pin_strict(n, p_net, net_alias_map) for n in spec_nets_local):
                    strict_net_candidates.append((p_key, p_inst, p_net))

        if strict_net_candidates:
            # For power-like spec nets, prefer non-signal pins.
            if any(_is_power_like_net(n) for n in spec_nets_local):
                non_signal = [(k, p, n) for k, p, n in strict_net_candidates if not _is_signal_like_net(n)]
                if non_signal:
                    strict_net_candidates = non_signal

            if len(strict_net_candidates) == 1:
                p_key, p_inst, _ = strict_net_candidates[0]
                return p_inst, "strict_net_unique", p_key

            if has_coord:
                ranked = sorted(
                    [
                        (
                            next((r.get("d2", 1e12) for r in pin_rows if r["key"] == p_key), 1e12),
                            p_key,
                            p_inst,
                        )
                        for p_key, p_inst, _ in strict_net_candidates
                    ],
                    key=lambda x: (x[0], str(x[1])),
                )
                _, p_key, p_inst = ranked[0]
                return p_inst, "strict_net_multi_coord", p_key

            # no coord: deterministic but conservative
            p_key, p_inst, _ = sorted(strict_net_candidates, key=lambda x: str(x[0]))[0]
            return p_inst, "strict_net_multi_no_coord", p_key

        # strict mode fail-close: unresolved after bounded same-designator net fallback.
        return None, "strict_no_exact_pin", None

    # Components helper API path (UI name -> internal key)
    if components_api is not None:
        try:
            helper_pins = components_api.get_pin_from_component(comp_inst.name, pin_name=spec_pin) or []
            helper_tokens = set()
            for hp in helper_pins:
                for getter in (
                    lambda x: getattr(x, "component_pin", ""),
                    lambda x: getattr(x, "aedt_name", ""),
                    lambda x: getattr(x, "name", ""),
                ):
                    try:
                        helper_tokens.add(_normalize_pin_token(getter(hp)))
                    except Exception:
                        pass
                try:
                    helper_tokens.add(_normalize_pin_token(components_api.get_aedt_pin_name(hp)))
                except Exception:
                    pass
            helper_tokens = {t for t in helper_tokens if t}
            if helper_tokens:
                for row in pin_rows:
                    p_key, p_inst = row["key"], row["inst"]
                    if p_key in excluded:
                        continue
                    disp_tokens = set(row.get("tokens", set()))
                    if helper_tokens.intersection(disp_tokens):
                        return p_inst, "exact_components_api", p_key
        except Exception:
            pass

    # display/normalized name match
    for row in pin_rows:
        p_key, p_inst = row["key"], row["inst"]
        if p_key in excluded:
            continue
        disp_tokens = set(row.get("tokens", set()))
        if _normalize_pin_token(spec_pin) in disp_tokens:
            return p_inst, "exact_display_name", p_key

    # EDB truth-table assisted exact (RefDes -> all pins -> token/net staged filter)
    truth_rows = list(truth_component_rows or [])
    if truth_rows:
        spec_tok = _normalize_pin_token(spec_pin)
        staged = []
        for tr in truth_rows:
            p_key = str(tr.get("EDB_Pin", "")).strip()
            if not p_key or p_key in excluded or p_key not in comp_inst.pins:
                continue
            token_hit = spec_tok in set(tr.get("Tokens", []) or [])
            if not token_hit:
                continue
            tr_net = str(tr.get("Net", "")).strip()
            net_ok = True
            if spec_nets_local:
                net_ok = any(_net_matches_spec(n, tr_net, net_alias_map) for n in spec_nets_local)
            staged.append((0 if net_ok else 1, p_key))
        if staged:
            staged.sort(key=lambda x: (x[0], str(x[1])))
            chosen = staged[0][1]
            return comp_inst.pins[chosen], "truth_table_exact", chosen

    # Crosswalk (converted internal key)
    if crosswalk_pin_record:
        cand_pin = str(crosswalk_pin_record.get("edb_pin", "")).strip()
        conf = float(crosswalk_pin_record.get("confidence", 0.0) or 0.0)
        if cand_pin and cand_pin not in excluded and cand_pin in comp_inst.pins and conf >= 0.55:
            return comp_inst.pins[cand_pin], "crosswalk_pin", cand_pin

    # ------------------------------------------------------------------
    # Pass 2: Net Name search
    # ------------------------------------------------------------------
    net_candidates = []
    if spec_nets_local:
        for row in pin_rows:
            p_key, p_inst = row["key"], row["inst"]
            if p_key in excluded:
                continue
            nets_to_check = {str(row.get("net_primary", "")).strip()} | {str(n).strip() for n in row.get("net_candidates", set())}
            nets_to_check = {n for n in nets_to_check if n}
            if any(_net_matches_spec_pin_strict(n, cand_net, net_alias_map) for n in spec_nets_local for cand_net in nets_to_check):
                net_candidates.append((p_key, p_inst))

    if len(net_candidates) == 1:
        p_key, p_inst = net_candidates[0]
        return p_inst, "net_unique", p_key
    if len(net_candidates) > 1:
        # Pass 2-3: multiple net-hit candidates => choose nearest to spec coordinate.
        if has_coord:
            ranked = sorted(
                [
                    (next((r.get("d2", 1e12) for r in pin_rows if r["key"] == p_key), 1e12), p_key, p_inst)
                    for p_key, p_inst in net_candidates
                ],
                key=lambda x: (x[0], str(x[1])),
            )
            _, p_key, p_inst = ranked[0]
            return p_inst, "net_multi_coord", p_key
        p_key, p_inst = sorted(net_candidates, key=lambda x: str(x[0]))[0]
        return p_inst, "net_multi_no_coord", p_key

    # ------------------------------------------------------------------
    # Global fallback chain:
    #   1) board-wide padstack scan
    #   2) spatial query by CMP coordinate
    # ------------------------------------------------------------------
    gp_inst, gp_key = _find_component_pin_by_global_padstack_scan(
        comp_inst=comp_inst,
        spec_pin=spec_pin,
        spec_nets=spec_nets_local,
        net_alias_map=net_alias_map,
        excluded_pin_names=excluded,
        edbapp=edbapp,
    )
    if gp_inst is not None:
        return gp_inst, "global_padstack_scan", gp_key

    sq_inst, sq_key = _find_component_pin_by_spatial_query(
        comp_inst=comp_inst,
        spec_pin=spec_pin,
        spec_nets=spec_nets_local,
        net_alias_map=net_alias_map,
        cmp_pin_record=cmp_pin_record,
        excluded_pin_names=excluded,
        edbapp=edbapp,
    )
    if sq_inst is not None:
        return sq_inst, "spatial_query", sq_key

    # ------------------------------------------------------------------
    # Pass 3: component-local nearest fallback (last resort)
    # ------------------------------------------------------------------
    all_pins = [(row["key"], row["inst"]) for row in pin_rows if row["key"] not in excluded]
    if not all_pins:
        return None, "no_net_candidate", None

    if has_coord:
        filtered = all_pins
        if spec_nets_local and any(_is_power_like_net(n) for n in spec_nets_local):
            non_signal = [(k, p) for k, p in all_pins if not _is_signal_like_net(p.net_name)]
            if non_signal:
                filtered = non_signal
            else:
                return None, "no_net_candidate_signal_filtered", None
        ranked_all = sorted(
            [
                (next((r.get("d2", 1e12) for r in pin_rows if r["key"] == p_key), 1e12), p_key, p_inst)
                for p_key, p_inst in filtered
            ],
            key=lambda x: (x[0], str(x[1])),
        )
        _, p_key, p_inst = ranked_all[0]
        return p_inst, "coord_only", p_key

    p_key, p_inst = sorted(all_pins, key=lambda x: str(x[0]))[0]
    local_fallback = (p_inst, "fallback_first_pin", p_key)
    return local_fallback

def normalize_spec_pin_for_strict_mode(
    spec_pin: str,
    spec_nets,
    crosswalk_pin_record: dict | None,
    net_alias_map,
):
    """
    SI-TDR strict mode 보조:
    - 런타임에서는 RefDes+Pin exact를 사용하되,
    - 사전 crosswalk가 신뢰 가능한 경우 Spec pin을 EDB pin으로 정규화해 exact 매칭에 진입시킨다.
    """
    spec_pin = str(spec_pin or "").strip()
    if not spec_pin:
        return spec_pin, None
    if not crosswalk_pin_record:
        return spec_pin, None

    edb_pin = str(crosswalk_pin_record.get("edb_pin", "")).strip()
    if not edb_pin:
        return spec_pin, None
    method = str(crosswalk_pin_record.get("method", "")).strip().lower()
    conf = float(crosswalk_pin_record.get("confidence", 0.0) or 0.0)
    ui_net = str(crosswalk_pin_record.get("ui_net", "") or "").strip()
    edb_net = str(crosswalk_pin_record.get("edb_net", "") or "").strip()
    net_match = bool(crosswalk_pin_record.get("net_match", False))

    # 신뢰도/방법 기반 가드: coord-only 류는 제외.
    if conf < 0.85:
        return spec_pin, None
    if method not in {"pin_exact", "coord_net", "coord_net_validated"}:
        return spec_pin, None

    # 전원 스펙은 net 불일치에 민감해야 하지만, UI/PCB 네이밍 불일치를 감안해
    # pin_exact 고신뢰인 경우에 한해 통과를 허용한다.
    spec_nets_local = [str(n).strip() for n in (spec_nets or []) if str(n).strip()]
    has_power_spec = any(_is_power_like_net(n) for n in spec_nets_local)
    if has_power_spec and (not net_match):
        if method != "pin_exact":
            return spec_pin, None

    reason = {
        "from": spec_pin,
        "to": edb_pin,
        "method": method,
        "confidence": round(conf, 3),
        "ui_net": ui_net,
        "edb_net": edb_net,
        "net_match": net_match,
    }
    return edb_pin, reason

def resolve_spec_pin_via_spec_api_gui(
    comp_inst,
    spec_pin: str,
    spec_nets,
    crosswalk_pin_record: dict | None,
    ui_api_component_map: dict | None,
    truth_component_rows: list[dict] | None,
    net_alias_map,
    excluded_pin_names=None,
):
    """
    3-step relation resolver:
      1) Spec -> API candidate mapping
      2) API -> EDB UI candidate mapping
      3) Relation join and final API pin selection for downstream setup (port/vrm)
    Returns:
      (pin_inst, resolved_pin_name, trace_dict) or (None, None, trace_dict)
    """
    excluded = {str(x) for x in (excluded_pin_names or set()) if str(x)}
    spec_pin = str(spec_pin or "").strip()
    spec_tok = _normalize_pin_token(spec_pin)
    spec_nets_local = [str(n).strip() for n in (spec_nets or []) if str(n).strip()]
    rows = list(truth_component_rows or [])

    stage1 = []  # spec -> api
    stage2 = []  # api -> ui
    seen_stage1 = set()

    def _add_stage1(pin_name: str, reason: str, pri: int, conf: float):
        pin_name = str(pin_name or "").strip()
        if not pin_name or pin_name in excluded:
            return
        if pin_name not in comp_inst.pins:
            return
        if pin_name in seen_stage1:
            return
        p_inst = comp_inst.pins[pin_name]
        p_net = str(getattr(p_inst, "net_name", "") or "").strip()
        net_match = True
        if spec_nets_local:
            net_match = any(_net_matches_spec(n, p_net, net_alias_map) for n in spec_nets_local)
        stage1.append(
            {
                "pin": pin_name,
                "net": p_net,
                "reason": reason,
                "priority": int(pri),
                "confidence": float(conf),
                "net_match": bool(net_match),
            }
        )
        seen_stage1.add(pin_name)

    # Step 0) dedicated UI<->API crosswalk mapping (highest priority)
    ui_map = ui_api_component_map or {}
    by_ui = (ui_map.get("by_ui", {}) or {}) if isinstance(ui_map, dict) else {}
    by_tok = (ui_map.get("by_token", {}) or {}) if isinstance(ui_map, dict) else {}
    for rec in (by_ui.get(spec_pin, []) or []):
        _add_stage1(
            rec.get("edb_pin", ""),
            reason=f"spec_api:ui_api:{rec.get('source', 'map')}",
            pri=-2,
            conf=max(float(rec.get("confidence", 0.0) or 0.0), 0.9),
        )
    if spec_tok:
        for rec in (by_tok.get(spec_tok, []) or []):
            _add_stage1(
                rec.get("edb_pin", ""),
                reason=f"spec_api:ui_api_token:{rec.get('source', 'map')}",
                pri=-1,
                conf=max(float(rec.get("confidence", 0.0) or 0.0), 0.88),
            )

    # Step 1) Spec -> API candidate mapping
    for p_key, p_inst in comp_inst.pins.items():
        if p_key in excluded:
            continue
        disp_names = [str(x).strip() for x in _extract_pin_display_names(p_key, p_inst) if str(x).strip()]
        disp_toks = {_normalize_pin_token(x) for x in disp_names}
        if spec_pin and spec_pin in disp_names:
            _add_stage1(p_key, reason="spec_api:runtime_exact", pri=0, conf=0.96)
        elif spec_tok and spec_tok in disp_toks:
            _add_stage1(p_key, reason="spec_api:runtime_token", pri=1, conf=0.90)

    if crosswalk_pin_record:
        cw_pin = str(crosswalk_pin_record.get("edb_pin", "")).strip()
        cw_conf = float(crosswalk_pin_record.get("confidence", 0.0) or 0.0)
        cw_method = str(crosswalk_pin_record.get("method", "") or "").strip()
        if cw_pin:
            _add_stage1(
                cw_pin,
                reason=f"spec_api:crosswalk:{cw_method or 'unknown'}",
                pri=2,
                conf=max(cw_conf, 0.80),
            )

    # Step 2) API -> EDB UI mapping (truth/runtime evidence)
    ui_meta_by_pin = {}
    for row in rows:
        pin_name = str(row.get("EDB_Pin", "")).strip()
        if not pin_name:
            continue
        ui_names = [str(x).strip() for x in (row.get("UI_Pin_Candidates", []) or []) if str(x).strip()]
        toks = {str(x).strip() for x in (row.get("Tokens", []) or []) if str(x).strip()}
        ui_tok = {_normalize_pin_token(x) for x in ui_names}
        ui_hit = bool(spec_pin and spec_pin in ui_names) or bool(spec_tok and (spec_tok in toks or spec_tok in ui_tok))
        ui_meta_by_pin[pin_name] = {
            "ui_names": ui_names,
            "tokens": sorted(toks),
            "ui_hit": ui_hit,
            "from": "truth_table",
        }

    for p_key, p_inst in comp_inst.pins.items():
        if p_key in excluded:
            continue
        disp_names = [str(x).strip() for x in _extract_pin_display_names(p_key, p_inst) if str(x).strip()]
        disp_toks = {_normalize_pin_token(x) for x in disp_names}
        ui_hit = bool(spec_pin and spec_pin in disp_names) or bool(spec_tok and spec_tok in disp_toks)
        prev = ui_meta_by_pin.get(p_key, {})
        merged_names = sorted(set((prev.get("ui_names") or []) + disp_names))
        merged_toks = sorted(set((prev.get("tokens") or []) + list(disp_toks)))
        ui_meta_by_pin[p_key] = {
            "ui_names": merged_names,
            "tokens": merged_toks,
            "ui_hit": bool(prev.get("ui_hit", False) or ui_hit),
            "from": "truth+runtime" if prev else "runtime",
        }

    # keep stage2 snapshot for debug/report
    for pin_name, meta in ui_meta_by_pin.items():
        p_net = ""
        try:
            p_net = str(getattr(comp_inst.pins.get(pin_name), "net_name", "") or "").strip()
        except Exception:
            p_net = ""
        stage2.append(
            {
                "pin": pin_name,
                "net": p_net,
                "ui_hit": bool(meta.get("ui_hit", False)),
                "ui_names": list(meta.get("ui_names") or []),
                "tokens": list(meta.get("tokens") or []),
                "source": meta.get("from", ""),
            }
        )

    # Step 3) relation join and final API selection
    relation = []
    for s1 in stage1:
        ui_meta = ui_meta_by_pin.get(s1["pin"], {})
        relation.append(
            {
                **s1,
                "ui_hit": bool(ui_meta.get("ui_hit", False)),
                "ui_source": ui_meta.get("from", ""),
                "ui_names": list(ui_meta.get("ui_names") or []),
            }
        )

    if not relation:
        return None, None, {"status": "no_spec_api_candidate", "spec_pin": spec_pin, "stage1": [], "stage2": stage2}

    linked = [c for c in relation if c.get("ui_hit")]
    if linked:
        ranked_pool = linked
        status = "ok_relation"
    else:
        ranked_pool = relation
        status = "ok_spec_api_only"

    ranked = sorted(
        ranked_pool,
        key=lambda c: (
            0 if c.get("ui_hit") else 1,
            0 if c["net_match"] else 1,
            int(c["priority"]),
            -float(c["confidence"]),
            str(c["pin"]),
        ),
    )
    best = ranked[0]
    if best["pin"] not in comp_inst.pins:
        return None, None, {"status": "selected_pin_not_found", "spec_pin": spec_pin, "stage1": stage1, "stage2": stage2}

    # strict policy: require relation link when strict mode is enabled at caller
    # (caller already handles strict skip). We expose status so caller can decide.
    return comp_inst.pins[best["pin"]], best["pin"], {
        "status": status,
        "spec_pin": spec_pin,
        "selected": best,
        "relation_candidates": ranked,
        "stage1_spec_api": stage1,
        "stage2_api_ui": stage2,
    }

def remap_pin_by_net_then_ui(
    comp_inst,
    spec_pin: str,
    spec_nets,
    current_pin_inst,
    current_pin_name: str | None,
    crosswalk_pin_record: dict | None,
    truth_component_rows: list[dict] | None,
    cmp_pin_record: dict | None,
    net_alias_map,
    excluded_pin_names=None,
):
    """
    Policy:
      1) If current mapping net mismatches Spec net, try to switch by net.
      2) Prefer crosswalk internal key when its net matches.
      3) If key missing, reverse-search pins by same net and re-verify with EDB UI tokens/names.
    """
    excluded = {str(x) for x in (excluded_pin_names or set()) if str(x)}
    spec_pin = str(spec_pin or "").strip()
    spec_tok = _normalize_pin_token(spec_pin)
    spec_nets_local = [str(n).strip() for n in (spec_nets or []) if str(n).strip()]
    if not spec_nets_local:
        return current_pin_inst, current_pin_name, {"mode": "no_spec_net", "changed": False, "ui_verified": False}

    def _net_ok(pinst):
        try:
            p_net = str(getattr(pinst, "net_name", "") or "").strip()
        except Exception:
            p_net = ""
        return bool(p_net) and any(_net_matches_spec_pin_strict(sn, p_net, net_alias_map) for sn in spec_nets_local)

    truth_by_pin = {}
    for r in (truth_component_rows or []):
        p = str(r.get("EDB_Pin", "")).strip()
        if p:
            truth_by_pin[p] = r

    # already net-valid: keep as is
    if current_pin_inst is not None and _net_ok(current_pin_inst):
        return current_pin_inst, current_pin_name, {"mode": "net_valid_keep", "changed": False, "ui_verified": True}

    # 1) crosswalk internal key first when net matches
    if crosswalk_pin_record:
        cw_pin = str(crosswalk_pin_record.get("edb_pin", "") or "").strip()
        if cw_pin and cw_pin not in excluded and cw_pin in comp_inst.pins:
            cw_inst = comp_inst.pins[cw_pin]
            if _net_ok(cw_inst):
                tr = truth_by_pin.get(cw_pin, {})
                ui_names = [str(x).strip() for x in (tr.get("UI_Pin_Candidates", []) or []) if str(x).strip()]
                toks = {str(x).strip() for x in (tr.get("Tokens", []) or []) if str(x).strip()}
                ui_verified = bool(spec_pin and spec_pin in ui_names) or bool(spec_tok and spec_tok in toks)
                return cw_inst, cw_pin, {"mode": "net_fix_crosswalk", "changed": True, "ui_verified": ui_verified}

    # 2) reverse map by same net in same designator, then UI verify + coord tie-break
    tx = ty = None
    if cmp_pin_record and (cmp_pin_record.get("x") is not None) and (cmp_pin_record.get("y") is not None):
        try:
            tx, ty = float(cmp_pin_record["x"]), float(cmp_pin_record["y"])
        except Exception:
            tx = ty = None

    candidates = []
    for p_key, p_inst in comp_inst.pins.items():
        if p_key in excluded:
            continue
        if not _net_ok(p_inst):
            continue
        disp = [str(x).strip() for x in _extract_pin_display_names(p_key, p_inst) if str(x).strip()]
        disp_toks = {_normalize_pin_token(x) for x in disp}
        tr = truth_by_pin.get(p_key, {})
        ui_names = [str(x).strip() for x in (tr.get("UI_Pin_Candidates", []) or []) if str(x).strip()]
        ui_toks = {str(x).strip() for x in (tr.get("Tokens", []) or []) if str(x).strip()}
        ui_hit = 0
        if spec_pin and (spec_pin in disp or spec_pin in ui_names):
            ui_hit = 2
        elif spec_tok and (spec_tok in disp_toks or spec_tok in ui_toks):
            ui_hit = 1

        d2 = 1e12
        if tx is not None and ty is not None:
            try:
                px, py = float(p_inst.position[0]), float(p_inst.position[1])
                d2 = (px - tx) ** 2 + (py - ty) ** 2
            except Exception:
                d2 = 1e12
        candidates.append((0 if ui_hit > 0 else 1, -ui_hit, d2, str(p_key), p_inst))

    if not candidates:
        return None, None, {"mode": "net_reverse_no_candidate", "changed": True, "ui_verified": False}

    candidates.sort(key=lambda x: (x[0], x[1], x[2], x[3]))
    _, ui_rank, _, best_key, best_inst = candidates[0]
    ui_verified = bool(ui_rank < 0)
    return best_inst, best_key, {"mode": "net_reverse_ui_verified" if ui_verified else "net_reverse_ui_nearest", "changed": True, "ui_verified": ui_verified}

def resolve_spec_pin_designator_primary(
    comp_inst,
    spec_pin: str,
    spec_nets,
    net_alias_map,
    ui_api_component_map: dict | None,
    crosswalk_pin_record: dict | None,
    cmp_pin_record: dict | None = None,
    siw_pin_record: dict | None = None,
    components_api=None,
    excluded_pin_names=None,
):
    """
    Designator + Pin number primary resolver.
    Priority:
      1) exact pin name in component (key/display)
      2) UI<->API crosswalk exact/token
      3) crosswalk internal key (net-validated)
      4) coordinate bridge (CMP/SIW) with strict net validation
    """
    excluded = {str(x) for x in (excluded_pin_names or set()) if str(x)}
    spec_pin = str(spec_pin or "").strip()
    spec_tok = _normalize_pin_token(spec_pin)
    if not spec_pin:
        return None, "empty_spec_pin", None, {}

    spec_nets_local = [str(n).strip() for n in (spec_nets or []) if str(n).strip()]

    def _net_ok_for_pin(pin_inst):
        if not spec_nets_local:
            return True
        p_net = str(getattr(pin_inst, "net_name", "") or "").strip()
        return any(_net_matches_spec_pin_strict(sn, p_net, net_alias_map) for sn in spec_nets_local)

    # 1) exact key/display-name in this designator
    if spec_pin in comp_inst.pins and spec_pin not in excluded:
        p = comp_inst.pins[spec_pin]
        if _net_ok_for_pin(p):
            return p, "designator_pin_exact", spec_pin, {"source": "component_key"}
    for p_key, p_inst in (getattr(comp_inst, "pins", {}) or {}).items():
        if p_key in excluded:
            continue
        disp = [str(x).strip() for x in _extract_pin_display_names(p_key, p_inst) if str(x).strip()]
        if spec_pin in disp and _net_ok_for_pin(p_inst):
            return p_inst, "designator_pin_display", p_key, {"source": "component_display"}

    # 2) dedicated UI<->API crosswalk
    ui_map = ui_api_component_map or {}
    by_ui = (ui_map.get("by_ui", {}) or {}) if isinstance(ui_map, dict) else {}
    by_tok = (ui_map.get("by_token", {}) or {}) if isinstance(ui_map, dict) else {}
    ui_recs = list(by_ui.get(spec_pin, []) or [])
    tok_recs = list(by_tok.get(spec_tok, []) or []) if spec_tok else []
    for rec in (ui_recs + tok_recs):
        edb_pin = str(rec.get("edb_pin", "")).strip()
        if not edb_pin or edb_pin in excluded or edb_pin not in comp_inst.pins:
            continue
        p_inst = comp_inst.pins[edb_pin]
        if _net_ok_for_pin(p_inst):
            src = str(rec.get("source", "") or "ui_api")
            mode = "designator_pin_ui_api_exact" if rec in ui_recs else "designator_pin_ui_api_token"
            return p_inst, mode, edb_pin, {"source": src}

    # 3) component pin crosswalk internal key
    if crosswalk_pin_record:
        edb_pin = str(crosswalk_pin_record.get("edb_pin", "")).strip()
        if edb_pin and edb_pin not in excluded and edb_pin in comp_inst.pins:
            p_inst = comp_inst.pins[edb_pin]
            if _net_ok_for_pin(p_inst):
                return p_inst, "designator_pin_crosswalk", edb_pin, {"source": str(crosswalk_pin_record.get("method", ""))}

    # 4) .NET raw pin + SIW/CMP coordinate bridge
    #    (bypass wrapper cache mismatch and use physical proximity)
    tx = ty = None
    coord_src = ""
    for src_name, rec in (("siw", siw_pin_record or {}), ("cmp", cmp_pin_record or {})):
        if rec and (rec.get("x") is not None) and (rec.get("y") is not None):
            try:
                tx, ty = float(rec["x"]), float(rec["y"])
                coord_src = src_name
                break
            except Exception:
                pass
    if tx is not None and ty is not None:
        raw_comp = getattr(comp_inst, "_edb_object", None) or getattr(comp_inst, "edbcomponent", None)
        dotnet_rows = []
        if raw_comp is not None:
            try:
                for dp in _iter_dotnet_pins_from_component(raw_comp):
                    toks = _collect_dotnet_pin_tokens(dp, components_api=components_api)
                    dnet = _dotnet_pin_net_name(dp)
                    dpos = _dotnet_pin_position(dp)
                    if not dpos:
                        continue
                    d2 = (float(dpos[0]) - tx) ** 2 + (float(dpos[1]) - ty) ** 2
                    name_hit = 1 if (spec_tok and spec_tok in toks) else 0
                    strict_net_hit = 0
                    soft_net_hit = 0
                    if spec_nets_local:
                        strict_net_hit = 1 if any(_net_matches_spec_pin_strict(sn, dnet, net_alias_map) for sn in spec_nets_local) else 0
                        if not strict_net_hit:
                            try:
                                dv = extract_voltage(dnet)
                            except Exception:
                                dv = None
                            for sn in spec_nets_local:
                                try:
                                    sv = extract_voltage(sn)
                                except Exception:
                                    sv = None
                                if sv is not None and dv is not None and abs(float(sv) - float(dv)) < 1e-6 and (not _is_signal_like_net(dnet)):
                                    soft_net_hit = 1
                                    break
                    dotnet_rows.append((name_hit, strict_net_hit, soft_net_hit, d2, toks, dnet, dpos))
            except Exception:
                dotnet_rows = []

        if dotnet_rows:
            # Map selected dotnet candidate back to component pin key
            wrapper_rows = []
            for p_key, p_inst in (getattr(comp_inst, "pins", {}) or {}).items():
                if p_key in excluded:
                    continue
                wtoks = {_normalize_pin_token(x) for x in _extract_pin_display_names(p_key, p_inst) if str(x).strip()}
                try:
                    wx, wy = float(p_inst.position[0]), float(p_inst.position[1])
                except Exception:
                    wx = wy = None
                wrapper_rows.append((p_key, p_inst, wtoks, wx, wy, str(getattr(p_inst, "net_name", "") or "").strip()))

            ranked_dn = sorted(
                dotnet_rows,
                key=lambda x: (
                    0 if x[0] else 1,            # name hit first
                    0 if x[1] else 1,            # strict net
                    0 if x[2] else 1,            # soft net(only when strict missing)
                    x[3],                        # nearest coord
                ),
            )
            for name_hit, strict_hit, soft_hit, d2, dtoks, dnet, dpos in ranked_dn:
                best_wrap = None
                for p_key, p_inst, wtoks, wx, wy, wnet in wrapper_rows:
                    tok_overlap = len(set(dtoks).intersection(set(wtoks)))
                    if tok_overlap <= 0 and (wx is None or wy is None):
                        continue
                    wd2 = 1e12
                    if wx is not None and wy is not None:
                        wd2 = (float(wx) - float(dpos[0])) ** 2 + (float(wy) - float(dpos[1])) ** 2
                    net_guard = True
                    if spec_nets_local:
                        net_guard = any(_net_matches_spec_pin_strict(sn, wnet, net_alias_map) for sn in spec_nets_local)
                        if not net_guard and soft_hit:
                            try:
                                wv = extract_voltage(wnet)
                            except Exception:
                                wv = None
                            for sn in spec_nets_local:
                                try:
                                    sv = extract_voltage(sn)
                                except Exception:
                                    sv = None
                                if sv is not None and wv is not None and abs(float(sv) - float(wv)) < 1e-6 and (not _is_signal_like_net(wnet)):
                                    net_guard = True
                                    break
                    if not net_guard:
                        continue
                    cand = ((0 if tok_overlap > 0 else 1), wd2, str(p_key), p_inst)
                    if best_wrap is None or cand < best_wrap:
                        best_wrap = cand
                if best_wrap is not None:
                    _, wd2, p_key, p_inst = best_wrap
                    mode = "designator_pin_dotnet_siw_name_net" if (name_hit and strict_hit) else \
                           "designator_pin_dotnet_siw_name_softnet" if (name_hit and soft_hit) else \
                           "designator_pin_dotnet_siw_coord_net" if strict_hit else \
                           "designator_pin_dotnet_siw_coord_softnet"
                    return p_inst, mode, p_key, {
                        "source": f"dotnet+{coord_src}",
                        "d2_siw": d2,
                        "d2_wrap": wd2,
                        "dotnet_net": dnet,
                    }

    # 5) coordinate bridge (CMP/SIW) on wrapper pins
    tx = ty = None
    coord_src = ""
    for src_name, rec in (("cmp", cmp_pin_record or {}), ("siw", siw_pin_record or {})):
        if rec and (rec.get("x") is not None) and (rec.get("y") is not None):
            try:
                tx, ty = float(rec["x"]), float(rec["y"])
                coord_src = src_name
                break
            except Exception:
                pass
    if tx is not None and ty is not None:
        cands = []
        for p_key, p_inst in (getattr(comp_inst, "pins", {}) or {}).items():
            if p_key in excluded:
                continue
            if not _net_ok_for_pin(p_inst):
                continue
            try:
                px, py = float(p_inst.position[0]), float(p_inst.position[1])
                d2 = (px - tx) ** 2 + (py - ty) ** 2
            except Exception:
                d2 = 1e12
            cands.append((d2, str(p_key), p_inst))
        if cands:
            cands.sort(key=lambda x: (x[0], x[1]))
            d2, p_key, p_inst = cands[0]
            return p_inst, f"designator_pin_coord_{coord_src}", p_key, {"source": coord_src, "d2": d2}

    return None, "designator_pin_unresolved", None, {}

def configure_ports_and_vrms_from_spec(app, cases, gnd_net, bulk_inductor_set, output_dir, logger, vrm_setup_conf=None, port_app=None):
    return vrm_setup.configure_ports_and_vrms_from_spec(
        app=app,
        cases=cases,
        gnd_net=gnd_net,
        bulk_inductor_set=bulk_inductor_set,
        output_dir=output_dir,
        logger=logger,
        vrm_setup_conf=vrm_setup_conf,
        port_app=port_app,
    )

def resolve_siwave_executable(aedt_version: str):
    clean_version = aedt_version.replace('20', '', 1).replace('.', '')
    env_var = f"ANSYSEM_ROOT{clean_version}"
    install_dir_str = os.environ.get(env_var)
    if not install_dir_str:
        raise PDNSessionException(ErrorCode.SIWAVE_EXECUTABLE_NOT_FOUND, f"Environment variable {env_var} not found.")
    aedt_install_dir = Path(install_dir_str)
    siw_execute_file = aedt_install_dir / 'siwave_ng.exe'
    if not siw_execute_file.exists():
        raise PDNSessionException(ErrorCode.SIWAVE_EXECUTABLE_NOT_FOUND, siw_execute_file)
    return siw_execute_file

def apply_drop_metrics(case, drop_voltage: float):
    v_mag = float(case.get('Vmag', 0.0) or 0.0)
    case['Result'] = round(drop_voltage, 3)
    case['Drop Voltage'] = round(v_mag - drop_voltage, 3)
    if abs(v_mag) < 1e-12: case['Drop Rate'] = 0.0
    else: case['Drop Rate'] = round((case['Drop Voltage'] / v_mag) * 100, 3)
    min_v = float(case['MinSpec'])
    max_v = float(case['MaxSpec'])
    case['Pass/Fail'] = 'Pass' if min_v < drop_voltage < max_v else 'Fail'

def build_preprocessing_record(case, idx, net_siw_file, net_edb_dir, v_port_name, i_port_name, gnd_net, solver_backend="siwave"):
    target_net_display = case.get("Display_Net", case.get("Spec_Net", case.get("Net", "")))
    return {
        "Schema_Version": 3,
        "Case_Index": idx + 1,
        "IC_Designator": case.get('IC', ''),
        "Spec_Pin": case.get('Spec_Pin', ''),
        "IC_Pin": case.get('IC_pin', ''),
        "Target_Net": target_net_display,
        "PCB_Target_Net": case.get('Net', ''),
        "Spec_Target_Net": case.get('Spec_Net', ''),
        "Full_Net_Chain": case.get('Full_Net_Chain', case.get('Source_net_chain', [])),
        "Source_Component": case.get('Source_name', ''),
        "Source_Pin": case.get('Source_pin', ''),
        "Net_Chain": case.get('Source_net_chain', []),
        "Voltage_V": case.get('Vmag', 0.0),
        "Current_A": case.get('Imag', 0.0),
        "Min_Spec_V": case.get('MinSpec', 0.0),
        "Max_Spec_V": case.get('MaxSpec', 0.0),
        "Project_Path": str(net_siw_file.resolve()),
        "Project_File": net_siw_file.name,
        "Edb_Path": str(net_edb_dir.resolve()),
        "Edb_Folder": net_edb_dir.name,
        "Artifact_Ownership": {
            "Project_File": "Pre",
            "Edb_Folder": "Post",
        },
        "V_Port_Name": v_port_name,
        "I_Port_Name": i_port_name,
        "GND_Net": gnd_net,
        "Solver_Backend": solver_backend,
        "Mapping_Mode": case.get("Mapping_Mode", ""),
        "Mapping_Trace": case.get("Mapping_Trace", {}),
        "Mapping_Status": case.get("Mapping_Status", ""),
        "Mapping_Confidence": case.get("Mapping_Confidence", ""),
        "Mapping_Note": case.get("Mapping_Note", ""),
    }


def detect_solver_backend_from_preprocessing(output_dir: Path) -> str:
    """Detect solver backend from preprocessing_result.json for stage=post compatibility."""
    pre_file = Path(output_dir) / "preprocessing_result.json"
    if not pre_file.exists():
        return "siwave"
    try:
        with open(pre_file, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return "siwave"
    if not isinstance(data, list) or not data:
        return "siwave"
    for rec in data:
        backend = str(rec.get("Solver_Backend", "")).strip().lower()
        if backend:
            return backend
    return "siwave"


def export_aedt_cutout_post_reports(output_dir: Path, logger: Logger):
    """Generate minimal post artifacts for AEDT-cutout backend without SIwave result dependency."""
    pre_file = output_dir / "preprocessing_result.json"
    cut_file = output_dir / "aedt_cutout_result.json"
    if not pre_file.exists() or not cut_file.exists():
        raise FileNotFoundError(f"Missing required files for AEDT post: {pre_file}, {cut_file}")

    with open(pre_file, "r", encoding="utf-8") as f:
        pre = json.load(f)
    with open(cut_file, "r", encoding="utf-8") as f:
        cut = json.load(f)

    by_case = {int(r.get("Case_Index")): r for r in (cut.get("Records", []) if isinstance(cut, dict) else [])}
    summary = []
    for rec in pre if isinstance(pre, list) else []:
        idx = int(rec.get("Case_Index", 0) or 0)
        cr = by_case.get(idx, {})
        done = str(cr.get("Status", "")).lower() == "done"
        item = {
            "IC": rec.get("IC_Designator", ""),
            "IC_pin": rec.get("IC_Pin", ""),
            "Net": rec.get("Target_Net", ""),
            "Source_name": rec.get("Source_Component", ""),
            "Source_pin": rec.get("Source_Pin", ""),
            "Source_net": rec.get("Net_Chain", []),
            "Full_Net_Chain": rec.get("Full_Net_Chain", []),
            "is_done": done,
            "Status": "Complete" if done else "Error",
            "Backend": "aedt_cutout",
            "Aedt_Project": cr.get("Aedt_Project", ""),
            "Cutout_Edb": cr.get("Cutout_Edb", ""),
            "Message": cr.get("Reason", "OK" if done else "Unknown"),
            "Impedance_Plot": cr.get("Impedance_Plot", ""),
            "Impedance_CSV": cr.get("Impedance_CSV", ""),
            "Touchstone": cr.get("Touchstone", ""),
            "FitView": cr.get("FitView", ""),
            "ZoomView": cr.get("ZoomView", ""),
        }
        summary.append(item)

    now = time.strftime('%Y.%m.%d, %H:%M:%S')
    result_payload = {
        "simSchedule": {"startDate": now, "endData": now},
        "summary": summary,
        "backend": "aedt_cutout",
    }
    result_detail_payload = {
        "result": summary,
        "changeHistory": [],
        "postInfo": {
            "resultBasis": "aedt_cutout_result.json",
            "viewerBasis": "none",
            "viewerReflectsLocalSettings": False,
            "artifactOwnership": {
                "preprocessing_result.json": "Pre",
                "aedt_cutout_result.json": "Solve",
                "viewer": "N/A",
            },
            "viewerArtifacts": [],
        },
    }
    setting_payload = {
        "tool": {"comp": "ANSYS", "name": "AEDT-HFSS3DLayout", "version": ""},
        "setting": summary,
        "backend": "aedt_cutout",
    }
    for name, payload in (
        ("result.json", result_payload),
        ("result_detail.json", result_detail_payload),
        ("setting.json", setting_payload),
    ):
        with open(output_dir / name, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
    logger.log(f"[POST][AEDT] Exported minimal post reports for backend=aedt_cutout at {output_dir}", level=LogLevel.INFO)

def run_standalone_post(conf_manager, input_json, output_dir, analysis_start=None, analysis_end=None):
    settings_manager = SettingsManager(input_json, configuration=conf_manager, logger=logger)
    state = reconstruct_post_state(output_dir)
    post_settings = prepare_post_settings(settings_manager.data)

    post_processor = PostProcessing(
        conf_manager.data,
        post_settings,
        output_dir,
        state.summary,
        state.gnd_net,
        logger,
    )
    post_processor.set_PDN_results(
        analysis_start or state.analysis_start,
        analysis_end or state.analysis_end,
    )
    state.viewer_artifacts = post_processor.extract_results(conf_manager.data['PDN']['version'])
    append_post_detail(output_dir, state)
    failed_viewers = [
        artifact for artifact in state.viewer_artifacts
        if artifact.get("Edb_Status") == "Error" or artifact.get("Viewer_Status") == "Error"
    ]
    if failed_viewers:
        failed_cases = ", ".join(str(item.get("Case_Index")) for item in failed_viewers)
        raise PostStageError(f"Post AEDB/Viewer generation failed for case(s): {failed_cases}")
    return state

def run_pdn_unified(
    cases, model_name, output_dir, ref_siwave_file_path, ref_edb_path, gnd_net,
    aedt_version, case_data_app, signal_layers, conf_data, siw_execute_file, exec_file,
    bulk_inductor_list=None, run_solve=True,
):
    preprocessing_data = []
    full_siw_file = (output_dir / f"{model_name}_PDN_FULL.siw").resolve()
    case_app = None
    case_runtime = []
    created_voltage_sources = 0
    created_current_sources = 0
    created_circuit_ports = 0
    skipped_no_source = 0
    skipped_connection_fail = 0
    is_syz_exec = False

    def _safe_name(text, extra=('_',)):
        return "".join(c for c in str(text or "") if c.isalnum() or c in extra).strip()

    def _case_name_net(case_row):
        return str(
            case_row.get("Display_Net", case_row.get("Spec_Net", case_row.get("Net", "")))
        )

    try:
        exec_text = Path(exec_file).read_text(encoding="utf-8", errors="ignore")
        is_syz_exec = "ExecSyzSim" in exec_text
    except Exception:
        is_syz_exec = False

    # SYZ flow: preserve preconfigured ports from Step5 and avoid re-save mutation in unified stage.
    if is_syz_exec:
        exclude_tokens = conf_data.get("PDN", {}).get("dcShort", {}).get("excludeNet", [])
        analysis_nets = collect_analysis_nets(cases, gnd_net, exclude_tokens=exclude_tokens)
        for idx, case in enumerate(cases):
            original_net_name = case.get('Net', '')
            ic_designator = case.get('IC', '')
            full_net_chain = case.get('Full_Net_Chain', case.get('Source_net_chain', []) + [original_net_name])
            best_net_name = _case_name_net(case) or SIwave.get_representative_net_name(full_net_chain)
            safe_net_port = _safe_name(best_net_name, ('_',))
            safe_ic = _safe_name(ic_designator, ('_',))
            v_port_name = f"V_{safe_ic}_{safe_net_port}"
            i_port_name = f"I_{safe_ic}_{safe_net_port}"
            preprocessing_data.append(
                build_preprocessing_record(
                    case=case, idx=idx, net_siw_file=full_siw_file, net_edb_dir=ref_edb_path,
                    v_port_name=v_port_name, i_port_name=i_port_name, gnd_net=gnd_net,
                )
            )

        try:
            if Path(ref_siwave_file_path).resolve() != full_siw_file.resolve():
                shutil.copy2(ref_siwave_file_path, full_siw_file)
            logger.log(f"[UNIFIED][SYZ] Target SIW path for injection/save: {full_siw_file}", level=LogLevel.INFO)
            injected = apply_selected_nets_to_siw_file(full_siw_file, analysis_nets, logger)
            relax_siw_plane_filters_for_pdn(full_siw_file, conf_data, logger)
            verify = verify_selected_nets_block_in_siw(full_siw_file)
            file_size = full_siw_file.stat().st_size if full_siw_file.exists() else 0
            logger.log(
                f"[NET] SIW verification after injection: injected={injected}, verify={verify}, file_size={file_size}",
                level=LogLevel.INFO,
            )
            if not injected or not verify.get("ok"):
                raise PDNSessionException(
                    ErrorCode.PDN_COMMAND_SIMULATION_FAIL,
                    f"Selected-net injection invalid for SYZ solve. verify={verify}",
                )
            logger.log(
                f"[UNIFIED][SYZ] Reuse preconfigured SIW for solve: {full_siw_file}",
                level=LogLevel.INFO,
            )
        except Exception as e:
            raise PDNSessionException(ErrorCode.PDN_COMMAND_SIMULATION_FAIL, f"Failed to stage SYZ SIW: {e}")

        if run_solve:
            logger.log("[UNIFIED] Running full-project PDN solve", level=LogLevel.INFO)
            cmd_variants = [
                [str(siw_execute_file), str(full_siw_file), str(exec_file), '-formatOutput', '-useSubdir'],
                [str(siw_execute_file), str(full_siw_file), str(exec_file), '-useSubdir'],
                [str(siw_execute_file), str(full_siw_file), str(exec_file)],
            ]
            result = None
            success = False
            for idx, cmd in enumerate(cmd_variants, start=1):
                logger.log(f"[UNIFIED] Solve attempt {idx}/{len(cmd_variants)}: {' '.join(cmd)}", level=LogLevel.DETAIL1)
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode == 0:
                    success = True
                    break
                logger.log(f"[UNIFIED][WARNING] Solve attempt {idx} failed with rc={result.returncode}", level=LogLevel.WARNING)
                if (result.stdout or "").strip():
                    logger.log(f"[UNIFIED][stdout][attempt {idx}]\n{result.stdout.strip()}", level=LogLevel.WARNING)
                if (result.stderr or "").strip():
                    logger.log(f"[UNIFIED][stderr][attempt {idx}]\n{result.stderr.strip()}", level=LogLevel.WARNING)

            if not success:
                logger.log(f"[UNIFIED][ERROR] SIW: {full_siw_file}", level=LogLevel.ERROR)
                logger.log(f"[UNIFIED][ERROR] EXEC: {exec_file} (exists={exec_file.exists()})", level=LogLevel.ERROR)
                raise PDNSessionException(ErrorCode.PDN_COMMAND_SIMULATION_FAIL, result.returncode if result else -1)

        return preprocessing_data

    try:
        case_app = SIwave(version=aedt_version, logger=logger)
        case_app.open_project(str(ref_siwave_file_path))

        for idx, case in enumerate(cases):
            original_net_name = case.get('Net', '')
            ic_designator = case.get('IC', '')
            src_comp_name = case.get('Source_name', '')
            full_net_chain = case.get('Full_Net_Chain', case.get('Source_net_chain', []) + [original_net_name])
            best_net_name = _case_name_net(case) or SIwave.get_representative_net_name(full_net_chain)
            safe_net_port = _safe_name(best_net_name, ('_',))
            safe_ic = _safe_name(ic_designator, ('_',))
            v_port_name = f"V_{safe_ic}_{safe_net_port}"
            i_port_name = f"I_{safe_ic}_{safe_net_port}"

            record = build_preprocessing_record(
                case=case, idx=idx, net_siw_file=full_siw_file, net_edb_dir=ref_edb_path,
                v_port_name=v_port_name, i_port_name=i_port_name, gnd_net=gnd_net,
            )
            preprocessing_data.append(record)
            case_runtime.append({"case": case, "record": record, "v_port": v_port_name, "i_port": i_port_name})

            if not src_comp_name:
                set_case_error_defaults(case)
                skipped_no_source += 1
                logger.log(
                    f"[UNIFIED][SKIP] Missing source component for case: IC={ic_designator}, Net={original_net_name}",
                    level=LogLevel.WARNING,
                )
                continue

            try:
                inductor_prefix = conf_data.get('PDN', {}).get('inductorPrefix', 'L')
                pos_coord, pos_layer, neg_coord, neg_layer, src_name = case_data_app.prepare_vrm_connection(
                    target_net=original_net_name, source_name=src_comp_name, source_pin=case.get('Source_pin'),
                    gnd_net=gnd_net, net_chain=full_net_chain, inductor_prefix=inductor_prefix, bulk_inductor_list=bulk_inductor_list,
                )
                if pos_coord is None or neg_coord is None:
                    set_case_error_defaults(case)
                    skipped_connection_fail += 1
                    logger.log(
                        f"[UNIFIED][SKIP] VRM connection not resolved: IC={ic_designator}, Net={original_net_name}, Source={src_comp_name}",
                        level=LogLevel.WARNING,
                    )
                    continue

                if src_name and "Inductor_" in src_name:
                    inductor_refdes = src_name.split("Inductor_")[-1]
                    try: case_app.delete_circuit_element(inductor_refdes)
                    except Exception: pass

                if is_syz_exec:
                    z_port_name = f"PORT_{safe_ic}_{safe_net_port}"
                    port_ok = case_app.place_circuit_port(
                        port_name=z_port_name,
                        pos_node=pos_coord,
                        pos_layer=pos_layer,
                        neg_node=neg_coord,
                        neg_layer=neg_layer,
                        impedance=0.1,
                    )
                    if port_ok:
                        created_circuit_ports += 1
                    else:
                        logger.log(
                            f"[UNIFIED][WARNING] Failed to place SYZ circuit port: {z_port_name}",
                            level=LogLevel.WARNING,
                        )

                v_mag, i_mag = case.get('Vmag', 0.0), case.get('Imag', 0.0)
                case_app.place_voltage_source(
                    v_port_name, pos_coord, pos_layer, neg_coord, neg_layer,
                    conf_data['PDN']['setup']['Vsource_Res'], v_mag
                )
                created_voltage_sources += 1

                ic_inst = case_data_app.edb._components.components.get(ic_designator)
                if ic_inst:
                    ic_layer = ic_inst.placement_layer
                    ic_pin_name = case.get('IC_pin')
                    if ic_pin_name and ic_pin_name in ic_inst.pins:
                        cur_pos = ic_inst.pins[ic_pin_name].position
                        cur_neg, cur_neg_layer = case_data_app.find_nearest_gnd(cur_pos, gnd_net)
                        if not cur_neg:
                            cur_neg = cur_pos
                            target_layer_index = signal_layers.index(ic_layer)
                            if target_layer_index == 0: cur_neg_layer = signal_layers[1]
                            elif target_layer_index == len(signal_layers) - 1: cur_neg_layer = signal_layers[-2]
                            else: cur_neg_layer = signal_layers[target_layer_index + 1]
                        case_app.place_current_source(
                            i_port_name, cur_pos, ic_layer, cur_neg, cur_neg_layer,
                            conf_data['PDN']['setup']['Isource_Res'], i_mag
                        )
                        created_current_sources += 1
                case['is_done'] = True
            except Exception:
                set_case_error_defaults(case)

        if conf_data['PDN'].get('doValchk', False):
            case_app.oproject.ScrRunValidationCheck()
        if not is_syz_exec:
            case_app.oproject.ScrSetSimulationName('dc', f'PDN - {model_name} - FULL')
        logger.log(
            f"[UNIFIED] Source placement summary: Vsrc={created_voltage_sources}, Isrc={created_current_sources}, Zport={created_circuit_ports}, "
            f"skip_no_source={skipped_no_source}, skip_connection={skipped_connection_fail}",
            level=LogLevel.INFO,
        )
        if is_syz_exec and created_circuit_ports == 0:
            logger.log(
                "[UNIFIED][WARNING] No SYZ circuit port created in unified flow.",
                level=LogLevel.WARNING,
            )
        if created_voltage_sources == 0:
            logger.log(
                "[UNIFIED][INFO] No voltage source created. Continue because current flow is PDN Z setup (source hard requirement disabled).",
                level=LogLevel.WARNING,
            )
        case_app.save_project_as(full_siw_file)

        if run_solve:
            logger.log("[UNIFIED] Running full-project PDN solve", level=LogLevel.INFO)
            cmd_variants = [
                [str(siw_execute_file), str(full_siw_file), str(exec_file), '-formatOutput', '-useSubdir'],
                [str(siw_execute_file), str(full_siw_file), str(exec_file), '-useSubdir'],
                [str(siw_execute_file), str(full_siw_file), str(exec_file)],
            ]
            result = None
            success = False
            for idx, cmd in enumerate(cmd_variants, start=1):
                logger.log(f"[UNIFIED] Solve attempt {idx}/{len(cmd_variants)}: {' '.join(cmd)}", level=LogLevel.DETAIL1)
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode == 0:
                    success = True
                    break
                logger.log(f"[UNIFIED][WARNING] Solve attempt {idx} failed with rc={result.returncode}", level=LogLevel.WARNING)
                if (result.stdout or "").strip():
                    logger.log(f"[UNIFIED][stdout][attempt {idx}]\n{result.stdout.strip()}", level=LogLevel.WARNING)
                if (result.stderr or "").strip():
                    logger.log(f"[UNIFIED][stderr][attempt {idx}]\n{result.stderr.strip()}", level=LogLevel.WARNING)

            if not success:
                logger.log(f"[UNIFIED][ERROR] SIW: {full_siw_file}", level=LogLevel.ERROR)
                logger.log(f"[UNIFIED][ERROR] EXEC: {exec_file} (exists={exec_file.exists()})", level=LogLevel.ERROR)
                raise PDNSessionException(ErrorCode.PDN_COMMAND_SIMULATION_FAIL, result.returncode if result else -1)

            ced_file = full_siw_file.with_suffix('.siwaveresults') / '0000' / '0000.ced'
            if not ced_file.exists():
                raise PDNSessionException(ErrorCode.PDN_RESULT_NOT_FOUND, ced_file)

            ced_map = {}
            with open(str(ced_file), 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    cols = line.split()
                    if cols: ced_map[cols[0]] = cols

            for item in case_runtime:
                case = item["case"]
                i_port_name = item["i_port"]
                line_data = ced_map.get(i_port_name)
                if line_data and len(line_data) > 2:
                    try:
                        load_voltage = float(line_data[2])
                        apply_drop_metrics(case, load_voltage)
                        case['is_done'] = True
                    except Exception:
                        set_case_error_defaults(case)
                else:
                    set_case_error_defaults(case)

    finally:
        if case_app:
            case_app.quit_application()

    return preprocessing_data
# endregion   


def resolve_solver_backend(layer_count: int | None, conf_data: dict, logger: Logger) -> str:
    """Resolve PDN solve backend by policy and layer count."""
    policy = str(
        conf_data.get("PDN", {}).get("setup", {}).get("solver_backend_policy", "auto")
    ).strip().lower()
    if policy == "force_aedt":
        backend = "aedt_cutout"
    elif policy == "force_siwave":
        backend = "siwave"
    else:
        backend = "aedt_cutout" if (layer_count is not None and layer_count <= 2) else "siwave"
    logger.log(
        f"[BACKEND] Solver backend resolved: {backend} (policy={policy}, layer_count={layer_count})",
        level=LogLevel.INFO,
    )
    return backend


def run_pdn_aedt_cutout_solve(
    cases,
    model_name: str,
    ref_edb_path: Path,
    output_dir: Path,
    aedt_version: str,
    conf_data: dict,
    logger: Logger,
):
    """Run 2-layer PDN Z analysis through AEDT cutout flow (modular wrapper)."""
    aedt_runner = AEDT(version=aedt_version, logger=logger)
    summary = aedt_runner.run_cutout_batch(
        cases=cases,
        model_name=model_name,
        ref_edb_path=Path(ref_edb_path),
        output_dir=Path(output_dir),
        conf_data=conf_data,
    )
    if summary["Done"] == 0:
        raise PDNSessionException(ErrorCode.PDN_COMMAND_SIMULATION_FAIL, "AEDT cutout solve: no successful case")
    return summary

# region Write Headers & 0\~2. Initialize
try:
    if MODE == 2: incubator()
    START_TIME = time.strftime('%Y.%m.%d, %H:%M:%S')
    RUN_TAG = time.strftime('%Y%m%d_%H%M%S')
    username = os.environ.get("USERNAME", "Unknown") 
    logger.log("\n\n<2025> ANSYS, Inc. Unauthorized use, distribution, or duplication is prohibited", level=LogLevel.SECTION, line_change=False)
    logger.log(f"| Launched Ansys {UTILITY_NAME} v{VERSION} on {START_TIME}", level=LogLevel.SECTION, line_change=False)
except Exception: pass

try:
    logger.log(f"Step {step}. Initialize", level=LogLevel.INFO)
    parser = argparse.ArgumentParser(description=f"{UTILITY_NAME} v{VERSION}")
    parser.add_argument("json", type=str, help="Path to input json file")
    parser.add_argument("--stage", type=str, choices=["pre", "post", "full"], default="full",
                        help="'pre': generate case files before solve. 'post': rebuild reports from uploaded results. 'full': run entire flow (default)")
    cli_args = parser.parse_args()
    STAGE = cli_args.stage
    logger.log(f"Execution stage : {STAGE}", level=LogLevel.DETAIL1)
    INPUT_JSON = Path(cli_args.json).resolve()
    INPUT_DIR = INPUT_JSON.parent
    db = Database(INPUT_DIR)
    logger.log_dir = INPUT_DIR
    os.chdir(WORKING_DIR)
    ensure_admin_if_requested(logger)
    cleanup_siwave_background_processes(logger)
    OUTPUT_DIR = INPUT_DIR / 'outputs'
    PDNSessionException.OUTPUT_DIR = OUTPUT_DIR
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    step += 1
except Exception: logger.fatal(f"An error occurred while initializing: {traceback.format_exc()}")

try:
    logger.log(f"Step {step}. Get Configurations for PDN", level=LogLevel.INFO)
    CONF_FILE = WORKING_DIR / 'core' / 'config.json'
    conf_manager = SettingsManager(CONF_FILE)
    conf_manager.data['PDN'] = conf_manager.data.get('PDN', {})
    AEDT_VERSION = conf_manager.data['PDN']['version']
    log_runtime_preflight(logger, AEDT_VERSION)
    step += 1
except Exception: logger.fatal(f"An error occurred while loading configurations: {traceback.format_exc()}")

if STAGE == "post":
    try:
        logger.log(f"Step {step}. Standalone Post-processing", level=LogLevel.INFO)
        stage_post_backend = detect_solver_backend_from_preprocessing(OUTPUT_DIR)
        if stage_post_backend == "aedt_cutout":
            export_aedt_cutout_post_reports(OUTPUT_DIR, logger)
            logger.log(
                "Standalone Post completed for AEDT cutout backend.",
                level=LogLevel.DETAIL1,
            )
        else:
            post_state = run_standalone_post(conf_manager, INPUT_JSON, OUTPUT_DIR)
            complete_count = sum(1 for case in post_state.summary if case.get('is_done'))
            if complete_count == 0:
                raise PostStageError("Standalone Post failed: no completed Local result was detected")
            logger.log(f"Standalone Post completed: {complete_count}/{len(post_state.summary)} cases", level=LogLevel.DETAIL1)
        END_TIME = time.strftime('%Y.%m.%d, %H:%M:%S')
    except Exception:
        logger.fatal(f"An error occurred while performing standalone Post-processing: {traceback.format_exc()}")
        raise SystemExit(1)
    raise SystemExit(0)

try:
    logger.log(f"Step {step}. Get Settings for PDN", level=LogLevel.INFO)
    settings_manager = SettingsManager(INPUT_JSON, configuration=conf_manager, logger=logger)
    settings_manager.data.setdefault('CAE', {})
    settings_manager.data['CAE'].setdefault('PCB', {})
    settings_manager.data['CAE'].setdefault('SOC', {})

    original_spec_name = settings_manager.data.get('CAE', {}).get('SOC', {}).get('Spec', '')
    if original_spec_name:
        primary_spec_path = INPUT_DIR / original_spec_name
        if not primary_spec_path.exists():
            parts = original_spec_name.rsplit('_', 1) 
            base_name = parts[0] if len(parts) == 2 else original_spec_name.rsplit('.', 1)[0]
            fallback_spec_name = f"{base_name}_reference.csv"
            settings_manager.data['CAE']['SOC']['Spec'] = fallback_spec_name

    original_bom_name = settings_manager.data.get('CAE', {}).get('PCB', {}).get('BOM', '')
    if original_bom_name:
        primary_bom_path = INPUT_DIR / original_bom_name
        if not primary_bom_path.exists() and primary_bom_path.suffix.lower() == '.csv':
            for ext in ['.xlsx', '.xls']:
                fallback_bom_path = primary_bom_path.with_suffix(ext)
                if fallback_bom_path.exists():
                    settings_manager.data['CAE']['PCB']['BOM'] = original_bom_name.rsplit('.', 1)[0] + ext
                    break

    input_valchk = InValChk(settings_manager.data, INPUT_DIR, logger)
    default, optional = input_valchk.is_valid()
    
    # [수정] Pmap 비의존 PDN 입력만 유지
    settings_manager.data['CAE']['PCB'].update({'cadFile': default['cadFile'], 'Stackup': default['Stackup'], 'BOM': default['BOM']})
    settings_manager.data['CAE']['SOC'].update({'Spec': default['Spec'], 'Inner_cap': optional['Inner_cap']})
    step += 1
except Exception: 
    logger.fatal(f"An error occurred while loading settings: {traceback.format_exc()}")
    raise SystemExit(1)
# endregion 

# region 3. Get ECAD Data
try:
    logger.log(f"Step {step}. Get ECAD Data", level=LogLevel.INFO)
    INPUT_CAD_FILE = INPUT_DIR / settings_manager.data['CAE']['PCB']['cadFile']
    EDB_FILE_PATH = None
    CMP_FILE_PATH = None
    STACKUP_INPUT_FILE = input_valchk._default_inputFiles.get('Stackup') if input_valchk else None
    STACKUP_EFFECTIVE_FILE = resolve_stackup_for_project(input_valchk, INPUT_DIR, WORKING_DIR, logger)
    STACKUP_APPLIED_AT_PROJECT_CREATION = False
    
    if INPUT_CAD_FILE.suffix == '.zip':
        with zipfile.ZipFile(INPUT_CAD_FILE, 'r') as zip_ref: zip_ref.extractall(INPUT_CAD_FILE.parent)
        if conf_manager.data['PDN']['isZuken']:
            temp_preconv = resolve_temp_preconverted_files(INPUT_CAD_FILE, logger)
            if temp_preconv:
                DSGN_FILE = temp_preconv.get("DSGN_FILE")
                EDB_FILE_PATH = temp_preconv.get("EDB_FILE_PATH")
                if EDB_FILE_PATH and Path(EDB_FILE_PATH).exists():
                    valid_preconv, preconv_thickness = is_edb_stackup_valid_for_solve(
                        Path(EDB_FILE_PATH),
                        AEDT_VERSION,
                        logger,
                    )
                    logger.log(f"[STACKUP][CHECK] Preconverted EDB thickness map: {preconv_thickness}", level=LogLevel.DETAIL1)
                    if not valid_preconv:
                        logger.log(
                            "[STACKUP][CHECK] Preconverted EDB stackup is invalid. Force rebuild via create_project(ANF/CMP/STK).",
                            level=LogLevel.WARNING,
                        )
                        EDB_FILE_PATH = None
                        temp_preconv = None
            else:
                DSGN_FILE = None

            ZUKEN_BIN_DIR = Path(conf_manager.data['PDN']['DF_path'])
            pcb_files = list(INPUT_CAD_FILE.parent.glob('*.pcb'))
            if len(pcb_files) == 1: PCB_FILE = pcb_files[0]
            else: raise PDNSessionException(ErrorCode.INVALID_PCB_FILE_NUM, pcb_files)

            if not temp_preconv:
                CR5_EXEC = ZUKEN_BIN_DIR / 'DFevolv.cr5.exe'
                dsgn_candidate = PCB_FILE.with_suffix('.dsgn')
                if dsgn_candidate.exists():
                    DSGN_FILE = dsgn_candidate
                    logger.log(f"[PRE] Reuse existing DSGN for rebuild path: {DSGN_FILE}", level=LogLevel.DETAIL1)
                else:
                    result = run_external_tool([str(CR5_EXEC), str(PCB_FILE.parent)], "DFevolv.cr5.exe")
                    if result.returncode: raise PDNSessionException(ErrorCode.CONVERT_PCB_TO_DSGN_FAIL, result.returncode)
                    DSGN_FILE = dsgn_candidate

            if EDB_FILE_PATH is None:
                base_name_for_project = INPUT_CAD_FILE.stem.split('-')[0]
                use_create_project_flow = bool(DSGN_FILE and STACKUP_EFFECTIVE_FILE and Path(STACKUP_EFFECTIVE_FILE).exists())
                if use_create_project_flow:
                    anf_file, cmp_file = resolve_or_create_anf_cmp(Path(DSGN_FILE), ZUKEN_BIN_DIR, logger)
                    _, EDB_FILE_PATH = build_edb_via_create_project(
                        aedt_version=AEDT_VERSION,
                        anf_file=Path(anf_file),
                        cmp_file=Path(cmp_file),
                        stackup_file=Path(STACKUP_EFFECTIVE_FILE),
                        output_dir=OUTPUT_DIR,
                        base_name=base_name_for_project,
                        logger=logger,
                    )
                    STACKUP_APPLIED_AT_PROJECT_CREATION = True
                else:
                    DSGN2EDB_EXEC = ZUKEN_BIN_DIR / 'DFaedbout.exe'
                    EDB_FILE_PATH = DSGN_FILE.with_suffix('.aedb')
                    result = run_external_tool([str(DSGN2EDB_EXEC), '-r', str(DSGN_FILE), '-o', str(EDB_FILE_PATH)], "DFaedbout.exe")
                    if result.returncode:
                        raise PDNSessionException(ErrorCode.CONVERT_DSGN_TO_EDB_FAIL, result.returncode)
        else:
            EDB_FILE_PATH = INPUT_CAD_FILE.with_suffix('.aedb')
    else:
        raise PDNSessionException(ErrorCode.INVALID_CAD_FILE, INPUT_CAD_FILE)

    base_name = INPUT_CAD_FILE.stem.split('-')[0]
    CMP_FILE_PATH = discover_cmp_file(INPUT_DIR, base_name)
    NDF_FILE_PATH = discover_ndf_file(INPUT_DIR, base_name)
    if CMP_FILE_PATH:
        logger.log(f"[SPEC] CMP source selected for pin mapping: {CMP_FILE_PATH}", level=LogLevel.DETAIL1)
    else:
        logger.log("[SPEC][WARNING] CMP file not found. Spec->EDB pin coordinate fallback is disabled.", level=LogLevel.WARNING)
    if NDF_FILE_PATH:
        logger.log(f"[SPEC] NDF source selected for pin/net crosswalk: {NDF_FILE_PATH}", level=LogLevel.DETAIL1)
    else:
        logger.log("[SPEC][WARNING] NDF file not found. Spec pin/net crosswalk fallback is disabled.", level=LogLevel.WARNING)
    
    # [수정] SIwave_FILE_PATH를 outputs 폴더 하위로 지정하여 pre/full 모두 동일한 경로를 바라보도록 설정
    SIwave_FILE_PATH = OUTPUT_DIR / f'{base_name}_ready_for_solve.siw'

except Exception:
    logger.fatal(f"An error occurred while getting CAD database: {traceback.format_exc()}")
    raise PDNSessionException(ErrorCode.CAD_IMPORT_FAIL)
finally:
    step += 1
# endregion

# region 4. Modify CAD Data using EDB database
logger.log(f"Step {step}. CAD Modification using EDB database", level=LogLevel.INFO)
app = None
try:
    app = SIwave(version=AEDT_VERSION, logger=logger)
    app.set_cad_file(EDB_FILE_PATH)
    log_signal_layer_thicknesses(app, logger, tag="[STACKUP][Step4]")
    try: STACKUP_LAYER_COUNT = len(app.edb.stackup.signal_layers.keys())
    except Exception: STACKUP_LAYER_COUNT = None

    power_net_areas = {
        net_name: sum(p.area() for p in net_inst.primitives if p.type != 'Path')
        for net_name, net_inst in app.edb._nets.power.items()
        if conf_manager.data['PDN']['dcShort']['shortKey'] not in net_name and '+' not in net_name
    }
    if power_net_areas: GND_NET = max(power_net_areas, key=power_net_areas.get)
    else: raise PDNSessionException(ErrorCode.GND_NET_DETECT_FAIL)

    BOM_FILE = input_valchk._default_inputFiles['BOM']
    settings_manager.parse_bom_and_partlist(BOM_FILE)
    bom_info = settings_manager.get_bom() 

    delComp, missing_in_bom = {}, []
    exclude_prefixes = tuple(conf_manager.data['PDN']['dcShort'].get('excludePrefixes', ['AR', 'JK', 'P', 'IC', 'X', 'D']))
    delete_types = conf_manager.data['PDN']['dcShort'].get('deleteCompTypes', ['IC', 'IO', 'Other'])

    for comp_name, comp_inst in app.edb._components.components.items():
        if str(comp_name).upper().startswith('C'): continue
        if comp_name in bom_info['Designators']: continue
        elif not comp_name.startswith(exclude_prefixes) and comp_inst.component_def in conf_manager.data['PDN']['dcShort']['shortedComp']: continue
        else:
            if comp_inst.type in delete_types:
                delComp[comp_name] = comp_inst
                missing_in_bom.append(comp_name) 
            else: comp_inst.enabled = False

    SHORT_CORRECTION, DEL_COMP = {}, set()
    for comp_name, comp_inst in app.edb._components.components.items():
        if comp_name.startswith(exclude_prefixes) or comp_inst.component_def not in conf_manager.data['PDN']['dcShort']['shortedComp']: continue
        target_nets = app.edb.nets.nets_by_components[comp_name]
        if len(target_nets) != 2: continue
        net1, net2 = target_nets
        short_key = conf_manager.data['PDN']['dcShort']['shortKey']
        primary, secondary = (net2, net1) if short_key in net1 or (short_key not in net2 and len(net1) > len(net2)) else (net1, net2)
        SHORT_CORRECTION.setdefault(primary, []).append(secondary)
        DEL_COMP.add(comp_name)

    SPEC_FILE = input_valchk._default_inputFiles['Spec']
    settings_manager.parse_spec(SPEC_FILE)
    spec_info = settings_manager.get_spec()
    logger.log(f"[SPEC] Parsed rows: {len(spec_info)}", level=LogLevel.DETAIL1)
    target_designators = {str(row.get("Designator", "")).strip() for row in spec_info if row.get("Designator")}
    cmp_pin_records = parse_cmp_pin_records(CMP_FILE_PATH, target_designators=target_designators)
    ndf_pin_records = parse_ndf_pin_records(NDF_FILE_PATH, target_designators=target_designators)
    siw_crosswalk_source = discover_siw_for_ui_crosswalk(OUTPUT_DIR, INPUT_DIR, base_name)
    siw_pin_records = parse_siw_pin_records(siw_crosswalk_source, target_designators=target_designators)
    if siw_crosswalk_source:
        logger.log(f"[SPEC] SIW source selected for UI/API crosswalk: {siw_crosswalk_source}", level=LogLevel.DETAIL1)
    else:
        logger.log("[SPEC][WARNING] SIW source not found. SIW-assisted UI/API crosswalk is disabled.", level=LogLevel.WARNING)
    pin_crosswalk_cache = {}
    edb_truth_cache = {}
    ui_api_crosswalk_cache = {}
    pin_override_map, pin_override_path = _load_pin_overrides(INPUT_DIR)
    if pin_override_path:
        logger.log(
            f"[SPEC] Pin override table loaded: {pin_override_path} (rows={len(pin_override_map)})",
            level=LogLevel.DETAIL1,
        )
    else:
        logger.log("[SPEC] Pin override table not found. Continue without manual overrides.", level=LogLevel.DETAIL2)
    
    pdn_cases_info = []
    inner_cap_audit = []
    inner_cap_net_lookup = {}
    designator_list = {case['Designator'] for case in spec_info}
    
    time.sleep(3.0)

    def normalize_name(name): return re.sub(r'[^A-Za-z0-9]', '', str(name)).upper()
    def normalize_pin_name(pin_name): return re.sub(r'[^A-Za-z0-9]', '', str(pin_name or "")).upper()

    DEFAULT_NET_ALIASES = {"+1.8V": ["EMMC1V8", "VCC_1V8", "+1V8"], "PIF1V5": ["+VTERM", "VCC_1V5", "SIGN003116"]}

    def build_net_alias_map(short_correction):
        alias = {}
        for primary, secondaries in (short_correction or {}).items():
            all_nets = [str(primary)] + [str(s) for s in secondaries]
            for n in all_nets: alias.setdefault(n, set()).update(x for x in all_nets if x != n)
        for spec_net, edb_nets in DEFAULT_NET_ALIASES.items():
            alias.setdefault(spec_net, set()).update(edb_nets)
            for edb_net in edb_nets: alias.setdefault(edb_net, set()).add(spec_net)
        return alias

    net_alias_map = build_net_alias_map(SHORT_CORRECTION)
    normalized_edb_component_names = {normalize_name(c_name): c_name for c_name in app.edb._components.components.keys()}

    def get_component_by_normalized(norm_name):
        comp_name = normalized_edb_component_names.get(norm_name)
        return app.edb._components.components.get(comp_name) if comp_name else None

    INNER_CAP_FILE = settings_manager.data.get('CAE', {}).get('SOC', {}).get('Inner_cap')
    if INNER_CAP_FILE:
        inner_cap_path = INPUT_DIR / INNER_CAP_FILE
        if settings_manager.parse_inner_cap(inner_cap_path):
            inner_caps = settings_manager.get_inner_cap()
            for icap_item in inner_caps:
                lk = (normalize_name(icap_item.get('Designator', '')), normalize_pin_name(icap_item.get('Pin_Number', '')))
                if lk not in inner_cap_net_lookup:
                    inner_cap_net_lookup[lk] = {"PCB_Net": (icap_item.get('PCB_Net') or "").strip(), "SoC_Net": (icap_item.get('SoC_Net') or "").strip()}
            
            cap_name_counter = {}
            for idx, icap in enumerate(inner_caps):
                ic_refdes = icap['Designator']
                pin_no = icap['Pin_Number']
                cap_val = icap['Cap_Value']
                base_cap_name = f"C_{sanitize_name_token(ic_refdes)}_{sanitize_name_token(pin_no)}"
                seq = cap_name_counter.get(base_cap_name, 0) + 1
                cap_name_counter[base_cap_name] = seq
                cap_name = base_cap_name if seq == 1 else f"{base_cap_name}_Q{seq}"
                
                audit_item = {
                    "index": idx + 1, "component_name": cap_name, "designator": ic_refdes,
                    "pin_number": pin_no, "cap_value": cap_val, "status": "pending", "message": "",
                }
                
                norm_ic_name = normalize_name(ic_refdes)
                ic_inst = get_component_by_normalized(norm_ic_name)
                if not ic_inst: continue
                
                pin_inst = ic_inst.pins.get(pin_no)
                if not pin_inst: continue
                    
                pin_loc = pin_inst.position
                
                try:
                    gnd_pin_inst = find_nearest_gnd_pin(app.edb, pin_loc, GND_NET)
                    if not gnd_pin_inst: continue

                    created = app.create_rlc_component(
                        pins=[pin_inst, gnd_pin_inst], comp_name=cap_name,
                        part_name=icap.get('Part_Number', 'INNER_CAP'), r_value=1e9,
                    )
                    if created:
                        new_comp = app.edb._components.components.get(cap_name)
                        if new_comp:
                            new_comp.type = "Capacitor"
                            new_comp.value = cap_val
                            audit_item["status"] = "created"
                    inner_cap_audit.append(audit_item)
                except Exception as e:
                    logger.log(f"[ERROR] Inner Cap {cap_name} 생성 중 오류 발생: {e}", level=LogLevel.ERROR)

    spec_skip_missing_comp = 0
    spec_skip_missing_pin = 0
    spec_fallback_resolved = 0
    strict_refdes_pin = bool(
        conf_manager.data.get("PDN", {}).get("pinMapping", {}).get("strictRefdesPin", True)
    )
    pin_resolution_stats = defaultdict(int)
    pin_mapping_quality_stats = defaultdict(int)
    pin_mapping_records = []
    used_component_pins = defaultdict(set)
    logger.log(
        f"[SPEC] Pin matching policy: strict_refdes_pin={strict_refdes_pin}",
        level=LogLevel.INFO,
    )

    def add_pin_mapping_record(
        case_row,
        resolved_comp,
        spec_pin,
        resolved_pin,
        mode,
        spec_nets_row,
        resolved_net,
        trace_meta=None,
    ):
        spec_net_preferred = ""
        try:
            spec_net_preferred = str((spec_nets_row or [""])[0] or "").strip()
        except Exception:
            spec_net_preferred = ""
        trace_meta = trace_meta or {}
        mapping_status, mapping_confidence, mapping_note = evaluate_pin_mapping_quality(mode, trace_meta)
        pin_mapping_quality_stats[mapping_status] += 1
        pin_resolution_stats[mode] += 1
        pin_mapping_records.append(
            {
                "Spec_Designator": case_row.get("Designator"),
                "Resolved_Designator": resolved_comp,
                "Spec_Pin": spec_pin,
                "Resolved_Pin": resolved_pin,
                "Resolve_Mode": mode,
                "Spec_Nets": spec_nets_row,
                # Report-level naming policy: prefer Spec net label for readability.
                "Resolved_Net": spec_net_preferred or resolved_net,
                # Keep actual PCB-resolved net for traceability/debug.
                "Resolved_Net_PCB": resolved_net,
                "Trace_Start_Net": trace_meta.get("start_net", ""),
                "Trace_Reached_Net": trace_meta.get("reached_net", ""),
                "Trace_Hops": trace_meta.get("hops", ""),
                "Trace_Status": trace_meta.get("status", ""),
                "Trace_Reason": trace_meta.get("reason", ""),
                "Trace_Candidate_Pins": trace_meta.get("candidate_pins", []),
                "Mapping_Status": mapping_status,
                "Mapping_Confidence": mapping_confidence,
                "Mapping_Note": mapping_note,
            }
        )

    def append_case_from_target_net(
        case_row,
        resolved_comp,
        spec_pin,
        resolved_pin,
        target_net,
        spec_nets_row,
        mapping_mode="",
        trace_meta=None,
    ):
        try:
            net_obj = app.edb.nets.nets.get(target_net)
        except Exception:
            net_obj = None
        if net_obj is None:
            return False
        trace_meta = trace_meta or {}
        mapping_status, mapping_confidence, mapping_note = evaluate_pin_mapping_quality(mapping_mode, trace_meta)

        db.other_nets[target_net] = []
        result, net_chain = find_power_source(app.edb, net_obj, designator_list, bom_info, GND_NET, target_ic=resolved_comp)
        if isinstance(result, ErrorCode):
            net_chain = []

        source_pin_name = None
        if not isinstance(result, ErrorCode) and result and getattr(result, "pins", None):
            source_pin_name = next(
                (
                    p_name
                    for s_net in reversed(net_chain + [target_net])
                    for p_name, p in result.pins.items()
                    if p.net_name == s_net
                ),
                None,
            )

        full_chain = []
        for n in (net_chain + [target_net]):
            if n not in full_chain:
                full_chain.append(n)

        vmag_default = extract_voltage(target_net) or 1.0
        vmag = parse_numeric(case_row.get('Voltage_(V)'), vmag_default)
        imag = parse_numeric(case_row.get('Current_(A)'), 1.0)
        min_spec = parse_numeric(case_row.get('Min_Spec_(V)'), 0.0)
        max_spec = parse_numeric(case_row.get('Max_Spec_(V)'), max(vmag * 1.2, vmag + 0.1))
        spec_net_preferred = spec_nets_row[0] if spec_nets_row else target_net

        pdn_cases_info.append({
            'IC': resolved_comp,
            'Spec_Pin': spec_pin,
            'IC_pin': resolved_pin,
            'Net': target_net,
            'Spec_Net': spec_net_preferred,
            'Display_Net': spec_net_preferred or target_net,
            'Source_name': result.name if not isinstance(result, ErrorCode) else "",
            'Source_pin': source_pin_name,
            'Source_net_chain': net_chain,
            'Full_Net_Chain': full_chain,
            'Vmag': vmag,
            'Imag': imag,
            'MinSpec': min_spec,
            'MaxSpec': max_spec,
            'Mapping_Mode': mapping_mode,
            'Mapping_Trace': trace_meta,
            'Mapping_Status': mapping_status,
            'Mapping_Confidence': mapping_confidence,
            'Mapping_Note': mapping_note,
        })
        return True

    def trace_ic_local_power_pin(comp_inst_local, start_net, gnd_net, spec_nets=None, max_hops=10):
        """
        Trace from start_net through component connectivity and return first IC-local
        non-GND, non-signal, non-dummy pin reached.
        Also return diagnostic list of all IC-local pin hits encountered on traced nets.
        """
        start_net = str(start_net or "").strip()
        if not start_net or comp_inst_local is None:
            return "", "", -1, "invalid_input", {"visited_nets": [], "ic_hits": []}

        from collections import deque
        q = deque([(start_net, 0)])
        visited = {start_net}
        visited_order = [start_net]
        gnd_u = str(gnd_net or "").strip().upper()
        spec_nets_local = [str(n).strip() for n in (spec_nets or []) if str(n).strip()]
        ic_hits_all = []
        ic_hits_seen = set()

        while q:
            cur_net, hops = q.popleft()
            cur_u = str(cur_net or "").upper()
            cur_signal = _is_signal_like_net(cur_net)
            cur_forbidden = _is_forbidden_trace_net(cur_net)
            if cur_u == gnd_u or cur_signal or cur_forbidden:
                pass
            else:
                ic_hits = []
                for pn, p in comp_inst_local.pins.items():
                    pnet = str(getattr(p, "net_name", "") or "").strip()
                    if not pnet:
                        continue
                    if pnet == cur_net:
                        is_gnd = str(pnet).upper() == gnd_u
                        is_signal = _is_signal_like_net(pnet)
                        is_forbidden = _is_forbidden_trace_net(pnet)
                        is_power = _is_power_like_net(pnet)
                        hit_key = (str(pn), str(pnet), hops)
                        if hit_key not in ic_hits_seen:
                            ic_hits_all.append(
                                {
                                    "pin": str(pn),
                                    "net": str(pnet),
                                    "hop": hops,
                                    "is_gnd": bool(is_gnd),
                                    "is_power_like": bool(is_power),
                                    "is_signal_like": bool(is_signal),
                                    "is_forbidden": bool(is_forbidden),
                                }
                            )
                            ic_hits_seen.add(hit_key)
                        if is_gnd or is_signal or is_forbidden or (not is_power):
                            continue
                        if spec_nets_local and not any(_net_matches_spec(sn, pnet, net_alias_map) for sn in spec_nets_local):
                            continue
                        ic_hits.append((pn, pnet))
                if ic_hits:
                    ic_hits.sort(key=lambda x: str(x[0]))
                    return ic_hits[0][0], ic_hits[0][1], hops, "ok", {
                        "visited_nets": visited_order,
                        "ic_hits": ic_hits_all,
                    }

            if hops >= max_hops:
                continue

            for _, comp in app.edb._components.components.items():
                pins = list(comp.pins.values())
                if not pins:
                    continue
                # graph 폭발 방지
                if len(pins) > 24 and comp is not comp_inst_local:
                    continue
                if not any(str(getattr(p, "net_name", "") or "") == cur_net for p in pins):
                    continue
                for p in pins:
                    nxt = str(getattr(p, "net_name", "") or "").strip()
                    if not nxt or nxt in visited or nxt == cur_net:
                        continue
                    if str(nxt).upper() == gnd_u:
                        continue
                    if _is_signal_like_net(nxt) or _is_forbidden_trace_net(nxt):
                        continue
                    visited.add(nxt)
                    visited_order.append(nxt)
                    q.append((nxt, hops + 1))

        return "", "", -1, "no_ic_local_power_pin", {
            "visited_nets": visited_order,
            "ic_hits": ic_hits_all,
        }

    for case in spec_info:
        comp_name = case['Designator']
        norm_comp_name = normalize_name(comp_name)
        comp_inst = get_component_by_normalized(norm_comp_name)
        target_pin_name = str(case.get('Pin_number', '')).strip()
        spec_nets = _spec_net_candidates(case)
        if comp_inst is None:
            fallback_candidates = []
            for cand_name, cand_comp in app.edb._components.components.items():
                cand_pin = cand_comp.pins.get(target_pin_name)
                if not cand_pin:
                    continue
                if not spec_nets:
                    fallback_candidates.append((cand_name, cand_comp))
                    continue
                for spec_net in spec_nets:
                    if _net_matches_spec(spec_net, cand_pin.net_name, net_alias_map):
                        fallback_candidates.append((cand_name, cand_comp))
                        break
            if fallback_candidates:
                chosen_name, chosen_comp = fallback_candidates[0]
                comp_name = chosen_name
                comp_inst = chosen_comp
                spec_fallback_resolved += 1
                logger.log(
                    f"[SPEC][FALLBACK] Designator remapped: {case.get('Designator')} -> {comp_name}, pin={target_pin_name}, nets={spec_nets}",
                    level=LogLevel.WARNING,
                )
            else:
                spec_skip_missing_comp += 1
                logger.log(
                    f"[SPEC][SKIP] Component not found: designator={case.get('Designator')}, pin={target_pin_name}, nets={spec_nets}",
                    level=LogLevel.WARNING,
                )
                continue

        cmp_pin_info = (cmp_pin_records.get(comp_name, {}) or {}).get(target_pin_name)
        if comp_name not in pin_crosswalk_cache:
            pin_crosswalk_cache[comp_name] = build_component_pin_crosswalk(
                comp_inst=comp_inst,
                cmp_component_record=(cmp_pin_records.get(comp_name, {}) or {}),
                ndf_component_record=(ndf_pin_records.get(comp_name, {}) or {}),
                net_alias_map=net_alias_map,
            )
        if comp_name not in edb_truth_cache:
            edb_truth_cache[comp_name] = build_component_edb_truth_table(comp_inst)
        if comp_name not in ui_api_crosswalk_cache:
            ui_api_crosswalk_cache[comp_name] = build_component_ui_api_crosswalk(
                comp_inst=comp_inst,
                components_api=getattr(app.edb, "components", None),
                siw_component_record=(siw_pin_records.get(comp_name, {}) or {}),
                ndf_component_record=(ndf_pin_records.get(comp_name, {}) or {}),
                net_alias_map=net_alias_map,
            )
        crosswalk_pin_info = (pin_crosswalk_cache.get(comp_name, {}) or {}).get(target_pin_name)
        truth_component_rows = edb_truth_cache.get(comp_name, [])
        ui_api_component_map = ui_api_crosswalk_cache.get(comp_name, {})
        siw_pin_info = (siw_pin_records.get(comp_name, {}) or {}).get(target_pin_name)
        lookup_pin_name = target_pin_name
        strict_norm_reason = None
        if strict_refdes_pin:
            lookup_pin_name, strict_norm_reason = normalize_spec_pin_for_strict_mode(
                spec_pin=target_pin_name,
                spec_nets=spec_nets,
                crosswalk_pin_record=crosswalk_pin_info,
                net_alias_map=net_alias_map,
            )
            if strict_norm_reason:
                logger.log(
                    f"[SPEC][STRICT][NORMALIZE] {comp_name}:{target_pin_name} -> {lookup_pin_name} "
                    f"(method={strict_norm_reason.get('method')}, conf={strict_norm_reason.get('confidence')}, "
                    f"net_match={strict_norm_reason.get('net_match')})",
                    level=LogLevel.WARNING,
                )
        # 0) Manual override has top priority.
        pin_inst = None
        resolve_mode = "none"
        resolved_pin_name = None
        pin_trace_meta = {}
        ov_key = (str(comp_name).upper(), str(target_pin_name).upper())
        if ov_key in pin_override_map:
            ov_pin_name = str(pin_override_map.get(ov_key, "")).strip()
            ov_inst, ov_resolved_name = _find_component_pin_by_name_or_display(
                comp_inst=comp_inst,
                pin_name=ov_pin_name,
                excluded=used_component_pins.get(comp_name, set()),
            )
            if ov_inst is not None:
                pin_inst = ov_inst
                resolved_pin_name = ov_resolved_name or ov_pin_name
                resolve_mode = "pin_override"
                logger.log(
                    f"[SPEC][OVERRIDE] {comp_name}: {target_pin_name} -> {resolved_pin_name}",
                    level=LogLevel.WARNING,
                )
            else:
                logger.log(
                    f"[SPEC][OVERRIDE][WARNING] Override target not found: {comp_name}:{target_pin_name} -> {ov_pin_name}",
                    level=LogLevel.WARNING,
                )

        # 0.5) Designator + Pin number primary resolver (Spec-centric)
        if pin_inst is None:
            dsg_pin, dsg_mode, dsg_key, dsg_meta = resolve_spec_pin_designator_primary(
                comp_inst=comp_inst,
                spec_pin=target_pin_name,
                spec_nets=spec_nets,
                net_alias_map=net_alias_map,
                ui_api_component_map=ui_api_component_map,
                crosswalk_pin_record=crosswalk_pin_info,
                cmp_pin_record=cmp_pin_info,
                siw_pin_record=siw_pin_info,
                components_api=getattr(app.edb, "components", None),
                excluded_pin_names=used_component_pins.get(comp_name, set()),
            )
            if dsg_pin is not None:
                pin_inst = dsg_pin
                resolved_pin_name = dsg_key or target_pin_name
                resolve_mode = dsg_mode
                pin_trace_meta = {
                    "status": "direct",
                    "reason": resolve_mode,
                    "dsg_source": (dsg_meta or {}).get("source", ""),
                    "dsg_d2": (dsg_meta or {}).get("d2", ""),
                }
                logger.log(
                    f"[SPEC][PINMAP][D+P] {comp_name}: {target_pin_name} -> {resolved_pin_name} "
                    f"(mode={resolve_mode}, net={getattr(pin_inst, 'net_name', '')}, source={(dsg_meta or {}).get('source', '')})",
                    level=LogLevel.WARNING,
                )

        # 1) Spec + API key + EDB GUI pin (3-source) resolver
        if pin_inst is None:
            tri_pin, tri_name, tri_trace = resolve_spec_pin_via_spec_api_gui(
                comp_inst=comp_inst,
                spec_pin=target_pin_name,
                spec_nets=spec_nets,
                crosswalk_pin_record=crosswalk_pin_info,
                ui_api_component_map=ui_api_component_map,
                truth_component_rows=truth_component_rows,
                net_alias_map=net_alias_map,
                excluded_pin_names=used_component_pins.get(comp_name, set()),
            )
            tri_status = str((tri_trace or {}).get("status", "")).strip().lower()
            tri_selected = (tri_trace or {}).get("selected", {}) if isinstance(tri_trace, dict) else {}
            tri_relation_ok = bool(tri_selected.get("ui_hit", False))
            if tri_pin is not None and ((not strict_refdes_pin) or tri_relation_ok):
                pin_inst = tri_pin
                resolved_pin_name = tri_name
                resolve_mode = "spec_api_gui"
                pin_trace_meta = {
                    "status": "direct",
                    "reason": resolve_mode,
                    "tri_status": tri_status,
                    "tri_reason": tri_selected.get("reason"),
                    "tri_ui_hit": tri_relation_ok,
                }
                logger.log(
                    f"[SPEC][PINMAP][3SRC] {comp_name}: {target_pin_name} -> {resolved_pin_name} "
                    f"(net={getattr(pin_inst, 'net_name', '')}, tri_status={tri_status}, "
                    f"reason={tri_selected.get('reason')}, ui_hit={tri_selected.get('ui_hit')}, "
                    f"net_match={tri_selected.get('net_match')}, conf={tri_selected.get('confidence')})",
                    level=LogLevel.WARNING,
                )
            elif tri_pin is not None and strict_refdes_pin and not tri_relation_ok:
                logger.log(
                    f"[SPEC][PINMAP][3SRC][STRICT-SKIP] {comp_name}:{target_pin_name} "
                    f"spec-api candidate exists but api-ui relation is missing. tri_status={tri_status}",
                    level=LogLevel.WARNING,
                )

        if pin_inst is None:
            pin_inst, resolve_mode, resolved_pin_name = resolve_spec_pin_to_edb_pin(
                comp_inst=comp_inst,
                spec_pin=lookup_pin_name,
                spec_nets=spec_nets,
                net_alias_map=net_alias_map,
                cmp_pin_record=cmp_pin_info,
                cmp_component_record=(cmp_pin_records.get(comp_name, {}) or {}),
                crosswalk_pin_record=crosswalk_pin_info,
                components_api=getattr(app.edb, "components", None),
                excluded_pin_names=used_component_pins.get(comp_name, set()),
                edbapp=getattr(app, "edb", None),
                truth_component_rows=truth_component_rows,
                strict_refdes_pin=strict_refdes_pin,
            )

        # Net-referenced correction policy:
        # If pin mapping net differs from Spec net (or no pin), remap by net and re-verify with UI evidence.
        try:
            fixed_inst, fixed_name, fixed_meta = remap_pin_by_net_then_ui(
                comp_inst=comp_inst,
                spec_pin=target_pin_name,
                spec_nets=spec_nets,
                current_pin_inst=pin_inst,
                current_pin_name=resolved_pin_name,
                crosswalk_pin_record=crosswalk_pin_info,
                truth_component_rows=truth_component_rows,
                cmp_pin_record=cmp_pin_info,
                net_alias_map=net_alias_map,
                excluded_pin_names=used_component_pins.get(comp_name, set()),
            )
            fixed_mode = str((fixed_meta or {}).get("mode", "")).strip()
            if fixed_inst is None:
                if pin_inst is not None:
                    logger.log(
                        f"[SPEC][NET-FIX][WARNING] Drop invalid mapping after net/ui verification: "
                        f"{comp_name}:{target_pin_name} -> {resolved_pin_name} (mode={resolve_mode}, fix_mode={fixed_mode})",
                        level=LogLevel.WARNING,
                    )
                pin_inst = None
                resolved_pin_name = None
                resolve_mode = "net_fix_unresolved"
            else:
                if (pin_inst is None) or (resolved_pin_name != fixed_name) or bool((fixed_meta or {}).get("changed", False)):
                    logger.log(
                        f"[SPEC][NET-FIX] {comp_name}:{target_pin_name} remap -> {fixed_name} "
                        f"(prev={resolved_pin_name}, fix_mode={fixed_mode}, ui_verified={fixed_meta.get('ui_verified')})",
                        level=LogLevel.WARNING,
                    )
                    pin_inst = fixed_inst
                    resolved_pin_name = fixed_name
                    if fixed_mode and fixed_mode not in {"net_valid_keep", "no_spec_net"}:
                        resolve_mode = fixed_mode
        except Exception as net_fix_exc:
            logger.log(
                f"[SPEC][NET-FIX][WARNING] Net/UI correction failed for {comp_name}:{target_pin_name}: {net_fix_exc}",
                level=LogLevel.WARNING,
            )

        # Safety gate: reject coord-only mapping for power-like spec nets when resolved net mismatches.
        if pin_inst is not None and resolve_mode in ("coord_only", "coord_only_net_mismatch", "coord_only_power_net_unvalidated"):
            spec_nets_local = [str(n).strip() for n in (spec_nets or []) if str(n).strip()]
            if spec_nets_local and any(_is_power_like_net(n) for n in spec_nets_local):
                net_matched = any(_net_matches_spec(n, pin_inst.net_name, net_alias_map) for n in spec_nets_local)
                signal_like = _is_signal_like_net(pin_inst.net_name)
                if (not net_matched) or signal_like:
                    logger.log(
                        f"[SPEC][SAFETY] Reject coord-only power pin mapping: {comp_name}:{target_pin_name} -> {resolved_pin_name or target_pin_name} "
                        f"(resolved_net={pin_inst.net_name}, spec_nets={spec_nets_local}, signal_like={signal_like})",
                        level=LogLevel.WARNING,
                    )
                    # Recovery attempt after safety rejection:
                    # 1) board-global padstack scan
                    # 2) coordinate spatial query
                    try:
                        rec_inst, rec_key = _find_component_pin_by_global_padstack_scan(
                            comp_inst=comp_inst,
                            spec_pin=target_pin_name,
                            spec_nets=spec_nets_local,
                            net_alias_map=net_alias_map,
                            excluded_pin_names=used_component_pins.get(comp_name, set()),
                            edbapp=getattr(app, "edb", None),
                        )
                        if rec_inst is not None:
                            pin_inst = rec_inst
                            resolved_pin_name = rec_key or target_pin_name
                            resolve_mode = "global_padstack_scan_recover"
                            logger.log(
                                f"[SPEC][RECOVER] Global padstack scan recovered mapping: {comp_name}:{target_pin_name} -> {resolved_pin_name} (net={pin_inst.net_name})",
                                level=LogLevel.WARNING,
                            )
                        else:
                            sq_inst, sq_key = _find_component_pin_by_spatial_query(
                                comp_inst=comp_inst,
                                spec_pin=target_pin_name,
                                spec_nets=spec_nets_local,
                                net_alias_map=net_alias_map,
                                cmp_pin_record=cmp_pin_info,
                                excluded_pin_names=used_component_pins.get(comp_name, set()),
                                edbapp=getattr(app, "edb", None),
                            )
                            if sq_inst is not None:
                                pin_inst = sq_inst
                                resolved_pin_name = sq_key or target_pin_name
                                resolve_mode = "spatial_query_recover"
                                logger.log(
                                    f"[SPEC][RECOVER] Spatial query recovered mapping: {comp_name}:{target_pin_name} -> {resolved_pin_name} (net={pin_inst.net_name})",
                                    level=LogLevel.WARNING,
                                )
                    except Exception as rec_exc:
                        logger.log(
                            f"[SPEC][RECOVER][WARNING] Recovery attempt failed for {comp_name}:{target_pin_name}: {rec_exc}",
                            level=LogLevel.WARNING,
                        )

                    if pin_inst is not None and resolve_mode in ("global_padstack_scan_recover", "spatial_query_recover"):
                        # Recovered successfully; skip rejection path.
                        pass
                    else:
                        # Recovery diagnostics
                        try:
                            gcnt = 0
                            gtok = _normalize_pin_token(target_pin_name)
                            gspec = [str(n).strip() for n in (spec_nets_local or []) if str(n).strip()]
                            for pad in _iter_all_padstack_instances(getattr(app, "edb", None)):
                                pname = str(getattr(pad, "name", "") or "")
                                pnet = str(getattr(pad, "net_name", "") or "")
                                pcname = _pad_component_name(pad)
                                ptok = _normalize_pin_token(pname)
                                if gtok and gtok not in ptok:
                                    continue
                                if pcname and str(pcname).upper() != str(comp_name).upper():
                                    continue
                                if gspec and not any(_net_matches_spec(sn, pnet, net_alias_map) for sn in gspec):
                                    continue
                                gcnt += 1
                            logger.log(
                                f"[SPEC][DEBUG] global scan candidates for {comp_name}:{target_pin_name} => {gcnt}",
                                level=LogLevel.DETAIL1,
                            )
                        except Exception:
                            pass
                    # Detailed diagnostics to identify why helper-based mapping is not selected.
                    try:
                        helper = getattr(app.edb, "components", None)
                        hp = helper.get_pin_from_component(comp_name, pin_name=target_pin_name) if helper else []
                        hp = hp or []
                        hp_brief = []
                        for p in hp[:10]:
                            hp_brief.append({
                                "component_pin": str(getattr(p, "component_pin", "")),
                                "aedt_name": str(getattr(p, "aedt_name", "")),
                                "name": str(getattr(p, "name", "")),
                                "net": str(getattr(p, "net_name", "")),
                            })
                        logger.log(
                            f"[SPEC][DEBUG] helper pins for {comp_name}:{target_pin_name} => {hp_brief}",
                            level=LogLevel.DETAIL1,
                        )
                    except Exception:
                        pass
                    try:
                        raw_comp = getattr(comp_inst, "_edb_object", None) or getattr(comp_inst, "edbcomponent", None)
                        dpins = _iter_dotnet_pins_from_component(raw_comp)
                        ttok = _normalize_pin_token(target_pin_name)
                        dbrief = []
                        for dp in dpins:
                            toks = sorted(list(_collect_dotnet_pin_tokens(dp, components_api=getattr(app.edb, "components", None))))
                            if ttok and ttok not in toks:
                                continue
                            dbrief.append({
                                "tokens": toks[:6],
                                "net": _dotnet_pin_net_name(dp),
                                "pos": _dotnet_pin_position(dp),
                            })
                            if len(dbrief) >= 10:
                                break
                        logger.log(
                            f"[SPEC][DEBUG] dotnet pins for {comp_name}:{target_pin_name} => {dbrief}",
                            level=LogLevel.DETAIL1,
                        )
                    except Exception:
                        pass
                    if resolve_mode not in ("global_padstack_scan_recover", "spatial_query_recover"):
                        pin_inst = None
                        resolve_mode = "coord_rejected_power_net_mismatch"
                        resolved_pin_name = None
        actual_pin_name = resolved_pin_name or target_pin_name
        if not pin_inst:
            if strict_refdes_pin:
                trace_meta = {
                    "status": "skip",
                    "reason": "strict_refdes_pin_no_exact_match",
                }
                add_pin_mapping_record(
                    case_row=case,
                    resolved_comp=comp_name,
                    spec_pin=target_pin_name,
                    resolved_pin=actual_pin_name,
                    mode=resolve_mode or "strict_no_exact_pin",
                    spec_nets_row=spec_nets,
                    resolved_net="",
                    trace_meta=trace_meta,
                )
                spec_skip_missing_pin += 1
                logger.log(
                    f"[SPEC][SKIP][STRICT] Pin exact match not found: designator={comp_name}, pin={target_pin_name}, "
                    f"lookup_pin={lookup_pin_name}, nets={spec_nets}. Net/coord fallback disabled by policy.",
                    level=LogLevel.WARNING,
                )
                continue
            # Net-only fallback: if pin cannot be resolved, continue with resolved PCB net.
            try:
                board_nets = list((getattr(app.edb, "nets", None).nets or {}).keys())
            except Exception:
                board_nets = []
            fallback_net = _resolve_board_net_by_spec(spec_nets, board_nets, net_alias_map)
            trace_meta = {
                "start_net": fallback_net or "",
                "reached_net": "",
                "hops": "",
                "status": "skip",
                "reason": "",
            }
            if fallback_net:
                traced_pin_name, traced_pin_net, traced_hops, trace_reason, trace_diag = trace_ic_local_power_pin(
                    comp_inst_local=comp_inst,
                    start_net=fallback_net,
                    gnd_net=GND_NET,
                    spec_nets=spec_nets,
                    max_hops=10,
                )
                diag_hits = trace_diag.get("ic_hits", []) if isinstance(trace_diag, dict) else []
                diag_visited = trace_diag.get("visited_nets", []) if isinstance(trace_diag, dict) else []
                diag_hit_tokens = [f"{h.get('pin')}@{h.get('net')}(hop={h.get('hop')})" for h in diag_hits if isinstance(h, dict)]
                trace_meta.update(
                    {
                        "start_net": fallback_net,
                        "reached_net": traced_pin_net or "",
                        "hops": traced_hops if traced_hops >= 0 else "",
                        "reason": trace_reason,
                        "candidate_pins": diag_hit_tokens,
                        "visited_nets": diag_visited,
                    }
                )
                if diag_hits:
                    preview = ", ".join(diag_hit_tokens[:30])
                    if len(diag_hit_tokens) > 30:
                        preview += f", ...(+{len(diag_hit_tokens)-30})"
                    logger.log(
                        f"[SPEC][NET-FALLBACK][CANDIDATES] {comp_name}:{target_pin_name} traced_nets={diag_visited} | ic_hits={preview}",
                        level=LogLevel.WARNING,
                    )
                else:
                    logger.log(
                        f"[SPEC][NET-FALLBACK][CANDIDATES] {comp_name}:{target_pin_name} traced_nets={diag_visited} | ic_hits=<none>",
                        level=LogLevel.WARNING,
                    )
                if traced_pin_name:
                    trace_meta["status"] = "ok"
                    logger.log(
                        f"[SPEC][NET-FALLBACK][TRACE] {comp_name}:{target_pin_name} -> IC pin {traced_pin_name} "
                        f"(start_net={fallback_net}, reached_net={traced_pin_net}, hops={traced_hops})",
                        level=LogLevel.WARNING,
                    )
                else:
                    trace_meta["status"] = "skip"
                    logger.log(
                        f"[SPEC][NET-FALLBACK][TRACE][SKIP] Could not trace IC-local power pin from net={fallback_net} "
                        f"for {comp_name}:{target_pin_name} (GND/signal excluded).",
                        level=LogLevel.WARNING,
                    )
                    fallback_net = ""

            if fallback_net:
                if append_case_from_target_net(
                    case_row=case,
                    resolved_comp=comp_name,
                    spec_pin=target_pin_name,
                    resolved_pin=(traced_pin_name or target_pin_name),
                    target_net=fallback_net,
                    spec_nets_row=spec_nets,
                    mapping_mode="net_only_fallback",
                    trace_meta=trace_meta,
                ):
                    logger.log(
                        f"[SPEC][NET-FALLBACK] Pin unresolved. Continue with net mapping: {comp_name}:{target_pin_name} -> net={fallback_net}",
                        level=LogLevel.WARNING,
                    )
                    add_pin_mapping_record(
                        case_row=case,
                        resolved_comp=comp_name,
                        spec_pin=target_pin_name,
                        resolved_pin=(traced_pin_name or target_pin_name),
                        mode="net_only_fallback",
                        spec_nets_row=spec_nets,
                        resolved_net=fallback_net,
                        trace_meta=trace_meta,
                    )
                    continue

            add_pin_mapping_record(
                case_row=case,
                resolved_comp=comp_name,
                spec_pin=target_pin_name,
                resolved_pin=actual_pin_name,
                mode=resolve_mode,
                spec_nets_row=spec_nets,
                resolved_net="",
                trace_meta=trace_meta,
            )
            spec_skip_missing_pin += 1
            logger.log(
                f"[SPEC][SKIP] Pin not resolved: designator={comp_name}, pin={target_pin_name}, mode={resolve_mode}, nets={spec_nets}",
                level=LogLevel.WARNING,
            )
            continue
        add_pin_mapping_record(
            case_row=case,
            resolved_comp=comp_name,
            spec_pin=target_pin_name,
            resolved_pin=actual_pin_name,
            mode=resolve_mode,
            spec_nets_row=spec_nets,
            resolved_net=pin_inst.net_name,
            trace_meta=(pin_trace_meta or {"status": "direct"}),
        )
        # Prevent duplicate mapping to the same resolved pin within one component.
        used_component_pins[comp_name].add(actual_pin_name)
        if resolve_mode != "exact":
            logger.log(
                f"[SPEC][PINMAP] {comp_name}: {target_pin_name} -> {actual_pin_name} (mode={resolve_mode}, net={pin_inst.net_name})",
                level=LogLevel.WARNING,
            )
        append_case_from_target_net(
            case_row=case,
            resolved_comp=comp_name,
            spec_pin=target_pin_name,
            resolved_pin=actual_pin_name,
            target_net=pin_inst.net.name,
            spec_nets_row=spec_nets,
            mapping_mode=resolve_mode,
            trace_meta=(pin_trace_meta or {"status": "direct", "reason": resolve_mode}),
        )

    target_nets = {GND_NET} | {n for case in pdn_cases_info for n in case.get('Full_Net_Chain', [])}
    if not app.sanitize_nets(target_nets): raise PDNSessionException(ErrorCode.SANITIZE_FAIL)
    logger.log(
        f"[SPEC] Case build summary: total_rows={len(spec_info)}, cases={len(pdn_cases_info)}, "
        f"fallback_resolved={spec_fallback_resolved}, skip_missing_comp={spec_skip_missing_comp}, skip_missing_pin={spec_skip_missing_pin}",
        level=LogLevel.INFO,
    )
    logger.log(f"[SPEC] Pin resolution modes: {dict(pin_resolution_stats)}", level=LogLevel.INFO)
    logger.log(f"[SPEC] Pin mapping quality: {dict(pin_mapping_quality_stats)}", level=LogLevel.INFO)
    cross_csv, cross_json = export_pin_crosswalk_reports(OUTPUT_DIR, pin_crosswalk_cache)
    truth_csv, truth_json = export_edb_truth_table_reports(OUTPUT_DIR, edb_truth_cache)
    ui_api_csv, ui_api_json = export_ui_api_crosswalk_reports(OUTPUT_DIR, ui_api_crosswalk_cache)
    logger.log(f"[SPEC] Exported pin crosswalk CSV: {cross_csv}", level=LogLevel.DETAIL1)
    logger.log(f"[SPEC] Exported pin crosswalk JSON: {cross_json}", level=LogLevel.DETAIL1)
    logger.log(f"[SPEC] Exported EDB truth-table CSV: {truth_csv}", level=LogLevel.DETAIL1)
    logger.log(f"[SPEC] Exported EDB truth-table JSON: {truth_json}", level=LogLevel.DETAIL1)
    logger.log(f"[SPEC] Exported UI<->API crosswalk CSV: {ui_api_csv}", level=LogLevel.DETAIL1)
    logger.log(f"[SPEC] Exported UI<->API crosswalk JSON: {ui_api_json}", level=LogLevel.DETAIL1)
    pin_map_report = OUTPUT_DIR / "spec_to_edb_pin_map.json"
    with open(pin_map_report, "w", encoding="utf-8") as f:
        json.dump(
            {
                "Summary": {
                    "TotalRows": len(spec_info),
                    "ResolvedCases": len(pdn_cases_info),
                    "ResolutionModes": dict(pin_resolution_stats),
                    "MappingQuality": dict(pin_mapping_quality_stats),
                },
                "Records": pin_mapping_records,
            },
            f,
            indent=2,
            ensure_ascii=False,
        )
    logger.log(f"[SPEC] Exported Spec->EDB pin map: {pin_map_report}", level=LogLevel.DETAIL1)

    run_tag = RUN_TAG or time.strftime('%Y%m%d_%H%M%S')
    PRE_EDB_FILE_PATH = EDB_FILE_PATH.parent / f"{EDB_FILE_PATH.stem}_pre_{run_tag}{EDB_FILE_PATH.suffix}"
    ensure_pre_edb_saved(app=app, source_edb_path=EDB_FILE_PATH, pre_edb_path=PRE_EDB_FILE_PATH, max_retries=2, timeout=300.0)
    
    if STAGE == "full":
        app.set_cad_file(PRE_EDB_FILE_PATH)
        EDB_SETUP_APP = app
        app = None
    step += 1

except Exception:
    logger.fatal(f"Modify CAD Data using EDB database : {traceback.format_exc()}")
    raise
finally:
    if app:
        safe_close_edb_session(app, logger, "step4")
        app.quit_application()
    if STAGE != "full" and EDB_SETUP_APP and EDB_SETUP_APP is not app:
        safe_close_edb_session(EDB_SETUP_APP, logger, "step4-setup-app")
        EDB_SETUP_APP.quit_application()
        EDB_SETUP_APP = None
# endregion

# region PRE Stage Execution (Legacy)
# NOTE:
# Pre-stage used to terminate here. This early-exit path is intentionally disabled so that
# stage=pre reaches Step5 "Setting" (Port/VRM/setup) and logs full setting diagnostics.
if STAGE == "pre":
    logger.log(
        "[PRE] Legacy pre short-circuit is disabled. Continue to Step5 for setting-phase validation logs.",
        level=LogLevel.INFO,
    )
# endregion

# region 5. Modify CAD Data using SIwave and Set PDN Simulation
try:
    logger.log(f"Step {step}. CAD Modification", level=LogLevel.INFO)
    if not wait_for_edb_ready(PRE_EDB_FILE_PATH, timeout=300.0, check_interval=3.0):
        raise FileNotFoundError(f"Target EDB path or edb.def is not ready after retries: {PRE_EDB_FILE_PATH}")

    app = None
    image_app = None

    try:
        step5_backend = resolve_solver_backend(STACKUP_LAYER_COUNT, conf_manager.data, logger)
        if step5_backend == "siwave":
            app = SIwave(version=AEDT_VERSION, logger=logger)
            app.import_edb(str(PRE_EDB_FILE_PATH))
            edb_ops_app = EDB_SETUP_APP if (EDB_SETUP_APP and getattr(EDB_SETUP_APP, "edb", None)) else app
            if edb_ops_app is app and not getattr(app, "edb", None):
                app.set_cad_file(str(PRE_EDB_FILE_PATH))
        else:
            edb_ops_app = EDB_SETUP_APP if (EDB_SETUP_APP and getattr(EDB_SETUP_APP, "edb", None)) else None
            if not edb_ops_app:
                # Fallback for unexpected session loss: reopen EDB through wrapper.
                edb_ops_app = SIwave(version=AEDT_VERSION, logger=logger)
                edb_ops_app.set_cad_file(str(PRE_EDB_FILE_PATH))
                app = edb_ops_app

        log_signal_layer_thicknesses(edb_ops_app, logger, tag="[STACKUP][Step5]")

        pdn_logic = PDN(logger=logger)
        if hasattr(pdn_logic, "apply_dc_shorts"):
            pdn_logic.apply_dc_shorts(
                app=edb_ops_app,
                shorted_comp_defs=conf_manager.data['PDN']['dcShort']['shortedComp'],
                del_comps=DEL_COMP,
                short_correction=SHORT_CORRECTION
            )
        else:
            logger.log(
                "[PDN][Short][WARNING] apply_dc_shorts() is not available in PDN class. Skip short replacement.",
                level=LogLevel.WARNING,
            )

        stackup_input = STACKUP_EFFECTIVE_FILE if STACKUP_EFFECTIVE_FILE else (Path(STACKUP_INPUT_FILE) if STACKUP_INPUT_FILE else None)
        profile_key, sws_name, sfsdf_name = resolve_zparam_profile(
            conf_manager.data,
            Path(stackup_input) if stackup_input else None,
            STACKUP_LAYER_COUNT,
        )
        if step5_backend == "siwave" and stackup_input and not STACKUP_APPLIED_AT_PROJECT_CREATION:
            raw_stackup = Path(stackup_input)
            if app.import_layer_stackup(raw_stackup):
                logger.log(f"[PRE] Raw stackup imported: {raw_stackup}", level=LogLevel.DETAIL1)
            else:
                raise RuntimeError(f"[PRE] Failed to import raw stackup file: {raw_stackup}")
        elif step5_backend == "siwave" and STACKUP_APPLIED_AT_PROJECT_CREATION and stackup_input:
            logger.log(
                f"[PRE] Skip stackup re-import (already applied at create_project): {stackup_input}",
                level=LogLevel.DETAIL1,
            )
            
        SWS_FILE = WORKING_DIR / 'core' / sws_name

        s2p_dir_conf = conf_manager.data.get('PDN', {}).get('sParameter', {}).get('s2pDirectory', '')
        s2p_dir = Path(s2p_dir_conf) if s2p_dir_conf else None
        if s2p_dir and not s2p_dir.is_absolute():
            s2p_dir = INPUT_DIR / s2p_dir

        if conf_manager.data.get('PDN', {}).get('sParameter', {}).get('enableAssign', True):
            model_assign_app = edb_ops_app
            innercap_name = settings_manager.data.get('CAE', {}).get('SOC', {}).get('Inner_cap')
            innercap_csv_path = (INPUT_DIR / innercap_name) if innercap_name else None
            bom_name = settings_manager.data.get('CAE', {}).get('PCB', {}).get('BOM')
            bom_file_path = (INPUT_DIR / bom_name) if bom_name else None
            model_lib_candidates = conf_manager.data.get('PDN', {}).get('sParameter', {}).get('model_library_dir_candidates', [])
            resolved_candidates = [Path(cand) if Path(cand).is_absolute() else (INPUT_DIR / Path(cand)) for cand in model_lib_candidates if cand]
            assign_sparameter_models(
                app=model_assign_app,
                bom_info=bom_info,
                inner_cap_audit=inner_cap_audit,
                pmap_file=None, 
                s2p_dir=s2p_dir,
                gnd_net=GND_NET,
                output_dir=OUTPUT_DIR,
                logger=logger,
                innercap_csv_path=innercap_csv_path,
                bom_file_path=bom_file_path,
                search_roots=[INPUT_DIR, WORKING_DIR, OUTPUT_DIR] + resolved_candidates,
            )

        vrm_setup_conf = conf_manager.data.get("PDN", {}).get("vrmSetup", {})
        vrm_records = configure_ports_and_vrms_from_spec(
            app=edb_ops_app,
            cases=pdn_cases_info,
            gnd_net=GND_NET,
            bulk_inductor_set=set(bom_info.get("bulkInd", [])),
            output_dir=OUTPUT_DIR,
            logger=logger,
            vrm_setup_conf=vrm_setup_conf,
            port_app=(app if step5_backend == "siwave" else None),
        )
        vrm_done = sum(1 for r in vrm_records if r.get("Status") == "Done")
        vrm_skipped = len(vrm_records) - vrm_done
        logger.log(f"[VRM_SETUP] Summary: total={len(vrm_records)}, done={vrm_done}, skipped={vrm_skipped}", level=LogLevel.INFO)
        if STAGE == "pre":
            logger.log(
                f"[PRE][CHECK] Port/VRM detail report: {OUTPUT_DIR / 'vrm_port_setup_result.json'}",
                level=LogLevel.INFO,
            )
        if vrm_done == 0 and len(pdn_cases_info) > 0:
            logger.log(
                "[VRM_SETUP][WARNING] No Port/VRM termination was created. Solver may fail with no terminals/sources.",
                level=LogLevel.WARNING,
            )
        exclude_tokens = conf_manager.data.get("PDN", {}).get("dcShort", {}).get("excludeNet", [])
        analysis_nets = collect_analysis_nets(pdn_cases_info, GND_NET, exclude_tokens=exclude_tokens)
        classify_and_audit_analysis_nets(
            edb_ops_app,
            analysis_nets=analysis_nets,
            gnd_net=GND_NET,
            logger=logger,
            exclude_tokens=exclude_tokens,
        )

        # If ports/VRM were edited through a separate EDB session, sync back to the SIwave project session.
        if step5_backend == "siwave" and edb_ops_app is not app:
            base_cad_name = INPUT_CAD_FILE.stem.split('-')[0]
            sync_edb_path = OUTPUT_DIR / f"{base_cad_name}_step5_sync.aedb"
            sync_edb_changes_to_siw_project(
                source_app=edb_ops_app,
                target_app=app,
                sync_edb_path=sync_edb_path,
                logger=logger,
            )

        # Apply SIwave setup only when solver backend is SIwave.
        if step5_backend == "siwave":
            app.setup_simulation(None, SWS_FILE, None)
            apply_dynamic_frequency_setup(app, STACKUP_LAYER_COUNT, conf_manager.data, logger)
        else:
            logger.log(
                "[PRE] Skip SIwave setup import for AEDT cutout backend (avoid SIwave-only setup artifacts).",
                level=LogLevel.INFO,
            )

        base_cad_name = INPUT_CAD_FILE.stem.split('-')[0]
        run_tag = RUN_TAG or time.strftime('%Y%m%d_%H%M%S')
        FINAL_EDB_FILE_PATH = OUTPUT_DIR / f"{base_cad_name}_ref_{run_tag}.aedb"
        if step5_backend == "siwave":
            REF_SIwave_FILE_PATH = SIwave_FILE_PATH
            app.save_project_as(REF_SIwave_FILE_PATH)
            if not REF_SIwave_FILE_PATH.exists():
                raise FileNotFoundError(f"SIwave file was not created at {REF_SIwave_FILE_PATH}")
            app.export_edb(FINAL_EDB_FILE_PATH)
        else:
            if FINAL_EDB_FILE_PATH.exists():
                shutil.rmtree(FINAL_EDB_FILE_PATH, ignore_errors=True)
            edb_ops_app.edb.save_as(str(FINAL_EDB_FILE_PATH))
            if not wait_for_edb_ready(FINAL_EDB_FILE_PATH, timeout=300.0, check_interval=3.0):
                raise FileNotFoundError(f"Final EDB was not created at {FINAL_EDB_FILE_PATH}")
            REF_SIwave_FILE_PATH = SIwave_FILE_PATH
            create_siw_snapshot_from_edb(
                aedt_version=AEDT_VERSION,
                source_edb_path=FINAL_EDB_FILE_PATH,
                siw_output_path=REF_SIwave_FILE_PATH,
                logger=logger,
            )

            # Export full-board pre-solve AEDT project (before cutout) for review/debug handoff.
            try:
                aedt_full = AEDT(version=AEDT_VERSION, logger=logger)
                aedt_full.export_full_presolve_aedt(
                    ref_edb_path=Path(FINAL_EDB_FILE_PATH),
                    output_dir=OUTPUT_DIR,
                    project_stem=base_cad_name,
                )
            except Exception as full_aedt_exc:
                logger.log(
                    f"[AEDT][FULL][WARNING] Failed to export full pre-solve AEDT project: {full_aedt_exc}",
                    level=LogLevel.WARNING,
                )
    finally:
        if app:
            safe_close_edb_session(app, logger, "step5-main")
            app.quit_application()
        if EDB_SETUP_APP:
            safe_close_edb_session(EDB_SETUP_APP, logger, "step5-setup-app")
            EDB_SETUP_APP.quit_application()
            EDB_SETUP_APP = None

    if step5_backend == "siwave":
        image_app = None
        try:
            image_app = SIwave(version=AEDT_VERSION, logger=logger)
            image_app.set_cad_file(str(FINAL_EDB_FILE_PATH))
            image_app.export_layer_images(REF_SIwave_FILE_PATH, OUTPUT_DIR, GND_NET)
            image_app.close_edb()
        finally:
            if image_app:
                safe_close_edb_session(image_app, logger, "step5-image-export")
                image_app.quit_application()
    else:
        try:
            aedt_image = AEDT(version=AEDT_VERSION, logger=logger)
            aedt_image.export_edb_preview_images(
                ref_edb_path=Path(FINAL_EDB_FILE_PATH),
                output_dir=OUTPUT_DIR,
            )
        except Exception as img_exc:
            # Image export failure should not block solve stage.
            logger.log(
                f"[AEDT][IMG][WARNING] Preview image export failed but workflow will continue: {img_exc}",
                level=LogLevel.WARNING,
            )
            # Fallback: generate top/bottom images through SIwave snapshot if available.
            try:
                image_app = SIwave(version=AEDT_VERSION, logger=logger)
                image_app.set_cad_file(str(FINAL_EDB_FILE_PATH))
                image_app.export_layer_images(REF_SIwave_FILE_PATH, OUTPUT_DIR, GND_NET)
                image_app.close_edb()
                logger.log("[AEDT][IMG] Fallback SIwave layer image export succeeded.", level=LogLevel.INFO)
            except Exception as fallback_exc:
                logger.log(f"[AEDT][IMG][WARNING] Fallback SIwave image export failed: {fallback_exc}", level=LogLevel.WARNING)
            finally:
                try:
                    if 'image_app' in locals() and image_app:
                        safe_close_edb_session(image_app, logger, "step5-image-fallback")
                        image_app.quit_application()
                except Exception:
                    pass

    # stage=pre should stop after "Setting" phase (Port/VRM + setup artifacts).
    if STAGE == "pre":
        try:
            pre_solver_backend = resolve_solver_backend(STACKUP_LAYER_COUNT, conf_manager.data, logger)
        except Exception:
            pre_solver_backend = "siwave"

        pre_project_path = REF_SIwave_FILE_PATH if REF_SIwave_FILE_PATH else SIwave_FILE_PATH
        pre_project_path = Path(pre_project_path)
        pre_edb_dir = Path(FINAL_EDB_FILE_PATH) if FINAL_EDB_FILE_PATH else Path(PRE_EDB_FILE_PATH)

        pre_records = []
        for idx, case in enumerate(pdn_cases_info):
            net = str(case.get("Display_Net", case.get("Spec_Net", case.get("Net", ""))))
            ic = str(case.get("IC", ""))
            safe_ic = "".join(c for c in ic if c.isalnum() or c == "_")
            safe_net = "".join(c for c in net if c.isalnum() or c == "_")
            pre_records.append(
                build_preprocessing_record(
                    case=case,
                    idx=idx,
                    net_siw_file=pre_project_path,
                    net_edb_dir=pre_edb_dir,
                    v_port_name=f"V_{safe_ic}_{safe_net}",
                    i_port_name=f"I_{safe_ic}_{safe_net}",
                    gnd_net=GND_NET,
                    solver_backend=pre_solver_backend,
                )
            )

        with open(OUTPUT_DIR / 'preprocessing_result.json', 'w', encoding='utf-8') as f:
            json.dump(pre_records, f, indent=4, ensure_ascii=False)
        logger.log(
            f"[PRE] Exported preprocessing result to: {OUTPUT_DIR / 'preprocessing_result.json'}",
            level=LogLevel.INFO,
        )
        logger.log(
            f"[PRE][CHECK] Pin mapping report: {OUTPUT_DIR / 'spec_to_edb_pin_map.json'}",
            level=LogLevel.INFO,
        )
        logger.log(
            f"[PRE][CHECK] EDB truth table: {OUTPUT_DIR / 'edb_truth_table.csv'}",
            level=LogLevel.INFO,
        )
        logger.log(
            f"[PRE][CHECK] Final setting EDB: {FINAL_EDB_FILE_PATH}",
            level=LogLevel.INFO,
        )
        logger.log(
            f"[PRE][CHECK] Final setting SIW: {REF_SIwave_FILE_PATH}",
            level=LogLevel.INFO,
        )
        logger.log(
            "[PRE] Stage pre completed at Setting phase (Port/VRM + setup). Solve and report stages are skipped by design.",
            level=LogLevel.INFO,
        )

    step += 1

except Exception:
    logger.fatal(f"An error occurred while CAD modification process : {traceback.format_exc()}")
    raise SystemExit(1)
# endregion

# region 6. Generate Files and Run PDN Setup
app = None
try:
    if STAGE == "pre":
        logger.log(
            f"Step {step}. Generate Files and Run PDN Setup skipped (stage=pre, settings-only).",
            level=LogLevel.INFO,
        )
        END_TIME = time.strftime('%Y.%m.%d, %H:%M:%S')
    else:
        logger.log(f"Step {step}. Generate Files and Run PDN Setup (Unified Flow)", level=LogLevel.INFO)
        MODEL_NAME = INPUT_CAD_FILE.stem.split('-')[0]
        pdn_setup_conf = conf_manager.data.get("PDN", {}).get("setup", {})
        z_exec_name = str(pdn_setup_conf.get("zExecFile", "PDN.exec")).strip() or "PDN.exec"
        exec_file = WORKING_DIR / 'core' / z_exec_name
        if not exec_file.exists():
            logger.log(
                f"[WARNING] Z solve exec not found: {exec_file}. Fallback to DC exec.",
                level=LogLevel.WARNING,
            )
            exec_file = WORKING_DIR / 'core' / 'PDN.exec'
        runtime_edb_path = FINAL_EDB_FILE_PATH if (FINAL_EDB_FILE_PATH and Path(FINAL_EDB_FILE_PATH).exists()) else PRE_EDB_FILE_PATH
        logger.log(f"[UNIFIED] Runtime EDB selected: {runtime_edb_path}", level=LogLevel.DETAIL1)
        if not runtime_edb_path or not Path(runtime_edb_path).exists():
            raise FileNotFoundError(f"Runtime EDB not found: {runtime_edb_path}")
        solver_backend = resolve_solver_backend(STACKUP_LAYER_COUNT, conf_manager.data, logger)
        SOLVER_BACKEND_USED = solver_backend
        conf_manager.data.setdefault("PDN", {}).setdefault("runtime", {})["solver_backend"] = solver_backend
        if solver_backend not in {"siwave", "aedt_cutout"}:
            raise ValueError(f"Unsupported solver backend: {solver_backend}")
        preprocessing_data = []
        if solver_backend == "siwave":
            siw_execute_file = resolve_siwave_executable(AEDT_VERSION)
            case_data_app = EDB_SETUP_APP if EDB_SETUP_APP else app
            if not case_data_app:
                app = SIwave(version=AEDT_VERSION, logger=logger)
                app.set_cad_file(str(runtime_edb_path))
                case_data_app = app

            signal_layers = list(case_data_app.edb.stackup.signal_layers.keys())
            preprocessing_data = run_pdn_unified(
                cases=pdn_cases_info,
                model_name=MODEL_NAME,
                output_dir=OUTPUT_DIR,
                ref_siwave_file_path=REF_SIwave_FILE_PATH,
                ref_edb_path=runtime_edb_path,
                gnd_net=GND_NET,
                aedt_version=AEDT_VERSION,
                case_data_app=case_data_app,
                signal_layers=signal_layers,
                conf_data=conf_manager.data,
                siw_execute_file=siw_execute_file,
                exec_file=exec_file,
                bulk_inductor_list=bom_info.get('bulkInd', []),
                run_solve=True,
            )
        elif solver_backend == "aedt_cutout":
            # Keep preprocessing schema for compatibility without opening SIwave/EDB sessions in Step6.
            full_siw_file = (OUTPUT_DIR / f"{MODEL_NAME}_PDN_FULL.siw").resolve()
            for idx, case in enumerate(pdn_cases_info):
                net = str(case.get("Display_Net", case.get("Spec_Net", case.get("Net", ""))))
                ic = str(case.get("IC", ""))
                safe_ic = "".join(c for c in ic if c.isalnum() or c == "_")
                safe_net = "".join(c for c in net if c.isalnum() or c == "_")
                preprocessing_data.append(
                    build_preprocessing_record(
                        case=case,
                        idx=idx,
                        net_siw_file=full_siw_file,
                        net_edb_dir=Path(runtime_edb_path),
                        v_port_name=f"V_{safe_ic}_{safe_net}",
                        i_port_name=f"I_{safe_ic}_{safe_net}",
                        gnd_net=GND_NET,
                        solver_backend="aedt_cutout",
                    )
                )
            run_pdn_aedt_cutout_solve(
                cases=pdn_cases_info,
                model_name=MODEL_NAME,
                ref_edb_path=Path(runtime_edb_path),
                output_dir=OUTPUT_DIR,
                aedt_version=AEDT_VERSION,
                conf_data=conf_manager.data,
                logger=logger,
            )

        with open(OUTPUT_DIR / 'preprocessing_result.json', 'w', encoding='utf-8') as f:
            json.dump(preprocessing_data, f, indent=4, ensure_ascii=False)
        logger.log(f"Exported preprocessing result to: {OUTPUT_DIR / 'preprocessing_result.json'}", level=LogLevel.DETAIL1)

        step += 1

except Exception:
    logger.fatal(f"An error occurred while generating files and running simulation : {traceback.format_exc()}")
    raise SystemExit(1)
finally:
    if app:
        safe_close_edb_session(app, logger, "step6")
        app.quit_application()
    END_TIME = time.strftime('%Y.%m.%d, %H:%M:%S')
# endregion

# region 8. Post-Processing
if STAGE == "pre":
    logger.log(f"Step {step}. Post-processing skipped (stage=pre)", level=LogLevel.INFO)
elif SOLVER_BACKEND_USED == "aedt_cutout":
    try:
        logger.log(
            f"Step {step}. Post-processing : Export AEDT cutout summary artifacts",
            level=LogLevel.INFO,
        )
        export_aedt_cutout_post_reports(OUTPUT_DIR, logger)
    except Exception:
        logger.fatal(f"An error occurred while exporting AEDT post artifacts : {traceback.format_exc()}")
        raise SystemExit(1)
elif SOLVER_BACKEND_USED == "siwave":
    try:
        logger.log(f"Step {step}. Post-processing : Extracting PDN results", level=LogLevel.INFO)
        full_state = run_standalone_post(
            conf_manager,
            INPUT_JSON,
            OUTPUT_DIR,
            analysis_start=START_TIME,
            analysis_end=END_TIME,
        )
    except Exception:
        logger.fatal(f"An error occurred while performing PDN results extracting : {traceback.format_exc()}")
        raise SystemExit(1)
else:
    logger.fatal(f"Unsupported solver backend at post stage: {SOLVER_BACKEND_USED}")
    raise SystemExit(1)
# endregion
