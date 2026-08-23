# coding=utf-8
# <2025> ANSYS, Inc. Unauthorized use, distribution, or duplication is prohibited

import json
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


# region Global Variables
logger = Logger(name="PDN")
step = 0
UTILITY_NAME = "AutoPDN"
VERSION = "1.4"
START_TIME = None
END_TIME = None
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
    return {
        "Schema_Version": 1,
        "Generated_From": "Spec",
        "Case_Index": index + 1,
        "IC_Designator": case.get("IC", ""),
        "IC_Pin": case.get("IC_pin", ""),
        "Target_Net": case.get("Net", ""),
        "Source_Component": case.get("Source_name", ""),
        "Source_Pin": case.get("Source_pin", ""),
        "Net_Chain": case.get("Source_net_chain", []),
        "Full_Net_Chain": case.get("Full_Net_Chain", []),
        "Voltage_V": case.get("Vmag", 0.0),
        "Current_A": case.get("Imag", 0.0),
        "Min_Spec_V": case.get("MinSpec", 0.0),
        "Max_Spec_V": case.get("MaxSpec", 0.0),
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

        # PDN 해석용 셋업 (Pmap 제거, 동적 주파수 셋업 적용)
        app.setup_simulation(None, sws_file, None) 
        apply_dynamic_frequency_setup(app, layer_count, conf_data, logger)
        
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

def prepare_stackup_for_project(app, stackup_path: Path | None, work_dir: Path, logger: Logger):
    return pdn_setup_utils.prepare_stackup_for_project(app, stackup_path, work_dir, logger)


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
        result = run_external_tool([str(dsgn2anf_exec), '-r', str(dsgn_file), '-o', str(dsgn_file.parent)], "DFdsgn2anf.exe")
        last_rc = result.returncode
        anf_file = _pick_best_generated_file(dsgn_file.parent, ".anf", base_stem)
        cmp_file = _pick_best_generated_file(dsgn_file.parent, ".cmp", base_stem)
        if result.returncode == 0 and anf_file and cmp_file:
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

def sanitize_name_token(value):
    return re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "").strip()).strip("_")

def configure_ports_and_vrms_from_spec(app, cases, gnd_net, bulk_inductor_set, output_dir, logger, vrm_setup_conf=None):
    return vrm_setup.configure_ports_and_vrms_from_spec(
        app=app,
        cases=cases,
        gnd_net=gnd_net,
        bulk_inductor_set=bulk_inductor_set,
        output_dir=output_dir,
        logger=logger,
        vrm_setup_conf=vrm_setup_conf,
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

def build_preprocessing_record(case, idx, net_siw_file, net_edb_dir, v_port_name, i_port_name, gnd_net):
    return {
        "Schema_Version": 3,
        "Case_Index": idx + 1,
        "IC_Designator": case.get('IC', ''),
        "IC_Pin": case.get('IC_pin', ''),
        "Target_Net": case.get('Net', ''),
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
    }

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
    full_siw_file = output_dir / f"{model_name}_PDN_FULL.siw"
    case_app = None
    case_runtime = []

    def _safe_name(text, extra=('_',)):
        return "".join(c for c in str(text or "") if c.isalnum() or c in extra).strip()

    try:
        case_app = SIwave(version=aedt_version, logger=logger)
        case_app.open_project(str(ref_siwave_file_path))

        for idx, case in enumerate(cases):
            original_net_name = case.get('Net', '')
            ic_designator = case.get('IC', '')
            src_comp_name = case.get('Source_name', '')
            full_net_chain = case.get('Full_Net_Chain', case.get('Source_net_chain', []) + [original_net_name])
            best_net_name = SIwave.get_representative_net_name(full_net_chain)
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
                continue

            try:
                inductor_prefix = conf_data.get('PDN', {}).get('inductorPrefix', 'L')
                pos_coord, pos_layer, neg_coord, neg_layer, src_name = case_data_app.prepare_vrm_connection(
                    target_net=original_net_name, source_name=src_comp_name, source_pin=case.get('Source_pin'),
                    gnd_net=gnd_net, net_chain=full_net_chain, inductor_prefix=inductor_prefix, bulk_inductor_list=bulk_inductor_list,
                )
                if pos_coord is None or neg_coord is None:
                    set_case_error_defaults(case)
                    continue

                if src_name and "Inductor_" in src_name:
                    inductor_refdes = src_name.split("Inductor_")[-1]
                    try: case_app.delete_circuit_element(inductor_refdes)
                    except Exception: pass

                v_mag, i_mag = case.get('Vmag', 0.0), case.get('Imag', 0.0)
                case_app.place_voltage_source(
                    v_port_name, pos_coord, pos_layer, neg_coord, neg_layer,
                    conf_data['PDN']['setup']['Vsource_Res'], v_mag
                )

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
                case['is_done'] = True
            except Exception:
                set_case_error_defaults(case)

        if conf_data['PDN'].get('doValchk', False):
            case_app.oproject.ScrRunValidationCheck()
        case_app.oproject.ScrSetSimulationName('dc', f'PDN - {model_name} - FULL')
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

# region Write Headers & 0\~2. Initialize
try:
    if MODE == 2: incubator()
    START_TIME = time.strftime('%Y.%m.%d, %H:%M:%S')
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
        post_state = run_standalone_post(conf_manager, INPUT_JSON, OUTPUT_DIR)
        END_TIME = time.strftime('%Y.%m.%d, %H:%M:%S')
        complete_count = sum(1 for case in post_state.summary if case.get('is_done'))
        if complete_count == 0:
            raise PostStageError("Standalone Post failed: no completed Local result was detected")
        logger.log(f"Standalone Post completed: {complete_count}/{len(post_state.summary)} cases", level=LogLevel.DETAIL1)
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
    
    # [수정] DCIR용 Pmap 제거 및 PDN 필수 요소만 유지
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
            else:
                DSGN_FILE = None

            ZUKEN_BIN_DIR = Path(conf_manager.data['PDN']['DF_path'])
            pcb_files = list(INPUT_CAD_FILE.parent.glob('*.pcb'))
            if len(pcb_files) == 1: PCB_FILE = pcb_files[0]
            else: raise PDNSessionException(ErrorCode.INVALID_PCB_FILE_NUM, pcb_files)

            if not temp_preconv:
                CR5_EXEC = ZUKEN_BIN_DIR / 'DFevolv.cr5.exe'
                result = run_external_tool([str(CR5_EXEC), str(PCB_FILE.parent)], "DFevolv.cr5.exe")
                if result.returncode: raise PDNSessionException(ErrorCode.CONVERT_PCB_TO_DSGN_FAIL, result.returncode)
                DSGN_FILE = PCB_FILE.with_suffix('.dsgn')

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
    
    pdn_cases_info = []
    inner_cap_audit = []
    inner_cap_net_lookup = {}
    designator_list = {case['Designator'] for case in spec_info}
    
    time.sleep(3.0)

    def normalize_name(name): return re.sub(r'[^A-Za-z0-9]', '', str(name)).upper()
    def normalize_pin_name(pin_name): return re.sub(r'[^A-Za-z0-9]', '', str(pin_name or "")).upper()
    def normalize_net_name(net_name): return re.sub(r'[^A-Za-z0-9+]', '', str(net_name or "").strip()).upper()

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
                    
                power_net_name = pin_inst.net_name
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

    for idx, case in enumerate(spec_info):
        comp_name = case['Designator']
        norm_comp_name = normalize_name(comp_name)
        comp_inst = get_component_by_normalized(norm_comp_name)
        if comp_inst is None: continue

        target_pin_name = str(case['Pin_number']).strip()
        pin_inst, actual_pin_name = comp_inst.pins.get(target_pin_name), target_pin_name
        if not pin_inst: continue

        db.other_nets[pin_inst.net.name] = []
        result, net_chain = find_power_source(app.edb, pin_inst.net, designator_list, bom_info, GND_NET, target_ic=comp_name)
        if isinstance(result, ErrorCode): net_chain = []

        source_pin_name = next((p_name for s_net in reversed(net_chain + [pin_inst.net.name]) 
                              for p_name, p_inst in result.pins.items() if p_inst.net_name == s_net), None) if not isinstance(result, ErrorCode) and result else None

        full_chain = []
        for n in (net_chain + [pin_inst.net.name]):
            if n not in full_chain: full_chain.append(n)

        vmag_default = extract_voltage(pin_inst.net.name) or 1.0
        vmag = parse_numeric(case.get('Voltage_(V)'), vmag_default)
        imag = parse_numeric(case.get('Current_(A)'), 1.0)
        min_spec = parse_numeric(case.get('Min_Spec_(V)'), 0.0)
        max_spec = parse_numeric(case.get('Max_Spec_(V)'), max(vmag * 1.2, vmag + 0.1))

        pdn_cases_info.append({
            'IC': comp_name, 'IC_pin': actual_pin_name, 'Net': pin_inst.net.name,
            'Source_name': result.name if not isinstance(result, ErrorCode) else "",
            'Source_pin': source_pin_name, 'Source_net_chain': net_chain, 
            'Full_Net_Chain': full_chain, 'Vmag': vmag, 'Imag': imag,
            'MinSpec': min_spec, 'MaxSpec': max_spec
        })

    target_nets = {GND_NET} | {n for case in pdn_cases_info for n in case.get('Full_Net_Chain', [])}
    if not app.sanitize_nets(target_nets): raise PDNSessionException(ErrorCode.SANITIZE_FAIL)

    PRE_EDB_FILE_PATH = EDB_FILE_PATH.parent / f"{EDB_FILE_PATH.stem}_1{EDB_FILE_PATH.suffix}"
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

# region PRE Stage Execution (PDN 전용 워크플로우)
if STAGE == "pre":
    # 1. PDN 해석용 필수 입력 파일 및 경로 설정 (DCIR 관련 Pmap 제거)
    spec_input = input_valchk._default_inputFiles.get('Spec') if input_valchk else None
    spec_file_for_report = Path(spec_input) if spec_input else INPUT_JSON
    stackup_input = STACKUP_EFFECTIVE_FILE if STACKUP_EFFECTIVE_FILE else (Path(STACKUP_INPUT_FILE) if STACKUP_INPUT_FILE else None)

    # 2. PDN SIwave 프로젝트 스냅샷 생성
    try:
        if stackup_input:
            logger.log(f"Using Stackup file for PDN pre-stage: {stackup_input}", level=LogLevel.INFO)
            
        build_pre_stage_siw_snapshot(
            aedt_version=AEDT_VERSION,
            pre_edb_path=PRE_EDB_FILE_PATH,
            siw_output_path=SIwave_FILE_PATH, # outputs 폴더 하위의 .siw 경로 전달
            conf_data=conf_manager.data,
            settings_data=settings_manager.data,
            input_dir=INPUT_DIR,
            working_dir=WORKING_DIR,
            stackup_input=Path(stackup_input) if stackup_input else None,
            layer_count=STACKUP_LAYER_COUNT,
            bom_info=bom_info,
            inner_cap_audit=inner_cap_audit,
            gnd_net=GND_NET,
            output_dir=OUTPUT_DIR,
            logger=logger,
            import_stackup_to_snapshot=(not STACKUP_APPLIED_AT_PROJECT_CREATION),
        )
        
        # [수정] 파일 생성 검증 로직 추가
        if not SIwave_FILE_PATH.exists():
            raise FileNotFoundError(f"SIwave file was not created at {SIwave_FILE_PATH}")
            
    except Exception:
        logger.fatal(f"Failed to build PDN pre-stage SIW snapshot: {traceback.format_exc()}")
        raise SystemExit(1)

    # 3. PDN 전용 리포트 추출
    try:
        logger.log(f"Step {step}. PRE report export (PDN setup/report steps only)", level=LogLevel.INFO)
        export_pre_stage_reports(
            output_dir=OUTPUT_DIR,
            spec_file=spec_file_for_report,
            pre_edb_path=PRE_EDB_FILE_PATH,
            cases=pdn_cases_info,
            inner_caps=inner_cap_audit,
            logger=logger,
            s2p_dir=None,
        )
    except Exception:
        logger.fatal(f"Failed to export PDN pre-stage reports: {traceback.format_exc()}")
        raise SystemExit(1)

    END_TIME = time.strftime('%Y.%m.%d, %H:%M:%S')
    logger.log(f"PDN Pre-stage completed successfully at {END_TIME}", level=LogLevel.INFO)
    raise SystemExit(0)
# endregion

# region 5. Modify CAD Data using SIwave and Set PDN Simulation
try:
    logger.log(f"Step {step}. CAD Modification", level=LogLevel.INFO)
    if not wait_for_edb_ready(PRE_EDB_FILE_PATH, timeout=300.0, check_interval=3.0):
        raise FileNotFoundError(f"Target EDB path or edb.def is not ready after retries: {PRE_EDB_FILE_PATH}")

    app = None
    image_app = None

    try:
        app = SIwave(version=AEDT_VERSION, logger=logger)
        app.import_edb(str(PRE_EDB_FILE_PATH))
        edb_ops_app = EDB_SETUP_APP if (EDB_SETUP_APP and getattr(EDB_SETUP_APP, "edb", None)) else app
        if edb_ops_app is app and not getattr(app, "edb", None):
            app.set_cad_file(str(PRE_EDB_FILE_PATH))
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
        if stackup_input and not STACKUP_APPLIED_AT_PROJECT_CREATION:
            raw_stackup = Path(stackup_input)
            if app.import_layer_stackup(raw_stackup):
                logger.log(f"[PRE] Raw stackup imported: {raw_stackup}", level=LogLevel.DETAIL1)
            else:
                raise RuntimeError(f"[PRE] Failed to import raw stackup file: {raw_stackup}")
        elif STACKUP_APPLIED_AT_PROJECT_CREATION and stackup_input:
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

        # [수정] Pmap 인자 제거 및 동적 주파수 셋업 적용
        app.setup_simulation(None, SWS_FILE, None) 
        apply_dynamic_frequency_setup(app, STACKUP_LAYER_COUNT, conf_manager.data, logger)

        # [수정] REF_SIwave_FILE_PATH를 SIwave_FILE_PATH와 동일하게 지정하여 outputs 폴더에 저장
        REF_SIwave_FILE_PATH = SIwave_FILE_PATH
        app.save_project_as(REF_SIwave_FILE_PATH)
        
        # [수정] 파일 생성 검증 로직 추가
        if not REF_SIwave_FILE_PATH.exists():
            raise FileNotFoundError(f"SIwave file was not created at {REF_SIwave_FILE_PATH}")

        base_cad_name = INPUT_CAD_FILE.stem.split('-')[0]
        FINAL_EDB_FILE_PATH = OUTPUT_DIR / f"{base_cad_name}_ref.aedb"
        app.export_edb(FINAL_EDB_FILE_PATH)
    finally:
        if app:
            safe_close_edb_session(app, logger, "step5-main")
            app.quit_application()
        if EDB_SETUP_APP:
            safe_close_edb_session(EDB_SETUP_APP, logger, "step5-setup-app")
            EDB_SETUP_APP.quit_application()
            EDB_SETUP_APP = None

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

    step += 1

except Exception:
    logger.fatal(f"An error occurred while CAD modification process : {traceback.format_exc()}")
    raise SystemExit(1)
# endregion

# region 6. Generate Files and Run PDN Setup
app = None
try:
    logger.log(f"Step {step}. Generate Files and Run PDN Setup (Unified Flow)", level=LogLevel.INFO)
    MODEL_NAME = INPUT_CAD_FILE.stem.split('-')[0]
    siw_execute_file = resolve_siwave_executable(AEDT_VERSION)
    exec_file = WORKING_DIR / 'core' / 'PDN.exec'
    case_data_app = EDB_SETUP_APP if EDB_SETUP_APP else app
    if not case_data_app:
        app = SIwave(version=AEDT_VERSION, logger=logger)
        app.set_cad_file(str(PRE_EDB_FILE_PATH))
        case_data_app = app

    signal_layers = list(case_data_app.edb.stackup.signal_layers.keys())
    preprocessing_data = run_pdn_unified(
        cases=pdn_cases_info,
        model_name=MODEL_NAME,
        output_dir=OUTPUT_DIR,
        ref_siwave_file_path=REF_SIwave_FILE_PATH,
        ref_edb_path=PRE_EDB_FILE_PATH,
        gnd_net=GND_NET,
        aedt_version=AEDT_VERSION,
        case_data_app=case_data_app,
        signal_layers=signal_layers,
        conf_data=conf_manager.data,
        siw_execute_file=siw_execute_file,
        exec_file=exec_file,
        bulk_inductor_list=bom_info.get('bulkInd', []),
        run_solve=(STAGE != "pre"),
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
else:
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
# endregion
