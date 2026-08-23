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
# endregion

# region Terminate & Save Log (atexit 등록)
def terminate_and_save_log():
    try:
        logger.log("#" * 100, level=LogLevel.SECTION, line_change=False)
        logger.log("|", level=LogLevel.SECTION, line_change=False)
        logger.log(f"| Terminate Ansys {UTILITY_NAME} v{VERSION} on {time.strftime('%Y.%m.%d, %H:%M:%S')}", level=LogLevel.SECTION, line_change=False)
        logger.log("|", level=LogLevel.SECTION, line_change=False)
        logger.log("#" * 100, level=LogLevel.SECTION, line_change=False)
        
        # [개선 사항 반영 4] 좀비 프로세스 강제 종료 로직 추가
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
# =========================================================================================
# [개선 사항 반영 5] 동적 주파수 셋업(Config 기반) 로직 추가
# =========================================================================================
def apply_dynamic_frequency_setup(app, layer_count, conf_data, logger):
    """
    Layer 수에 따라 config.json(conf_data)에서 알맞은 주파수 설정 문자열을 읽어와
    SIwave 2025.2 API(add_sweep)를 통해 동적으로 셋업을 생성합니다.
    """
    try:
        current_profile = "2L" if (layer_count is None or layer_count <= 2) else "4L_plus"
        
        # 통합된 config.json 경로 반영
        layer_cfg = conf_data.get("PDN", {}).get("zParamSetup", {}).get("profiles", {}).get(current_profile, {})
        
        setup_name = layer_cfg.get("setup_name", f"SYZ_Setup_{current_profile}")
        
        # SIwave 전용 API로 SYZ 셋업 생성
        setup = app.create_syz_setup(name=setup_name)
        
        sweep_data = layer_cfg.get("sweep_data", "")
        if not sweep_data:
            logger.log(f"[PRE] sweep_data가 비어있습니다. Config 파일을 확인해주세요.", level=LogLevel.WARNING)
            return

        tokens = sweep_data.split()
        
        # 4개의 토큰(Type, Start, Stop, Count) 단위로 읽어 2025.2 API에 적용
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
# =========================================================================================

def safe_close_edb_session(edb_app, logger, context=""):
    if not edb_app:
        return
    try:
        if hasattr(edb_app, "close_edb"):
            edb_app.close_edb()
    except Exception as e:
        ctx = f" ({context})" if context else ""
        logger.log(f"[WARNING] Failed to close EDB session{ctx}: {e}", level=LogLevel.WARNING)

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
                    logger.log(f"파일 삭제 완료: {path}", level=LogLevel.DETAIL2)
                elif os.path.isdir(path):
                    shutil.rmtree(path)
                    logger.log(f"폴더 삭제 완료: {path}", level=LogLevel.DETAIL2)
            except Exception as e:
                logger.log(f"삭제 실패: {path} - {e}", level=LogLevel.WARNING)

def resolve_temp_preconverted_files(
    input_cad_file: Path,
    logger: Logger,
):
    use_preconverted = os.environ.get("PDN_USE_PRECONVERTED", "").strip().lower() in {"1", "true", "y", "yes", "on"}
    if not use_preconverted:
        return None

    work_dir = input_cad_file.parent
    base_stem = input_cad_file.stem.split('-')[0]

    preferred_dsgn = work_dir / f"{input_cad_file.stem}.dsgn"
    if preferred_dsgn.exists():
        dsgn_file = preferred_dsgn
    else:
        dsgn_candidates = sorted(work_dir.glob("*.dsgn"), key=lambda p: p.stat().st_mtime, reverse=True)
        dsgn_file = dsgn_candidates[0] if dsgn_candidates else None

    preferred_edb = work_dir / f"{base_stem}.aedb"
    if preferred_edb.exists():
        edb_file = preferred_edb
    else:
        edb_candidates = sorted(work_dir.glob("*.aedb"), key=lambda p: p.stat().st_mtime, reverse=True)
        edb_file = edb_candidates[0] if edb_candidates else None

    if not dsgn_file and not edb_file:
        logger.log(
            "[TEMP] PDN_USE_PRECONVERTED=1 but no preconverted .dsgn/.aedb found. Continue normal conversion flow.",
            level=LogLevel.WARNING,
        )
        return None

    logger.log("[TEMP] Preconverted artifacts detected. Zuken conversion will be skipped.", level=LogLevel.WARNING)
    if dsgn_file:
        logger.log(f"[TEMP] Using DSGN: {dsgn_file}", level=LogLevel.DETAIL1)
    if edb_file:
        logger.log(f"[TEMP] Using EDB : {edb_file}", level=LogLevel.DETAIL1)

    return {
        "DSGN_FILE": dsgn_file,
        "EDB_FILE_PATH": edb_file,
        "SKIP_ZUKEN_CONVERT": True,
    }

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
    if result.stdout and result.stdout.strip():
        logger.log(f"[TOOL][{tool_name}][stdout]\n{result.stdout.strip()}", level=LogLevel.DETAIL2)
    if result.stderr and result.stderr.strip():
        logger.log(f"[TOOL][{tool_name}][stderr]\n{result.stderr.strip()}", level=LogLevel.WARNING)
    return result

def ensure_pre_edb_saved(app, source_edb_path: Path, pre_edb_path: Path, max_retries: int = 2, timeout: float = 300.0):
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            if pre_edb_path.exists():
                shutil.rmtree(pre_edb_path, ignore_errors=True)

            logger.log(
                f"Saving PRE EDB (attempt {attempt}/{max_retries}): {pre_edb_path.name}",
                level=LogLevel.DETAIL2,
            )
            app.edb.save_as(str(pre_edb_path))
            app.close_edb()

            if wait_for_edb_ready(pre_edb_path, timeout=timeout):
                logger.log(
                    f"PRE EDB is ready: {pre_edb_path / 'edb.def'}",
                    level=LogLevel.DETAIL2,
                )
                return

            raise TimeoutError(
                f"edb.def not ready within {timeout:.1f}s after save_as: {pre_edb_path}"
            )
        except Exception as exc:
            last_error = exc
            logger.log(
                f"[WARNING] PRE EDB save attempt {attempt} failed: {exc}",
                level=LogLevel.WARNING,
            )
            if attempt < max_retries:
                try:
                    app.set_cad_file(source_edb_path)
                except Exception as reopen_exc:
                    logger.log(
                        f"[WARNING] Failed to reopen source EDB before retry: {reopen_exc}",
                        level=LogLevel.WARNING,
                    )
                time.sleep(5.0)
    raise FileNotFoundError(
        f"Failed to create a ready PRE EDB after {max_retries} attempts: {pre_edb_path}. "
        f"Last error: {last_error}"
    )

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
    logger.log(f"[PDN][NetSearch] Net: {net_inst.name} | Chain: {net_chain}", level=LogLevel.DETAIL2)
    LDO_list, inductors, fet_list, switch_list, source_list = find_power_components_in_net(net_inst, designator_list, bom_info)
    found_sources = []
    for src in source_list:
        found_sources.append({'type': 'SOURCE', 'inst': src, 'chain': net_chain, 'comp_chain': comp_chain})
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
        jump_pins, base_pin_name = None, None
        if connect_types_upper and len(matched_pins) >= 2:
            base_type = next((c_type for c_type, p_inst in matched_pins.items() if p_inst.net_name == net_inst.name), None)
            if base_type:
                jump_pins = [p_inst for c_type, p_inst in matched_pins.items() if c_type != base_type]
                base_pin_name = matched_pins[base_type].name
        elif len(pin_match_pins) >= len(target_pair) and target_pair:
            base_pin_inst = next((p_inst for p_inst in pin_match_pins if p_inst.net_name == net_inst.name), None)
            if base_pin_inst:
                jump_pins = [p_inst for p_inst in pin_match_pins if p_inst != base_pin_inst]
                base_pin_name = base_pin_inst.name
        if jump_pins:
            for other_pin_inst in jump_pins:
                other_net_name = other_pin_inst.net_name
                if other_net_name and other_net_name not in (net_inst.name, gndNet) and other_net_name in edb._nets.nets:
                    if other_net_name not in net_chain:
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
                if other_pin_inst and other_pin_inst.net_name and other_pin_inst.net_name not in (net_inst.name, gndNet) and other_pin_inst.net_name in edb._nets.nets:
                    if other_pin_inst.net_name not in net_chain:
                        candidate_nets[other_pin_inst.net_name] = edb._nets.nets[other_pin_inst.net_name]
        for other_net_name, other_net_inst in candidate_nets.items():
            add_to_other_net(net_inst.name, other_net_name)
            upstream_sources = trace_pdn_power_path(edb, other_net_inst, designator_list, bom_info, gndNet, pre_bead=None, net_chain=net_chain + [other_net_name], comp_chain=comp_chain + [f"{fet_inst.name}(FET)"], target_ic=target_ic)
            found_sources.extend(upstream_sources)
    if pre_bead:
        inductors["bead"] = [bead for bead in inductors["bead"] if bead.name != pre_bead.name]
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
            if name in bom.get('sourceComp', []):
                source_list.append(inst)
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
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return default
    if text[0] in ("<", ">"):
        text = text[1:].strip()
    text = text.replace("V", "").replace("A", "").strip()
    try:
        return float(text)
    except Exception:
        return default

def find_nearest_gnd_pin(edb, ref_coord, gnd_net):
    # [개선 사항 반영 1] 좌표가 없거나 (0.0, 0.0)인 경우 Fallback 로직 작동 및 안전한 EDB 참조
    if not ref_coord or (math.isclose(ref_coord[0], 0.0, abs_tol=1e-9) and math.isclose(ref_coord[1], 0.0, abs_tol=1e-9)):
        logger.log(f"[WARNING] 참조 좌표가 유효하지 않거나 (0,0)입니다. 임의의 GND 핀을 기본값으로 할당합니다.", level=LogLevel.WARNING)
        components_dict = getattr(edb, 'core_components', getattr(edb, 'components', getattr(edb, '_components', None)))
        if components_dict and hasattr(components_dict, 'components'):
            for comp in components_dict.components.values():
                for pin in comp.pins.values():
                    if pin.net_name == gnd_net:
                        return pin
        return None

    min_dist = float("inf")
    best_pin = None
    components_dict = getattr(edb, 'core_components', getattr(edb, 'components', getattr(edb, '_components', None)))
    if components_dict and hasattr(components_dict, 'components'):
        for comp in components_dict.components.values():
            for pin in comp.pins.values():
                if pin.net_name != gnd_net:
                    continue
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
    pmap_file: Path | None = None,
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
    export_innercap_s2p_registry(output_dir, inner_caps, pmap_file, logger, s2p_dir=s2p_dir)

def _normalize_token(text):
    return pdn_setup_utils._normalize_token(text)

def parse_pmap_lge_to_maker(pmap_file: Path | None):
    return pdn_setup_utils.parse_pmap_lge_to_maker(pmap_file)

def resolve_maker_part_number(part_number: str, lge_to_maker: dict):
    return pdn_setup_utils.resolve_maker_part_number(part_number, lge_to_maker)

def find_s2p_file_for_maker(maker_part_number: str, s2p_dir: Path | None):
    return pdn_setup_utils.find_s2p_file_for_maker(maker_part_number, s2p_dir)

def detect_s2p_port_count(s2p_file: Path | str):
    return pdn_setup_utils.detect_s2p_port_count(s2p_file)

def export_innercap_s2p_registry(output_dir: Path, inner_caps, pmap_file: Path | None, logger: Logger, s2p_dir: Path | None = None):
    return pdn_setup_utils.export_innercap_s2p_registry(output_dir, inner_caps, pmap_file, logger, s2p_dir=s2p_dir)

def _build_bom_lge_part_by_designator(bom_info):
    return pdn_setup_utils._build_bom_lge_part_by_designator(bom_info)

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
):
    if not _is_edb_ready(pre_edb_path):
        raise FileNotFoundError(f"PRE EDB is not ready for SIW snapshot: {pre_edb_path}")

    app = None
    try:
        app = SIwave(version=aedt_version, logger=logger)
        logger.log(f"[PRE] Import EDB for pre-ready SIW: {pre_edb_path}", level=LogLevel.DETAIL1)
        app.import_edb(str(pre_edb_path))
        # Keep EDB handle valid for model-assignment path that reads app.edb.
        app.set_cad_file(pre_edb_path)

        pmap_name = settings_data.get('CAE', {}).get('PCB', {}).get('Pmap')
        pmap_file = (input_dir / pmap_name) if pmap_name else None

        profile_key, sws_name, sfsdf_name = resolve_zparam_profile(
            conf_data,
            stackup_input,
            layer_count,
        )
        if stackup_input:
            effective_stackup = prepare_stackup_for_project(app, Path(stackup_input), output_dir, logger)
            if app.import_layer_stackup(effective_stackup):
                logger.log(f"[PRE] Stackup reapplied to SIW project: {effective_stackup}", level=LogLevel.DETAIL1)
            else:
                logger.log(f"[PRE][WARNING] Failed to reapply stackup: {effective_stackup}", level=LogLevel.WARNING)
        sws_file = working_dir / 'core' / sws_name
        sfsdf_file = (working_dir / 'core' / sfsdf_name) if sfsdf_name else None
        logger.log(
            f"[PRE] SIW setup profile={profile_key}, SWS={sws_file.name}, SFSDF={(sfsdf_file.name if sfsdf_file else '')}",
            level=LogLevel.DETAIL1,
        )

        s2p_dir_conf = conf_data.get('PDN', {}).get('sParameter', {}).get('s2pDirectory', '')
        s2p_dir = None
        if s2p_dir_conf:
            candidate_dir = Path(s2p_dir_conf)
            if not candidate_dir.is_absolute():
                candidate_dir = input_dir / candidate_dir
            s2p_dir = candidate_dir

        if conf_data.get('PDN', {}).get('sParameter', {}).get('enableAssign', True):
            innercap_name = settings_data.get('CAE', {}).get('SOC', {}).get('Inner_cap')
            innercap_csv_path = (input_dir / innercap_name) if innercap_name else None
            bom_name = settings_data.get('CAE', {}).get('PCB', {}).get('BOM')
            bom_file_path = (input_dir / bom_name) if bom_name else None
            model_lib_candidates = conf_data.get('PDN', {}).get('sParameter', {}).get('model_library_dir_candidates', [])
            resolved_candidates = []
            for cand in model_lib_candidates:
                if not cand:
                    continue
                cpath = Path(cand)
                if not cpath.is_absolute():
                    cpath = input_dir / cpath
                resolved_candidates.append(cpath)
            assign_sparameter_models(
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
                search_roots=[input_dir, working_dir, output_dir] + resolved_candidates,
            )

        # =========================================================================================
        # [개선 사항 반영 5] 기존 SFSDF 임포트 대신 동적 주파수 셋업 함수 호출
        # =========================================================================================
        app.setup_simulation(pmap_file, sws_file, None) # sfsdf_file 대신 None 전달
        apply_dynamic_frequency_setup(app, layer_count, conf_data, logger)
        # =========================================================================================
        
        app.save_project_as(siw_output_path)
        logger.log(f"[PRE] Pre-ready SIW generated: {siw_output_path}", level=LogLevel.INFO)
        return siw_output_path
    finally:
        if app:
            safe_close_edb_session(app, logger, "build_pre_stage_siw_snapshot")
            app.quit_application()

def prepare_stackup_for_project(app, stackup_path: Path | None, work_dir: Path, logger: Logger):
    """
    Normalize .stk layer names to the currently opened EDB layer-name casing.
    Prevents silent stackup-import mismatch when names differ only by case.
    """
    if not stackup_path or not Path(stackup_path).exists():
        return stackup_path

    try:
        src_text = Path(stackup_path).read_text(encoding="utf-8-sig", errors="ignore")
    except Exception:
        return stackup_path

    try:
        edb_layers = list(app.edb.stackup.signal_layers.keys()) if app and app.edb else []
    except Exception:
        edb_layers = []
    if not edb_layers:
        return stackup_path

    case_map = {str(name).upper(): str(name) for name in edb_layers}
    changed = False

    def _replace_layer_name(match):
        nonlocal changed
        original = match.group(1)
        mapped = case_map.get(str(original).upper(), original)
        if mapped != original:
            changed = True
        return f"LayerName='{mapped}'"

    patched_text = re.sub(r"LayerName='([^']+)'", _replace_layer_name, src_text)
    if not changed:
        return stackup_path

    patched_path = work_dir / f"{Path(stackup_path).stem}_autocase.stk"
    try:
        patched_path.write_text(patched_text, encoding="utf-8")
        logger.log(f"[ZParam] Auto-cased stackup for layer-name match: {patched_path}", level=LogLevel.DETAIL1)
        return patched_path
    except Exception as e:
        logger.log(f"[ZParam][WARNING] Failed to write auto-cased stackup: {e}", level=LogLevel.WARNING)
        return stackup_path


def sanitize_name_token(value):
    return re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "").strip()).strip("_")

def get_component_pins_by_net(comp_inst, net_name):
    return [pin for pin in comp_inst.pins.values() if pin.net_name == net_name]

def get_component_pin_names_by_net(comp_inst, net_name):
    return [pin_name for pin_name, pin in comp_inst.pins.items() if pin.net_name == net_name]

def _create_pin_group_with_compat(edb, pins, pin_names, group_name):
    candidates = []
    if hasattr(edb, "core_components") and hasattr(edb.core_components, "create_pingroup"):
        candidates.append(edb.core_components.create_pingroup)
    if hasattr(edb, "components") and hasattr(edb.components, "create_pingroup"):
        candidates.append(edb.components.create_pingroup)
    if hasattr(edb, "siwave") and hasattr(edb.siwave, "create_pin_group"):
        candidates.append(edb.siwave.create_pin_group)

    last_exc = None
    for creator in candidates:
        for payload in (pins, pin_names):
            for args in ((payload, group_name), (group_name, payload)):
                try:
                    return creator(*args)
                except Exception as exc:
                    last_exc = exc
    if last_exc:
        raise last_exc
    raise RuntimeError("No Pin Group API available")

def _create_port_with_compat(edb, pos_obj, gnd_obj, port_name):
    # 1) pin-group path
    if hasattr(edb, "ports") and hasattr(edb.ports, "create_port_between_pin_groups"):
        for args in (
            (pos_obj, gnd_obj),
            (pos_obj, gnd_obj, port_name),
        ):
            try:
                if len(args) == 2:
                    return edb.ports.create_port_between_pin_groups(*args, name=port_name)
                return edb.ports.create_port_between_pin_groups(*args)
            except Exception:
                pass
    # 2) pin-to-pin direct path
    if hasattr(edb, "ports") and hasattr(edb.ports, "create_port_between_pins"):
        for args in (
            (pos_obj, gnd_obj),
            (pos_obj, gnd_obj, port_name),
        ):
            try:
                if len(args) == 2:
                    return edb.ports.create_port_between_pins(*args, name=port_name)
                return edb.ports.create_port_between_pins(*args)
            except Exception:
                pass
    raise RuntimeError("Port creation API failed for both pin-group and pin-to-pin paths")

def find_series_inductor_on_chain(edb, net_chain, bulk_inductor_set, allowed_prefixes=None):
    if not net_chain or len(net_chain) < 2:
        return None, None, None
    allowed_prefixes = tuple(allowed_prefixes or ())
    for idx in range(len(net_chain) - 1):
        net_a, net_b = net_chain[idx], net_chain[idx + 1]
        for comp_name, comp_inst in edb._components.components.items():
            if allowed_prefixes and not str(comp_name).upper().startswith(tuple(p.upper() for p in allowed_prefixes)):
                continue
            if bulk_inductor_set and comp_name not in bulk_inductor_set:
                continue
            comp_nets = {p.net_name: p for p in comp_inst.pins.values() if p.net_name}
            if net_a in comp_nets and net_b in comp_nets:
                return comp_inst, comp_nets[net_a], comp_nets[net_b]
    return None, None, None

def find_nearest_shunt_cap_pin(edb, ref_coord, target_net, gnd_net):
    best_pin = None
    min_dist = float("inf")
    for comp_name, comp_inst in edb._components.components.items():
        if not str(comp_name).upper().startswith("C"):
            continue
        target_pins = [p for p in comp_inst.pins.values() if p.net_name == target_net]
        gnd_pins = [p for p in comp_inst.pins.values() if p.net_name == gnd_net]
        if not target_pins or not gnd_pins:
            continue
        for pin in target_pins:
            dist = (pin.position[0] - ref_coord[0]) ** 2 + (pin.position[1] - ref_coord[1]) ** 2
            if dist < min_dist:
                min_dist = dist
                best_pin = pin
    return best_pin

def clear_vrm_setup_artifacts(app, logger, prefixes=None):
    prefixes = tuple(prefixes or ("PORT_", "Rvrm_"))
    for comp_name in list(app.edb._components.components.keys()):
        if comp_name.startswith(prefixes):
            try:
                app.edb._components.components[comp_name].delete()
            except Exception as e:
                logger.log(f"[WARNING] Failed to clear previous setup artifact {comp_name}: {e}", level=LogLevel.WARNING)

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
    if abs(v_mag) < 1e-12:
        case['Drop Rate'] = 0.0
    else:
        case['Drop Rate'] = round((case['Drop Voltage'] / v_mag) * 100, 3)

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

def run_standalone_post(
    conf_manager,
    input_json,
    output_dir,
    analysis_start=None,
    analysis_end=None,
):
    settings_manager = SettingsManager(input_json, configuration=conf_manager, logger=logger)
    state = reconstruct_post_state(output_dir)
    post_settings = prepare_post_settings(settings_manager.data)

    for index, history in enumerate(state.change_history, start=1):
        if history.get("Status") == "Complete":
            logger.log(
                f"Case #{index} result detected: {history.get('Project_File')} "
                f"(latest run {history.get('Latest_Run')})",
                level=LogLevel.DETAIL2,
            )
        else:
            logger.log(
                f"Case #{index} result detection failed: {history.get('Error', 'Unknown error')} "
                f"[folder: {history.get('Result_Folder')}]",
                level=LogLevel.ERROR,
            )

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

def run_pdn_case(
    case,
    idx,
    total_cases,
    mode,
    model_name,
    output_dir,
    ref_siwave_file_path,
    gnd_net,
    aedt_version,
    case_data_app,
    signal_layers,
    conf_data,
    siw_execute_file,
    exec_file,
    bulk_inductor_list=None,
    run_solve=True
):
    original_net_name = case['Net']
    ic_designator = case.get('IC', '')
    src_comp_name = case.get('Source_name', '')
    logger.log(f"[{idx + 1}/{total_cases}] Processing Case: IC={ic_designator}, Net={original_net_name}", level=LogLevel.DETAIL1)
    case['is_done'] = False

    if idx != 0 and mode == 1:
        return None

    safe_sanitize = lambda s, extra: "".join(c for c in str(s or "") if c.isalnum() or c in extra).strip()
    
    full_net_chain = case.get('Full_Net_Chain', case.get('Source_net_chain', []) + [original_net_name])
    
    best_net_name = SIwave.get_representative_net_name(full_net_chain)

    safe_net_name = safe_sanitize(best_net_name, (' ', '.', '_', '-', '+'))
    safe_ic_name = safe_sanitize(ic_designator, (' ', '.', '_', '-', '+'))
    safe_net_port = safe_sanitize(best_net_name, ('_',))

    net_siw_file = output_dir / f"{model_name}_{safe_ic_name}_{safe_net_name}.siw"
    net_edb_dir = output_dir / f"{model_name}_{safe_ic_name}_{safe_net_name}.aedb"
    v_mag, i_mag = case.get('Vmag', 0.0), case.get('Imag', 0.0)
    
    v_port_name, i_port_name = f"V_{safe_net_port}", f"I_{safe_net_port}"

    if not src_comp_name:
        logger.log("Invalid Case (No Source Found). Skipping simulation.", level=LogLevel.WARNING)
        set_case_error_defaults(case)
        return build_preprocessing_record(
            case, idx, net_siw_file, net_edb_dir, v_port_name, i_port_name, gnd_net
        )

    case_app = None
    try:
        case_app = SIwave(version=aedt_version)
        case_app.open_project(str(ref_siwave_file_path))

        inductor_prefix = conf_data.get('PDN', {}).get('inductorPrefix', 'L')

        pos_coord, pos_layer, neg_coord, neg_layer, src_name = case_data_app.prepare_vrm_connection(
            target_net=original_net_name,
            source_name=src_comp_name,
            source_pin=case.get('Source_pin'),
            gnd_net=gnd_net,
            net_chain=full_net_chain,        
            inductor_prefix=inductor_prefix,
            bulk_inductor_list=bulk_inductor_list
        )

        if pos_coord is None or neg_coord is None:
            logger.log(f"  -> [경고] 전압원 인가 실패: VRM 연결 좌표를 찾을 수 없어 해당 케이스를 건너뜁니다.", level=LogLevel.WARNING)
            set_case_error_defaults(case)
            return build_preprocessing_record(
                case, idx, net_siw_file, net_edb_dir, v_port_name, i_port_name, gnd_net
            )

        if src_name and "Inductor_" in src_name:
            inductor_refdes = src_name.split("Inductor_")[-1]
            try:
                case_app.delete_circuit_element(inductor_refdes)
                logger.log(f"  -> [SIwave] 인덕터 {inductor_refdes} 비활성화(삭제) 완료.", level=LogLevel.DETAIL2)
            except Exception as e:
                logger.log(f"  -> [경고] 인덕터 {inductor_refdes} 삭제 실패: {e}", level=LogLevel.WARNING)

        case_app.place_voltage_source(
            v_port_name, pos_coord, pos_layer,
            neg_coord, neg_layer,
            conf_data['PDN']['setup']['Vsource_Res'], v_mag
        )
        logger.log(f"  -> [Voltage Source] {v_port_name} ({src_name}) : {v_mag}V @ {pos_layer} to {neg_layer}", level=LogLevel.DETAIL2)

        ic_inst = case_data_app.edb._components.components.get(ic_designator)
        if ic_inst:
            ic_layer = ic_inst.placement_layer
            ic_pin_name = case.get('IC_pin')
            
            if ic_pin_name and ic_pin_name in ic_inst.pins:
                pos_coord = ic_inst.pins[ic_pin_name].position
                
                neg_coord, neg_layer = case_data_app.find_nearest_gnd(pos_coord, gnd_net)
                
                if not neg_coord:
                    neg_coord = pos_coord
                    target_layer_index = signal_layers.index(ic_layer)
                    if target_layer_index == 0:
                        neg_layer = signal_layers[1]
                    elif target_layer_index == len(signal_layers) - 1:
                        neg_layer = signal_layers[-2]
                    else:
                        neg_layer = signal_layers[target_layer_index + 1]

                try:
                    case_app.place_current_source(
                        i_port_name, pos_coord, ic_layer,
                        neg_coord, neg_layer,
                        conf_data['PDN']['setup']['Isource_Res'], i_mag
                    )
                    logger.log(f"  -> [Current Source] {i_port_name} : {i_mag}A @ {ic_designator} ({ic_layer} to {neg_layer})", level=LogLevel.DETAIL2)
                except Exception as e:
                    logger.log(f"  -> [경고] 전류원 인가 실패: {e}", level=LogLevel.WARNING)

        if conf_data['PDN'].get('doValchk', False):
            case_app.oproject.ScrRunValidationCheck()

        pdn_sim_name = f'PDN - {case["IC"]} : {best_net_name}'
        case_app.oproject.ScrSetSimulationName('dc', pdn_sim_name)
        
        case_app.save_project_as(net_siw_file)
        case['edb'] = net_edb_dir
        case_app.quit_application()
        case_app = None

        if run_solve:
            logger.log("  -> Running siwave_ng.exe...", level=LogLevel.DETAIL2)
            cmd = [str(siw_execute_file), str(net_siw_file), str(exec_file), '-formatOutput', '-useSubdir']
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode:
                logger.log(f"  -> [siwave_ng] command : {subprocess.list2cmdline(cmd)}", level=LogLevel.ERROR)
                logger.log(f"  -> [siwave_ng] returncode : {result.returncode}", level=LogLevel.ERROR)
                for stream_name, stream_text in (('stdout', result.stdout), ('stderr', result.stderr)):
                    text = (stream_text or "").strip()
                    if text:
                        logger.log(f"  -> [siwave_ng] {stream_name} :\n{text}", level=LogLevel.ERROR)
                raise PDNSessionException(ErrorCode.PDN_COMMAND_SIMULATION_FAIL, result.returncode)
            case['is_done'] = True

            logger.log("  -> Exporting PDN results...", level=LogLevel.DETAIL2)
            pdn_result_file_path = net_siw_file.with_suffix('.siwaveresults') / '0000' / '0000.ced'
            if not pdn_result_file_path.exists():
                raise PDNSessionException(ErrorCode.PDN_RESULT_NOT_FOUND, pdn_result_file_path)
            viewer_siw_file_path = pdn_result_file_path.with_suffix('.siw')
            if not viewer_siw_file_path.exists():
                raise PDNSessionException(ErrorCode.PDN_RESULT_NOT_FOUND, viewer_siw_file_path)
            case['_viewer_siw'] = viewer_siw_file_path

            with open(str(pdn_result_file_path), 'r') as f:
                lines = f.readlines()

            line_data = None
            for line in lines:
                if line.startswith(i_port_name):
                    line_data = line.split()
                    break

            if line_data and len(line_data) > 2:
                try:
                    drop_voltage = float(line_data[2])
                    apply_drop_metrics(case, drop_voltage)
                except ValueError:
                    case['Pass/Fail'] = 'Error'
                    set_case_error_defaults(case)
            else:
                set_case_error_defaults(case)

            safe_net = sanitize_str(best_net_name, ('_',))
            case['FitView'] = output_dir / f'{case["IC"]}_{safe_net}_FitView.jpg'
            case['ZoomView'] = output_dir / f'{case["IC"]}_{safe_net}_ZoomView.jpg'
            case['Field_Case'] = output_dir / f'Field_{case["IC"]}_{safe_net}.case'
            case['Mesh_Case'] = output_dir / f'Mesh_{case["IC"]}_{safe_net}.case'
        else:
            logger.log(
                "  -> Solve skipped (stage=pre). Case SIW generated; case AEDB is deferred to Post.",
                level=LogLevel.DETAIL2,
            )

    except Exception:
        logger.log(f"Failed to process case {net_siw_file.name}: {traceback.format_exc()}", level=LogLevel.ERROR)
        set_case_error_defaults(case)
    finally:
        if case_app:
            try:
                case_app.quit_application()
            except Exception:
                pass

    return build_preprocessing_record(
        case, idx, net_siw_file, net_edb_dir, v_port_name, i_port_name, gnd_net
    )

def run_pdn_unified(
    cases,
    model_name,
    output_dir,
    ref_siwave_file_path,
    ref_edb_path,
    gnd_net,
    aedt_version,
    case_data_app,
    signal_layers,
    conf_data,
    siw_execute_file,
    exec_file,
    bulk_inductor_list=None,
    run_solve=True,
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
                case=case,
                idx=idx,
                net_siw_file=full_siw_file,
                net_edb_dir=ref_edb_path,
                v_port_name=v_port_name,
                i_port_name=i_port_name,
                gnd_net=gnd_net,
            )
            preprocessing_data.append(record)
            case_runtime.append({"case": case, "record": record, "v_port": v_port_name, "i_port": i_port_name})

            if not src_comp_name:
                logger.log(f"[UNIFIED][SKIP] {ic_designator}:{original_net_name} - source not found", level=LogLevel.WARNING)
                set_case_error_defaults(case)
                continue

            try:
                inductor_prefix = conf_data.get('PDN', {}).get('inductorPrefix', 'L')
                pos_coord, pos_layer, neg_coord, neg_layer, src_name = case_data_app.prepare_vrm_connection(
                    target_net=original_net_name,
                    source_name=src_comp_name,
                    source_pin=case.get('Source_pin'),
                    gnd_net=gnd_net,
                    net_chain=full_net_chain,
                    inductor_prefix=inductor_prefix,
                    bulk_inductor_list=bulk_inductor_list,
                )
                if pos_coord is None or neg_coord is None:
                    logger.log(f"[UNIFIED][SKIP] {ic_designator}:{original_net_name} - VRM connection not found", level=LogLevel.WARNING)
                    set_case_error_defaults(case)
                    continue

                if src_name and "Inductor_" in src_name:
                    inductor_refdes = src_name.split("Inductor_")[-1]
                    try:
                        case_app.delete_circuit_element(inductor_refdes)
                    except Exception as e:
                        logger.log(f"[UNIFIED][WARN] Failed to delete inductor {inductor_refdes}: {e}", level=LogLevel.WARNING)

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
                            if target_layer_index == 0:
                                cur_neg_layer = signal_layers[1]
                            elif target_layer_index == len(signal_layers) - 1:
                                cur_neg_layer = signal_layers[-2]
                            else:
                                cur_neg_layer = signal_layers[target_layer_index + 1]
                        case_app.place_current_source(
                            i_port_name, cur_pos, ic_layer, cur_neg, cur_neg_layer,
                            conf_data['PDN']['setup']['Isource_Res'], i_mag
                        )
                case['is_done'] = True
            except Exception:
                logger.log(
                    f"[UNIFIED][ERROR] Failed to place source for {ic_designator}:{original_net_name}\n{traceback.format_exc()}",
                    level=LogLevel.WARNING,
                )
                set_case_error_defaults(case)

        if conf_data['PDN'].get('doValchk', False):
            case_app.oproject.ScrRunValidationCheck()
        case_app.oproject.ScrSetSimulationName('dc', f'PDN - {model_name} - FULL')
        case_app.save_project_as(full_siw_file)

        if run_solve:
            logger.log("[UNIFIED] Running full-project PDN solve", level=LogLevel.INFO)
            cmd = [str(siw_execute_file), str(full_siw_file), str(exec_file), '-formatOutput', '-useSubdir']
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode:
                logger.log(f"[UNIFIED][ERROR] siwave_ng failed rc={result.returncode}", level=LogLevel.ERROR)
                if (result.stdout or "").strip():
                    logger.log(f"[UNIFIED][stdout]\n{result.stdout.strip()}", level=LogLevel.ERROR)
                if (result.stderr or "").strip():
                    logger.log(f"[UNIFIED][stderr]\n{result.stderr.strip()}", level=LogLevel.ERROR)
                raise PDNSessionException(ErrorCode.PDN_COMMAND_SIMULATION_FAIL, result.returncode)

            ced_file = full_siw_file.with_suffix('.siwaveresults') / '0000' / '0000.ced'
            if not ced_file.exists():
                raise PDNSessionException(ErrorCode.PDN_RESULT_NOT_FOUND, ced_file)

            ced_map = {}
            with open(str(ced_file), 'r', encoding='utf-8', errors='ignore') as f:
                for line in f:
                    cols = line.split()
                    if cols:
                        ced_map[cols[0]] = cols

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
    step += 1
except Exception: logger.fatal(f"An error occurred while loading configurations: {traceback.format_exc()}")

if STAGE == "post":
    try:
        logger.log(f"Step {step}. Standalone Post-processing", level=LogLevel.INFO)
        post_state = run_standalone_post(conf_manager, INPUT_JSON, OUTPUT_DIR)
        END_TIME = time.strftime('%Y.%m.%d, %H:%M:%S')
        complete_count = sum(1 for case in post_state.summary if case.get('is_done'))
        if complete_count == 0:
            raise PostStageError(
                f"Standalone Post failed: no completed Local result was detected "
                f"(0/{len(post_state.summary)} cases). Check the case errors above and "
                f"{OUTPUT_DIR / 'result_detail.json'}"
            )
        logger.log(
            f"Standalone Post completed: {complete_count}/{len(post_state.summary)} cases",
            level=LogLevel.DETAIL1,
        )
    except Exception:
        logger.fatal(f"An error occurred while performing standalone Post-processing: {traceback.format_exc()}")
        raise SystemExit(1)
    raise SystemExit(0)

try:
    logger.log(f"Step {step}. Get Settings for PDN", level=LogLevel.INFO)
    settings_manager = SettingsManager(INPUT_JSON, configuration=conf_manager, logger=logger)

    original_spec_name = settings_manager.data.get('CAE', {}).get('SOC', {}).get('Spec', '')
    
    if original_spec_name:
        primary_spec_path = INPUT_DIR / original_spec_name
        
        if not primary_spec_path.exists():
            parts = original_spec_name.rsplit('_', 1) 
            base_name = parts[0] if len(parts) == 2 else original_spec_name.rsplit('.', 1)[0]
            
            fallback_spec_name = f"{base_name}_reference.csv"
            fallback_spec_path = INPUT_DIR / fallback_spec_name
            
            if not fallback_spec_path.exists():
                excel_found = False
                
                excel_candidates = [
                    (INPUT_DIR / f"{primary_spec_path.stem}{ext}", primary_spec_path) for ext in ['.xlsx', '.xls']
                ] + [
                    (INPUT_DIR / f"{fallback_spec_path.stem}{ext}", fallback_spec_path) for ext in ['.xlsx', '.xls']
                ]
                
                for excel_path, target_csv_path in excel_candidates:
                    if excel_path.exists():
                        logger.log(f"CSV not found. Found Excel file '{excel_path.name}'. Converting to CSV...", level=LogLevel.WARNING)
                        try:
                            import pandas as pd
                            df = pd.read_excel(excel_path)
                            df.to_csv(target_csv_path, index=False, encoding='utf-8-sig')
                            
                            logger.log(f"Successfully converted to '{target_csv_path.name}'.", level=LogLevel.DETAIL1)
                            settings_manager.data['CAE']['SOC']['Spec'] = target_csv_path.name
                            excel_found = True
                            break
                        except ImportError:
                            logger.log("[ERROR] 'pandas' or 'openpyxl' library is required to convert Excel to CSV.", level=LogLevel.ERROR)
                        except Exception as e:
                            logger.log(f"[ERROR] Failed to convert Excel to CSV: {e}", level=LogLevel.ERROR)
                
                if not excel_found:
                    logger.log(f"Both '{original_spec_name}' and '{fallback_spec_name}' (including Excel formats) not found.", level=LogLevel.ERROR)
            else:
                logger.log(f"Primary Spec '{original_spec_name}' not found. Fallback to '{fallback_spec_name}'", level=LogLevel.WARNING)
                settings_manager.data['CAE']['SOC']['Spec'] = fallback_spec_name

    original_bom_name = settings_manager.data.get('CAE', {}).get('PCB', {}).get('BOM', '')
    if original_bom_name:
        primary_bom_path = INPUT_DIR / original_bom_name
        
        if not primary_bom_path.exists() and primary_bom_path.suffix.lower() == '.csv':
            for ext in ['.xlsx', '.xls']:
                fallback_bom_path = primary_bom_path.with_suffix(ext)
                if fallback_bom_path.exists():
                    new_bom_name = original_bom_name.rsplit('.', 1)[0] + ext
                    logger.log(f"Primary BOM '{original_bom_name}' not found. Fallback to '{new_bom_name}'", level=LogLevel.WARNING)
                    settings_manager.data['CAE']['PCB']['BOM'] = new_bom_name
                    break

    input_valchk = InValChk(settings_manager.data, INPUT_DIR, logger)
    default, optional = input_valchk.is_valid()
    settings_manager.data['CAE']['PCB'].update({'cadFile': default['cadFile'], 'Stackup': default['Stackup'], 'BOM': default['BOM'], 'Pmap': optional['Pmap']})
    settings_manager.data['CAE']['SOC'].update({'Spec': default['Spec'], 'Inner_cap': optional['Inner_cap']})
    step += 1
except Exception: 
    logger.fatal(f"An error occurred while loading settings: {traceback.format_exc()}")   
# endregion 

# region 3. Get ECAD Data (PDN 수정 반영: ANF/CMP 추출 유지, EDB 직접 추출)
try:
    logger.log(f"Step {step}. Get ECAD Data", level=LogLevel.INFO)
    INPUT_CAD_FILE = INPUT_DIR / settings_manager.data['CAE']['PCB']['cadFile']
    EDB_FILE_PATH = None
    
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
                if result.returncode:
                    raise PDNSessionException(ErrorCode.CONVERT_PCB_TO_DSGN_FAIL, result.returncode)
                expected_dsgn = PCB_FILE.with_suffix('.dsgn')
                if expected_dsgn.exists():
                    DSGN_FILE = expected_dsgn
                else:
                    dsgn_candidates = sorted(INPUT_CAD_FILE.parent.glob('*.dsgn'), key=lambda p: p.stat().st_mtime, reverse=True)
                    if dsgn_candidates:
                        DSGN_FILE = dsgn_candidates[0]
                        logger.log(
                            f"[WARNING] Expected DSGN not found ({expected_dsgn.name}). Reusing latest DSGN: {DSGN_FILE.name}",
                            level=LogLevel.WARNING,
                        )
                    else:
                        dir_snapshot = [p.name for p in INPUT_CAD_FILE.parent.iterdir()]
                        logger.log(f"[DEBUG] Conversion output dir snapshot: {dir_snapshot}", level=LogLevel.WARNING)
                        raise PDNSessionException(
                            ErrorCode.INPUT_FILE_NOT_FOUND,
                            f"DSGN not found after DFevolv conversion. Expected: {expected_dsgn}",
                        )
            elif not DSGN_FILE:
                # keep flow robust when only EDB is preconverted
                DSGN_FILE = PCB_FILE.with_suffix('.dsgn')

            # [PDN 수정] ANF/CMP 변환 로직 복구 (파일 추출 목적)
            if conf_manager.data['PDN']['exportANF'] and not temp_preconv:
                DSGN2ANF_EXEC = ZUKEN_BIN_DIR / 'DFdsgn2anf.exe'
                ANF_FILE = DSGN_FILE.with_suffix('.anf')
                
                for attempt in range(MAX_RETRIES):
                    result = run_external_tool(
                        [str(DSGN2ANF_EXEC), '-r', str(DSGN_FILE), '-o', str(ANF_FILE)],
                        "DFdsgn2anf.exe",
                    )
                    
                    if result.returncode == 0:
                        logger.log("DSGN to ANF 변환 성공", level=LogLevel.DETAIL1)
                        break
                    else:
                        logger.log(f"[경고] 변환 실패 (시도 {attempt + 1}/{MAX_RETRIES}). Return Code: {result.returncode}", level=LogLevel.WARNING)
                        
                        if attempt < MAX_RETRIES - 1:
                            logger.log(f"라이선스 문제일 수 있으므로 {RETRY_DELAY}초 후 다시 시도합니다...", level=LogLevel.WARNING)
                            cleanup_failed_files(target_dir=str(INPUT_CAD_FILE.parent)) 
                            time.sleep(RETRY_DELAY)
                        else:
                            raise PDNSessionException(ErrorCode.CONVERT_DSGN_TO_ANF_FAIL, result.returncode)

                cmp_files = list(INPUT_CAD_FILE.parent.glob('*.cmp'))
                if len(cmp_files) == 1: CMP_FILE = cmp_files[0]
                else: raise PDNSessionException(ErrorCode.INVALID_CMP_FILE_NUM, cmp_files)

            if conf_manager.data['PDN']['exportODB'] and not temp_preconv:
                DSGN2ODB_EXEC = ZUKEN_BIN_DIR / 'DFodbout.exe'
                result = run_external_tool(
                    [str(DSGN2ODB_EXEC), '-r', str(DSGN_FILE), '-o', str(DSGN_FILE.parent)],
                    "DFodbout.exe",
                )
                make_tgz(INPUT_CAD_FILE.parent / INPUT_CAD_FILE.stem, logger=logger)
                if result.stderr.strip(): raise PDNSessionException(ErrorCode.CONVERT_DSGN_TO_ODB_FAIL, result.returncode)

            # [PDN 수정] EDB 직접 추출
            force_export_edb_in_temp = bool(temp_preconv) and (EDB_FILE_PATH is None) and bool(DSGN_FILE)
            force_export_edb_required = (EDB_FILE_PATH is None) and bool(DSGN_FILE)
            if force_export_edb_in_temp:
                logger.log(
                    "[TEMP] .aedb is missing. Forcing DSGN->EDB export even when config exportEDB=false.",
                    level=LogLevel.WARNING,
                )
            elif force_export_edb_required and not conf_manager.data['PDN']['exportEDB']:
                logger.log(
                    "[PDN] exportEDB=false in config, but EDB is required for stage pre. Forcing DSGN->EDB export.",
                    level=LogLevel.WARNING,
                )

            if EDB_FILE_PATH is None and (conf_manager.data['PDN']['exportEDB'] or force_export_edb_in_temp or force_export_edb_required):
                DSGN2EDB_EXEC = ZUKEN_BIN_DIR / 'DFaedbout.exe'
                EDB_FILE_PATH = DSGN_FILE.with_suffix('.aedb')
                result = run_external_tool(
                    [str(DSGN2EDB_EXEC), '-r', str(DSGN_FILE), '-o', str(EDB_FILE_PATH)],
                    "DFaedbout.exe",
                )
                if result.returncode:
                    raise PDNSessionException(ErrorCode.CONVERT_DSGN_TO_EDB_FAIL, result.returncode)
                if not EDB_FILE_PATH.exists():
                    raise PDNSessionException(
                        ErrorCode.INPUT_FILE_NOT_FOUND,
                        f"EDB export command finished but output not found: {EDB_FILE_PATH}",
                    )
                logger.log("Zuken에서 원본 EDB 직접 추출 완료", level=LogLevel.DETAIL1)

            if EDB_FILE_PATH is None:
                fallback_edb = DSGN_FILE.with_suffix('.aedb')
                if fallback_edb.exists():
                    EDB_FILE_PATH = fallback_edb
                    logger.log(f"[TEMP] Reusing existing EDB without conversion: {EDB_FILE_PATH}", level=LogLevel.WARNING)
                else:
                    raise PDNSessionException(
                        ErrorCode.INPUT_FILE_NOT_FOUND,
                        f"EDB file not found after conversion skip: {fallback_edb}",
                    )
        else:
            # Zuken이 아닌 경우 기존 파일 매핑
            anf_files = list(INPUT_CAD_FILE.parent.glob('*.anf'))
            cmp_files = list(INPUT_CAD_FILE.parent.glob('*.cmp'))
            if len(anf_files) == 1 and len(cmp_files) == 1: ANF_FILE, CMP_FILE = anf_files[0], cmp_files[0]
            else: raise PDNSessionException(ErrorCode.INVALID_ANF_FILE_NUM, anf_files)
            EDB_FILE_PATH = INPUT_CAD_FILE.with_suffix('.aedb')
    else:
        raise PDNSessionException(ErrorCode.INVALID_CAD_FILE, INPUT_CAD_FILE)

    # [PDN 수정] app.create_project(ANF_FILE, CMP_FILE, ...) 호출을 생략하여 
    # ANF/CMP를 통한 SIwave 프로젝트 생성을 방지합니다.
    base_name = INPUT_CAD_FILE.stem.split('-')[0]
    SIwave_FILE_PATH = INPUT_DIR / f'{base_name}.siw'

except Exception:
    logger.fatal(f"An error occurred while getting CAD database: {traceback.format_exc()}")
    raise PDNSessionException(ErrorCode.CAD_IMPORT_FAIL)
finally:
    step += 1
# endregion

# region 4. Modify CAD Data using EDB database (PDN 수정 반영: 커패시터 보존 및 EDB 직접 로드)
logger.log(f"Step {step}. CAD Modification using EDB database", level=LogLevel.INFO)
app = None
try:
    app = SIwave(version=AEDT_VERSION, logger=logger)
    
    # [PDN 수정] ANF/CMP로 만든 프로젝트가 아닌, 추출된 원본 EDB를 바로 엽니다.
    app.set_cad_file(EDB_FILE_PATH)
    try:
        STACKUP_LAYER_COUNT = len(app.edb.stackup.signal_layers.keys())
        logger.log(f"[ZParam] Detected signal layer count: {STACKUP_LAYER_COUNT}", level=LogLevel.DETAIL1)
    except Exception:
        STACKUP_LAYER_COUNT = None

    logger.log(f"Find Ground Net", level=LogLevel.DETAIL1)
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
        # [PDN 수정] 커패시터(C) 무조건 보존
        if str(comp_name).upper().startswith('C'):
            continue
            
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
    try:
        _dummy_count = len(app.edb._components.components)
        logger.log(f"Successfully loaded {_dummy_count} components from EDB.", level=LogLevel.DETAIL1)
    except Exception as e:
        logger.log(f"[WARNING] Failed to pre-load EDB components: {e}", level=LogLevel.WARNING)

    def normalize_name(name):
        return re.sub(r'[^A-Za-z0-9]', '', str(name)).upper()

    def normalize_pin_name(pin_name):
        return re.sub(r'[^A-Za-z0-9]', '', str(pin_name or "")).upper()

    def normalize_net_name(net_name):
        return re.sub(r'[^A-Za-z0-9+]', '', str(net_name or "").strip()).upper()

    # [개선 사항 반영 4] Spec 넷 이름과 실제 EDB 넷 이름을 매핑하는 기본 Alias 딕셔너리 추가
    DEFAULT_NET_ALIASES = {
        "+1.8V": ["EMMC1V8", "VCC_1V8", "+1V8"],
        "PIF1V5": ["+VTERM", "VCC_1V5", "SIGN003116"]
    }

    def build_net_alias_map(short_correction):
        alias = {}
        # 1. Short Correction 기반 Alias 추가
        for primary, secondaries in (short_correction or {}).items():
            all_nets = [str(primary)] + [str(s) for s in secondaries]
            for n in all_nets:
                alias.setdefault(n, set()).update(x for x in all_nets if x != n)
        
        # 2. 기본 하드코딩 Alias 병합
        for spec_net, edb_nets in DEFAULT_NET_ALIASES.items():
            alias.setdefault(spec_net, set()).update(edb_nets)
            for edb_net in edb_nets:
                alias.setdefault(edb_net, set()).add(spec_net)
                
        return alias

    net_alias_map = build_net_alias_map(SHORT_CORRECTION)

    normalized_edb_component_names = {
        normalize_name(c_name): c_name
        for c_name in app.edb._components.components.keys()
    }

    def get_component_by_normalized(norm_name):
        comp_name = normalized_edb_component_names.get(norm_name)
        if not comp_name:
            return None
        return app.edb._components.components.get(comp_name)

    # =================================================================
    # [PDN 수정] Inner Cap 생성 및 연결 로직 추가 (방어 코드 적용)
    # =================================================================
    INNER_CAP_FILE = settings_manager.data.get('CAE', {}).get('SOC', {}).get('Inner_cap')
    GND_NET = settings_manager.get_gnd_net()  # 💡 [방어 3] 하드코딩 제거 및 동적 할당

    if INNER_CAP_FILE:
        inner_cap_path = INPUT_DIR / INNER_CAP_FILE
        
        # 💡 [방어 4] 스크립트 재실행 시 이름 중복(Collision) 방지를 위한 사전 삭제
        previous_innercap_names = set()
        previous_innercap_report = OUTPUT_DIR / "innercap_verification.json"
        if previous_innercap_report.exists():
            try:
                with open(previous_innercap_report, "r", encoding="utf-8") as pf:
                    prev_data = json.load(pf)
                for rec in prev_data.get("Details", []):
                    comp_name = rec.get("component_name", "")
                    if comp_name:
                        previous_innercap_names.add(comp_name)
            except Exception:
                pass
        for comp_name in list(app.edb.components.components.keys()):
            if comp_name.startswith("C_INNER_") or comp_name in previous_innercap_names:
                try:
                    app.edb.components.components[comp_name].delete()
                except Exception as e:
                    logger.log(f"[WARNING] 기존 Inner Cap({comp_name}) 삭제 실패: {e}", level=LogLevel.WARNING)

        # SettingsManager를 통해 파일 파싱 및 데이터 가져오기
        if settings_manager.parse_inner_cap(inner_cap_path):
            inner_caps = settings_manager.get_inner_cap()
            for icap_item in inner_caps:
                lk = (normalize_name(icap_item.get('Designator', '')), normalize_pin_name(icap_item.get('Pin_Number', '')))
                if lk not in inner_cap_net_lookup:
                    inner_cap_net_lookup[lk] = {
                        "PCB_Net": (icap_item.get('PCB_Net') or "").strip(),
                        "SoC_Net": (icap_item.get('SoC_Net') or "").strip(),
                    }
            
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
                    "index": idx + 1,
                    "component_name": cap_name,
                    "designator": ic_refdes,
                    "pin_number": pin_no,
                    "cap_value": cap_val,
                    "maker_part_number": icap.get('Part_Number', ''),
                    "quantity": icap.get('Quantity', 1),
                    "quantity_index": icap.get('Quantity_Index', 1),
                    "status": "pending",
                    "message": "",
                }
                
                # 1. 타겟 IC 부품 찾기
                norm_ic_name = normalize_name(ic_refdes)
                ic_inst = get_component_by_normalized(norm_ic_name)
                
                if not ic_inst:
                    logger.log(f"[WARNING] Inner Cap 타겟 IC({ic_refdes})를 EDB에서 찾을 수 정 없습니다.", level=LogLevel.WARNING)
                    audit_item["status"] = "target_ic_not_found"
                    audit_item["message"] = "Target IC not found in EDB"
                    inner_cap_audit.append(audit_item)
                    continue
                
                # 2. 타겟 핀 찾기 및 위치/Net 정보 추출
                pin_inst = ic_inst.pins.get(pin_no)
                if not pin_inst:
                    # Fallback 1) normalized pin-name match
                    pin_norm_target = normalize_pin_name(pin_no)
                    pin_norm_map = {}
                    for p_name in ic_inst.pins.keys():
                        norm_key = normalize_pin_name(p_name)
                        if norm_key and norm_key not in pin_norm_map:
                            pin_norm_map[norm_key] = p_name
                    norm_hit = pin_norm_map.get(pin_norm_target)
                    if norm_hit:
                        pin_inst = ic_inst.pins.get(norm_hit)
                        logger.log(
                            f"[INNERCAP][FALLBACK] Normalized pin matched: IC={ic_refdes}, SpecPin={pin_no}, EDBPin={norm_hit}",
                            level=LogLevel.DETAIL1,
                        )

                if not pin_inst:
                    # Fallback 2) net-based pin pick using SoC/PCB net hints
                    hint_nets = []
                    for net_hint in ((icap.get('SoC_Net') or "").strip(), (icap.get('PCB_Net') or "").strip()):
                        if net_hint and net_hint not in hint_nets:
                            hint_nets.append(net_hint)
                    if hint_nets:
                        available_pin_nets = {p_name: p.net_name for p_name, p in ic_inst.pins.items() if p.net_name}
                        for net_hint in hint_nets:
                            norm_hint = normalize_net_name(net_hint)
                            for p_name, p_net in available_pin_nets.items():
                                if p_net == net_hint or normalize_net_name(p_net) == norm_hint:
                                    pin_inst = ic_inst.pins.get(p_name)
                                    logger.log(
                                        f"[INNERCAP][FALLBACK] Net hint matched: IC={ic_refdes}, Pin={p_name}, HintNet={net_hint}, EDBNet={p_net}",
                                        level=LogLevel.DETAIL1,
                                    )
                                    break
                            if pin_inst:
                                break

                if not pin_inst:
                    logger.log(f"[WARNING] IC({ic_refdes})에서 핀({pin_no})을 찾을 수 없습니다.", level=LogLevel.WARNING)
                    audit_item["status"] = "pin_not_found"
                    audit_item["message"] = "Target pin not found in IC"
                    inner_cap_audit.append(audit_item)
                    continue
                    
                power_net_name = pin_inst.net_name
                pin_loc = pin_inst.position
                audit_item["power_net"] = power_net_name
                audit_item["gnd_net"] = GND_NET
                
                # 4. EDB에 가상 커패시터 부품(RLC) 생성
                try:
                    gnd_pin_inst = find_nearest_gnd_pin(app.edb, pin_loc, GND_NET)
                    if not gnd_pin_inst:
                        logger.log(
                            f"[WARNING] IC({ic_refdes}) 핀({pin_no}) 주변 GND 핀을 찾지 못해 Inner Cap 생성을 건너뜁니다.",
                            level=LogLevel.WARNING,
                        )
                        audit_item["status"] = "gnd_pin_not_found"
                        audit_item["message"] = "Nearest GND pin not found"
                        inner_cap_audit.append(audit_item)
                        continue

                    created = app.create_rlc_component(
                        pins=[pin_inst, gnd_pin_inst],
                        comp_name=cap_name,
                        part_name=icap.get('Part_Number', 'INNER_CAP'),
                        r_value=1e9,
                    )
                    if not created:
                        raise RuntimeError("create_rlc_component returned False")

                    new_comp = app.edb._components.components.get(cap_name)
                    if new_comp:
                        new_comp.type = "Capacitor"
                        new_comp.value = cap_val
                        nets = sorted({p.net_name for p in new_comp.pins.values() if p.net_name})
                        audit_item["created_nets"] = nets
                        if power_net_name in nets and GND_NET in nets:
                            audit_item["status"] = "created"
                            audit_item["message"] = "Created and connected to target power/GND nets"
                        else:
                            audit_item["status"] = "created_but_net_mismatch"
                            audit_item["message"] = f"Created but nets mismatch: {nets}"
                    else:
                        audit_item["status"] = "created_not_found"
                        audit_item["message"] = "Created call succeeded but component lookup failed"
                        
                    logger.log(f"[*] Inner Cap 생성 완료: {cap_name} ({cap_val}) 연결망: {power_net_name} <-> {GND_NET}", level=LogLevel.DETAIL1)
                    inner_cap_audit.append(audit_item)
                    
                except Exception as e:
                    logger.log(f"[ERROR] Inner Cap {cap_name} 생성 중 오류 발생: {e}", level=LogLevel.ERROR)
                    audit_item["status"] = "create_error"
                    audit_item["message"] = str(e)
                    inner_cap_audit.append(audit_item)
    # =================================================================

    for idx, case in enumerate(spec_info):
        comp_name = case['Designator']
        norm_comp_name = normalize_name(comp_name)
        target_net_from_spec = case.get('Net_Name') or case.get('Target_Net') or case.get('Net_name') or case.get('Net')
        
        comp_inst = get_component_by_normalized(norm_comp_name)
        
        if comp_inst is None:
            logger.log(f"[ERROR] Component '{comp_name}' (Normalized: {norm_comp_name}) not found in EDB. Skipping this case.", level=LogLevel.ERROR)
            continue

        target_pin_name = str(case['Pin_number']).strip()
        pin_inst, actual_pin_name = comp_inst.pins.get(target_pin_name), target_pin_name
        lookup_key = (norm_comp_name, normalize_pin_name(target_pin_name))
        lookup_nets = inner_cap_net_lookup.get(lookup_key, {})
        candidate_nets = []
        for net_val in [target_net_from_spec, lookup_nets.get("PCB_Net"), lookup_nets.get("SoC_Net")]:
            if net_val and net_val not in candidate_nets:
                candidate_nets.append(net_val)

        if not pin_inst:
            available_pins_info = {p_name: p_inst.net_name for p_name, p_inst in comp_inst.pins.items() if p_inst.net_name}
            normalized_target_pin = normalize_pin_name(target_pin_name)

            # Fallback 1) normalized pin-name match (for hidden chars, spacing, case variance)
            norm_pin_map = {}
            for p_name in comp_inst.pins.keys():
                norm_key = normalize_pin_name(p_name)
                if norm_key and norm_key not in norm_pin_map:
                    norm_pin_map[norm_key] = p_name
            norm_pin_name = norm_pin_map.get(normalized_target_pin)
            if norm_pin_name:
                pin_inst = comp_inst.pins.get(norm_pin_name)
                actual_pin_name = norm_pin_name
                logger.log(
                    f"[PINMAP][FALLBACK] Normalized pin-name matched: IC={comp_name}, "
                    f"SpecPin={repr(target_pin_name)} -> EDBPin={repr(actual_pin_name)}",
                    level=LogLevel.DETAIL1,
                )

            if (not pin_inst) and candidate_nets:
                # Fallback 2) exact net-name match (Spec + InnerCap lookup nets)
                for net_candidate in candidate_nets:
                    for p_name, net_name in available_pins_info.items():
                        if net_name == net_candidate:
                            pin_inst, actual_pin_name = comp_inst.pins[p_name], p_name
                            logger.log(
                                f"[PINMAP][FALLBACK] Exact net matched: IC={comp_name}, "
                                f"NetCandidate={net_candidate}, EDBNet={net_name}, Pin={actual_pin_name}",
                                level=LogLevel.DETAIL1,
                            )
                            break
                    if pin_inst:
                        break
            if (not pin_inst) and candidate_nets:
                # Fallback 3) normalized/alias net-name match (for every candidate net)
                for net_candidate in candidate_nets:
                    spec_norm_net = normalize_net_name(net_candidate)
                    alias_candidates = {net_candidate}
                    alias_candidates.update(net_alias_map.get(net_candidate, set()))
                    alias_norm_nets = {normalize_net_name(n) for n in alias_candidates if n}
                    for p_name, net_name in available_pins_info.items():
                        if normalize_net_name(net_name) in alias_norm_nets or normalize_net_name(net_name) == spec_norm_net:
                            pin_inst, actual_pin_name = comp_inst.pins[p_name], p_name
                            logger.log(
                                f"[PINMAP][FALLBACK] Net normalized/alias matched: IC={comp_name}, "
                                f"NetCandidate={net_candidate}, EDBNet={net_name}, Pin={actual_pin_name}",
                                level=LogLevel.DETAIL1,
                            )
                            break
                    if pin_inst:
                        break
            if not pin_inst:
                sample_pin_net = list(available_pins_info.items())[:15]
                sample_pin_net_text = ", ".join([f"{repr(k)}:{repr(v)}" for k, v in sample_pin_net])
                spec_norm_net = normalize_net_name(target_net_from_spec) if target_net_from_spec else ""
                net_norm_matches = [
                    (p_name, n_name)
                    for p_name, n_name in available_pins_info.items()
                    if normalize_net_name(n_name) == spec_norm_net
                ][:10]
                logger.log(
                    f"[WARNING] Spec pin/net mapping not found. Skip case: IC={comp_name}, Pin={target_pin_name}, "
                    f"SpecNet={target_net_from_spec}",
                    level=LogLevel.WARNING,
                )
                logger.log(
                    f"[PINMAP][DEBUG] target_pin_repr={repr(target_pin_name)}, target_pin_norm={normalized_target_pin}, "
                    f"spec_net_norm={spec_norm_net}, total_pins={len(comp_inst.pins)}, "
                    f"candidate_nets={candidate_nets}, "
                    f"alias_nets={sorted(list(net_alias_map.get(target_net_from_spec, []))) if target_net_from_spec else []}",
                    level=LogLevel.WARNING,
                )
                logger.log(
                    f"[PINMAP][DEBUG] sample_pin_to_net={sample_pin_net_text}",
                    level=LogLevel.WARNING,
                )
                if net_norm_matches:
                    logger.log(
                        f"[PINMAP][DEBUG] normalized net matches exist but pin unresolved: {net_norm_matches}",
                        level=LogLevel.WARNING,
                    )
                continue

        if pin_inst and target_net_from_spec:
            pin_net_actual = pin_inst.net_name
            spec_norm_net = normalize_net_name(target_net_from_spec)
            actual_norm_net = normalize_net_name(pin_net_actual)
            if spec_norm_net and actual_norm_net and spec_norm_net != actual_norm_net:
                logger.log(
                    f"[PINMAP][INFO] Spec/EDB net mismatch: IC={comp_name}, Pin={actual_pin_name}, "
                    f"SpecNet={target_net_from_spec}, EDBNet={pin_net_actual}, "
                    f"SpecAlias={sorted(list(net_alias_map.get(target_net_from_spec, [])))}",
                    level=LogLevel.WARNING,
                )

        db.other_nets[pin_inst.net.name] = []
        
        result, net_chain = find_power_source(app.edb, pin_inst.net, designator_list, bom_info, GND_NET, target_ic=comp_name)
        if isinstance(result, ErrorCode): net_chain = []

        source_pin_name = next((p_name for s_net in reversed(net_chain + [pin_inst.net.name]) 
                              for p_name, p_inst in result.pins.items() if p_inst.net_name == s_net), None) if not isinstance(result, ErrorCode) and result else None

        full_chain = []
        for n in (net_chain + [pin_inst.net.name]):
            if n not in full_chain:
                full_chain.append(n)

        vmag_default = extract_voltage(pin_inst.net.name) or 1.0
        vmag = parse_numeric(case.get('Voltage_(V)'), vmag_default)
        imag = parse_numeric(case.get('Current_(A)'), 1.0)
        min_spec = parse_numeric(case.get('Min_Spec_(V)'), 0.0)
        max_spec = parse_numeric(case.get('Max_Spec_(V)'), max(vmag * 1.2, vmag + 0.1))

        pdn_cases_info.append({
            'IC': comp_name, 'IC_pin': actual_pin_name, 'Net': pin_inst.net.name,
            'Source_name': result.name if not isinstance(result, ErrorCode) else "",
            'Source_pin': source_pin_name, 'Source_net_chain': net_chain, 
            'Full_Net_Chain': full_chain, 
            'Vmag': vmag, 'Imag': imag,
            'MinSpec': min_spec, 'MaxSpec': max_spec
        })

    vrm_setup_conf = conf_manager.data.get('PDN', {}).get('vrmSetup', {})
    enable_vrm_setup = bool(vrm_setup_conf.get("enable", True))
    env_override = os.environ.get("PDN_ENABLE_VRM_SETUP")
    if env_override is not None:
        enable_vrm_setup = env_override.strip().lower() in {"1", "true", "y", "yes", "on"}
    if enable_vrm_setup:
        try:
            configure_ports_and_vrms_from_spec(
                app=app,
                cases=pdn_cases_info,
                gnd_net=GND_NET,
                bulk_inductor_set=set(bom_info.get('bulkInd', [])),
                output_dir=OUTPUT_DIR,
                logger=logger,
                vrm_setup_conf=vrm_setup_conf,
            )
        except Exception as e:
            logger.log(f"[VRM_SETUP][WARNING] setup pipeline failed: {e}", level=LogLevel.WARNING)

    target_nets = {GND_NET} | {n for case in pdn_cases_info for n in case.get('Full_Net_Chain', [])}

    if not app.sanitize_nets(target_nets): raise PDNSessionException(ErrorCode.SANITIZE_FAIL)

    pdn_logic = PDN(logger=logger)

    traced_net_pairs = set()
    for case in pdn_cases_info:
        full_chain = case.get('Full_Net_Chain', []) 
        for i in range(len(full_chain) - 1):
            net1, net2 = full_chain[i], full_chain[i+1]
            if net1 != net2:
                traced_net_pairs.add(tuple(sorted([net1, net2])))

    zero_ohm_candidates = [k for k, v in conf_manager.data['PDN']['BOM']['compProp'].items() if 'connectPin' in v]

    for comp_type in zero_ohm_candidates:
        target_comps = bom_info.get(comp_type, [])
        if not target_comps: continue
        
        original_prop = conf_manager.data['PDN']['BOM']['compProp'][comp_type]
        connect_pins_config = original_prop.get('connectPin', [])
        
        pin_pairs = connect_pins_config if (connect_pins_config and isinstance(connect_pins_config[0], list)) else [connect_pins_config]
            
        for comp_name in target_comps:
            norm_name = re.sub(r'[^A-Za-z0-9]', '', str(comp_name)).upper()
            comp_inst = get_component_by_normalized(norm_name)
            if not comp_inst: continue
            
            pin_nets = {str(p_name).strip(): p_inst.net_name for p_name, p_inst in comp_inst.pins.items() if p_inst.net_name}
            
            for pair in pin_pairs:
                if len(pair) < 2: continue
                
                p1, p2 = str(pair[0]), str(pair[1])
                net1, net2 = pin_nets.get(p1), pin_nets.get(p2)
                
                if net1 and net2 and net1 != net2:
                    current_pair = tuple(sorted([net1, net2]))
                    
                    if current_pair in traced_net_pairs:
                        exact_pins = [int(p1) if p1.isdigit() else p1, int(p2) if p2.isdigit() else p2]
                        
                        custom_prop = original_prop.copy()
                        custom_prop['connectPin'] = exact_pins
                        if 'connectType' in custom_prop: del custom_prop['connectType']
                        
                        logger.log(f"[*] Smart Short Applied: {comp_name} (Pin {exact_pins[0]} <-> Pin {exact_pins[1]}) bridging {net1} and {net2}", level=LogLevel.DETAIL1)
                        
                        pdn_logic.install_0ohm_resistors(
                            app=app, 
                            comp_type=comp_name,  
                            target_comp=[comp_name], 
                            comp_prop=custom_prop, 
                            exclude_prefixes=()
                        )

    PRE_EDB_FILE_PATH = EDB_FILE_PATH.parent / f"{EDB_FILE_PATH.stem}_1{EDB_FILE_PATH.suffix}"
    ensure_pre_edb_saved(
        app=app,
        source_edb_path=EDB_FILE_PATH,
        pre_edb_path=PRE_EDB_FILE_PATH,
        max_retries=2,
        timeout=300.0,
    )
    if STAGE == "full":
        # Reuse Step-4 EDB session in Step-5 model assignment to avoid opening a new EDB session.
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

if STAGE == "pre":
    # 1. s2pDirectory 및 기타 입력 파일 경로 사전 추출 (가독성 및 안전성 개선)
    s2p_dir_raw = conf_manager.data.get('PDN', {}).get('sParameter', {}).get('s2pDirectory')
    s2p_dir_path = None
    if s2p_dir_raw:
        raw_path = Path(s2p_dir_raw)
        s2p_dir_path = raw_path if raw_path.is_absolute() else (INPUT_DIR / raw_path)

    pmap_input = input_valchk._optional_inputFiles.get('Pmap') if input_valchk else None
    spec_input = input_valchk._default_inputFiles.get('Spec') if input_valchk else None
    spec_file_for_report = Path(spec_input) if spec_input else INPUT_JSON
    stackup_input = input_valchk._default_inputFiles.get('Stackup') if input_valchk else None

    # 2. 스냅샷 생성 단계 예외 처리 분리 및 스택업 로그 추가
    try:
        if stackup_input:
            logger.log(f"Using Stackup file for pre-stage: {stackup_input}", level=LogLevel.INFO)
            
        build_pre_stage_siw_snapshot(
            aedt_version=AEDT_VERSION,
            pre_edb_path=PRE_EDB_FILE_PATH,
            siw_output_path=SIwave_FILE_PATH,
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
        )
    except Exception:
        logger.fatal(f"Failed to build pre-stage SIW snapshot: {traceback.format_exc()}")
        raise SystemExit(1)

    # 3. 리포트 추출 단계 예외 처리 분리
    try:
        logger.log(
            f"Step {step}. PRE report export (PDN setup/report steps only)",
            level=LogLevel.INFO,
        )
        export_pre_stage_reports(
            OUTPUT_DIR,
            spec_file_for_report,
            PRE_EDB_FILE_PATH,
            pdn_cases_info,
            inner_cap_audit,
            logger,
            Path(pmap_input) if pmap_input else None,
            s2p_dir_path,
        )
    except Exception:
        logger.fatal(f"Failed to export pre-stage reports: {traceback.format_exc()}")
        raise SystemExit(1)

    # 4. 종료 시간 로깅 반영
    END_TIME = time.strftime('%Y.%m.%d, %H:%M:%S')
    logger.log(f"Pre-stage completed successfully at {END_TIME}", level=LogLevel.INFO)
    raise SystemExit(0)

# region 5. Modify CAD Data using SIwave and Set PDN Simulation
try:
    logger.log(f"Step {step}. CAD Modification", level=LogLevel.INFO)

    logger.log("Waiting for EDB file I/O completion...", level=LogLevel.DETAIL2)
    max_wait_time = 300.0
    if not wait_for_edb_ready(PRE_EDB_FILE_PATH, timeout=max_wait_time, check_interval=3.0):
        logger.log(
            "[WARNING] PRE EDB not ready in time. Trying one recovery save from source EDB...",
            level=LogLevel.WARNING,
        )
        recovery_app = None
        try:
            recovery_app = SIwave(version=AEDT_VERSION, logger=logger)
            recovery_app.set_cad_file(EDB_FILE_PATH)
            ensure_pre_edb_saved(
                app=recovery_app,
                source_edb_path=EDB_FILE_PATH,
                pre_edb_path=PRE_EDB_FILE_PATH,
                max_retries=2,
                timeout=max_wait_time,
            )
        finally:
            if recovery_app:
                recovery_app.quit_application()

    if not _is_edb_ready(PRE_EDB_FILE_PATH):
        raise FileNotFoundError(
            f"Target EDB path or edb.def is not ready after retries: {PRE_EDB_FILE_PATH}"
        )

    app = None
    image_app = None

    try:
        app = SIwave(version=AEDT_VERSION, logger=logger)
        
        time.sleep(3.0) 
        
        max_retries = 3
        for attempt in range(max_retries):
            try:
                logger.log(f"Importing EDB to SIwave (Attempt {attempt + 1}/{max_retries}): {PRE_EDB_FILE_PATH.name}", level=LogLevel.DETAIL1)
                app.import_edb(str(PRE_EDB_FILE_PATH))
                break
            except Exception as e:
                if attempt < max_retries - 1:
                    logger.log(f"[WARNING] Failed to import EDB. Retrying in 5 seconds... ({e})", level=LogLevel.WARNING)
                    time.sleep(5.0)
                else:
                    logger.log(f"[ERROR] Failed to import EDB after {max_retries} attempts.", level=LogLevel.ERROR)
                    raise

        pdn_logic = PDN(logger=logger)
        pdn_logic.apply_dc_shorts(
            app=app, 
            shorted_comp_defs=conf_manager.data['PDN']['dcShort']['shortedComp'], 
            del_comps=DEL_COMP, 
            short_correction=SHORT_CORRECTION
        )

        PMAP_FILE = INPUT_DIR / settings_manager.data['CAE']['PCB']['Pmap'] if settings_manager.data['CAE']['PCB']['Pmap'] else None
        stackup_input = input_valchk._default_inputFiles.get('Stackup') if input_valchk else None
        profile_key, sws_name, sfsdf_name = resolve_zparam_profile(
            conf_manager.data,
            Path(stackup_input) if stackup_input else None,
            STACKUP_LAYER_COUNT,
        )
        if stackup_input:
            effective_stackup = prepare_stackup_for_project(app, Path(stackup_input), OUTPUT_DIR, logger)
            if app.import_layer_stackup(effective_stackup):
                logger.log(f"[ZParam] Reapplied stackup before setup: {effective_stackup}", level=LogLevel.DETAIL1)
            else:
                logger.log(f"[ZParam][WARNING] Stackup reapply failed: {effective_stackup}", level=LogLevel.WARNING)
        SWS_FILE = WORKING_DIR / 'core' / sws_name
        SFSDF_FILE = (WORKING_DIR / 'core' / sfsdf_name) if sfsdf_name else None
        logger.log(
            f"[ZParam] Profile={profile_key}, SWS={SWS_FILE.name}, SFSDF={(SFSDF_FILE.name if SFSDF_FILE else '')}",
            level=LogLevel.DETAIL1,
        )

        s2p_dir_conf = conf_manager.data.get('PDN', {}).get('sParameter', {}).get('s2pDirectory', '')
        s2p_dir = None
        if s2p_dir_conf:
            candidate_dir = Path(s2p_dir_conf)
            if not candidate_dir.is_absolute():
                candidate_dir = INPUT_DIR / candidate_dir
            s2p_dir = candidate_dir

        if conf_manager.data.get('PDN', {}).get('sParameter', {}).get('enableAssign', True):
            model_assign_app = EDB_SETUP_APP if EDB_SETUP_APP else app
            innercap_name = settings_manager.data.get('CAE', {}).get('SOC', {}).get('Inner_cap')
            innercap_csv_path = (INPUT_DIR / innercap_name) if innercap_name else None
            bom_name = settings_manager.data.get('CAE', {}).get('PCB', {}).get('BOM')
            bom_file_path = (INPUT_DIR / bom_name) if bom_name else None
            model_lib_candidates = conf_manager.data.get('PDN', {}).get('sParameter', {}).get('model_library_dir_candidates', [])
            resolved_candidates = []
            for cand in model_lib_candidates:
                if not cand:
                    continue
                cpath = Path(cand)
                if not cpath.is_absolute():
                    cpath = INPUT_DIR / cpath
                resolved_candidates.append(cpath)
            assign_sparameter_models(
                app=model_assign_app,
                bom_info=bom_info,
                inner_cap_audit=inner_cap_audit,
                pmap_file=PMAP_FILE,
                s2p_dir=s2p_dir,
                gnd_net=GND_NET,
                output_dir=OUTPUT_DIR,
                logger=logger,
                innercap_csv_path=innercap_csv_path,
                bom_file_path=bom_file_path,
                search_roots=[INPUT_DIR, WORKING_DIR, OUTPUT_DIR] + resolved_candidates,
            )

        # =========================================================================================
        # [개선 사항 반영 5] 기존 SFSDF 임포트 대신 동적 주파수 셋업 함수 호출
        # =========================================================================================
        app.setup_simulation(PMAP_FILE, SWS_FILE, None) # SFSDF_FILE 대신 None 전달
        apply_dynamic_frequency_setup(app, STACKUP_LAYER_COUNT, conf_manager.data, logger)
        # =========================================================================================

        REF_SIwave_FILE_PATH = SIwave_FILE_PATH.parent / f"{SIwave_FILE_PATH.stem}_ref{SIwave_FILE_PATH.suffix}"
        app.save_project_as(REF_SIwave_FILE_PATH)

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

    try:
        if EDB_FILE_PATH.exists(): shutil.rmtree(EDB_FILE_PATH)
        if PRE_EDB_FILE_PATH.exists(): shutil.rmtree(PRE_EDB_FILE_PATH)
        logger.log("Cleaned up intermediate EDB files to save disk space.", level=LogLevel.DETAIL1)
    except Exception as e:
        logger.log(f"Failed to clean up intermediate files: {e}", level=LogLevel.WARNING)

    step += 1

except Exception:
    logger.fatal(f"An error occurred while generating files and running simulation : {traceback.format_exc()}")
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
        complete_count = sum(1 for case in full_state.summary if case.get('is_done'))
        if complete_count == 0:
            raise PostStageError(
                f"FullBatch Post failed: no completed result was detected "
                f"(0/{len(full_state.summary)} cases)"
            )
    except Exception:
        logger.fatal(f"An error occurred while performing PDN results extracting : {traceback.format_exc()}")
# endregion
