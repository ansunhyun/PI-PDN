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
from pathlib import Path

from core.SettingsManager import SettingsManager, MAX_RETRIES, RETRY_DELAY
from core.logger import Logger, LogLevel
from core.database import DCIRSessionException, ErrorCode, InValChk, Database, DCIRCase, extract_voltage, sanitize_str
from core.post_processing import PostProcessing
from core.post_stage import PostStageError, append_post_detail, prepare_post_settings, reconstruct_post_state

MODE = 0  # Generic Mode

if not MODE == 2:
    from EBU_lib.SIwave import SIwave
    from EBU_lib.DCIR import DCIR 

# region Global Variables
logger = Logger(name="DCIR")
step = 0
UTILITY_NAME = "AutoDCIR"
VERSION = "1.4"
START_TIME = None
END_TIME = None
WORKING_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
INPUT_JSON = Path()
INPUT_DIR = Path()
db = None  
bom_info = {} 
dcir_cases_info = []
inner_cap_audit = []
PRE_EDB_FILE_PATH = None
# endregion

# region Terminate & Save Log (atexit 등록)
def terminate_and_save_log():
    try:
        logger.log("#" * 100, level=LogLevel.SECTION, line_change=False)
        logger.log("|", level=LogLevel.SECTION, line_change=False)
        logger.log(f"| Terminate Ansys {UTILITY_NAME} v{VERSION} on {time.strftime('%Y.%m.%d, %H:%M:%S')}", level=LogLevel.SECTION, line_change=False)
        logger.log("|", level=LogLevel.SECTION, line_change=False)
        logger.log("#" * 100, level=LogLevel.SECTION, line_change=False)
        
        save_dir = str(INPUT_DIR) if str(INPUT_DIR) != "." else str(WORKING_DIR)
        logger.save(save_dir)
    except Exception as e:
        print(f"Failed to save log during termination: {e}")

atexit.register(terminate_and_save_log)
# endregion

# region Functions
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
    """
    [TEMP]
    Skip Zuken conversion if preconverted artifacts already exist.
    This block is intentionally isolated so it can be removed easily later.

    Enable with environment variable:
      PDN_USE_PRECONVERTED=1
    """
    use_preconverted = os.environ.get("PDN_USE_PRECONVERTED", "").strip().lower() in {"1", "true", "y", "yes", "on"}
    if not use_preconverted:
        return None

    work_dir = input_cad_file.parent
    base_stem = input_cad_file.stem.split('-')[0]

    # Prefer exact same-base artifacts first.
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
    OUTPUT_DIR = Path(r"D:\2_QC\LGE_MS_DCIR\v0p99\Zuken_to_ODB")
    DSGN_FILE = Path(r"D:\2_QC\LGE_MS_DCIR\v0p99\Zuken_to_ODB\test_design.dsgn")
    if OUTPUT_DIR.exists() and DSGN_FILE.exists():
        shutil.make_archive(OUTPUT_DIR / DSGN_FILE.stem, 'zip', OUTPUT_DIR, DSGN_FILE.stem)
        ZIP_FILE = OUTPUT_DIR / f"{DSGN_FILE.stem}.zip"
        shutil.move(ZIP_FILE, ZIP_FILE.with_suffix('.tgz'))

def trace_power_path(edb, net_inst, designator_list, bom_info, gndNet, pre_bead=None, net_chain=None, comp_chain=None, target_ic=None):
    net_chain = net_chain or [net_inst.name]
    comp_chain = comp_chain or []
    logger.log(f"[*] Tracing Net: {net_inst.name} | Current Chain: {net_chain}", level=LogLevel.INFO)
    LDO_list, inductors, fet_list, switch_list, dcdc_list = find_IND_and_OTHER_in_net(net_inst, designator_list, bom_info)
    found_sources = []
    for dcdc in dcdc_list: found_sources.append({'type': 'DCDC', 'inst': dcdc, 'chain': net_chain, 'comp_chain': comp_chain})
    for ldo in LDO_list: found_sources.append({'type': 'LDO', 'inst': ldo, 'chain': net_chain, 'comp_chain': comp_chain})
    def add_to_other_net(current_net, next_net):
        if next_net not in db.other_nets.setdefault(current_net, []):
            db.other_nets[current_net].append(next_net)
    for ind_inst in inductors["bulk"]:
        other_net_name = next((x for x in ind_inst.nets if x != net_inst.name), None)
        if other_net_name and other_net_name in edb._nets.nets and other_net_name not in net_chain:
            add_to_other_net(net_inst.name, other_net_name)
            upstream_sources = trace_power_path(edb, edb._nets.nets[other_net_name], designator_list, bom_info, gndNet, pre_bead=None, net_chain=net_chain + [other_net_name], comp_chain=comp_chain + [f"{ind_inst.name}(BulkInd)"], target_ic=target_ic)
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
            upstream_sources = trace_power_path(edb, other_net_inst, designator_list, bom_info, gndNet, pre_bead=None, net_chain=net_chain + [other_net_name], comp_chain=comp_chain + [f"{switch_inst.name}(Switch)"], target_ic=target_ic)
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
            upstream_sources = trace_power_path(edb, other_net_inst, designator_list, bom_info, gndNet, pre_bead=None, net_chain=net_chain + [other_net_name], comp_chain=comp_chain + [f"{fet_inst.name}(FET)"], target_ic=target_ic)
            found_sources.extend(upstream_sources)
    if pre_bead:
        inductors["bead"] = [bead for bead in inductors["bead"] if bead.name != pre_bead.name]
    for bead_inst in inductors["bead"]:
        other_net_inst = find_other_net_ind(edb, bead_inst, net_inst.name)
        if other_net_inst and other_net_inst.name not in net_chain:
            add_to_other_net(net_inst.name, other_net_inst.name)
            upstream_sources = trace_power_path(edb, other_net_inst, designator_list, bom_info, gndNet, pre_bead=bead_inst, net_chain=net_chain + [other_net_inst.name], comp_chain=comp_chain + [f"{bead_inst.name}(Bead)"], target_ic=target_ic)
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

def find_bulkInd(edb, net_inst, designator_list, bom_info, gndNet, pre_bead=None, net_chain=None, target_ic=None):
    initial_chain = net_chain or [net_inst.name]
    all_sources = trace_power_path(edb, net_inst, designator_list, bom_info, gndNet, pre_bead=pre_bead, net_chain=net_chain, comp_chain=None, target_ic=target_ic)
    if not all_sources: return ErrorCode.NO_SOURCE_FOUND, initial_chain 
    unique_sources = {'DCDC': {}, 'LDO': {}}
    for src in all_sources: unique_sources[src['type']][src['inst'].name] = src
    for src_type, err_code in [('DCDC', ErrorCode.INVALID_DCDC_NUM), ('LDO', ErrorCode.INVALID_LDO_NUM)]:
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

def find_IND_and_OTHER_in_net(net_inst, designator_list, bom):
    LDO_list, inductors, fet_list, switch_list, dcdc_list = [], {"bulk": [], "bead": []}, [], [], []
    for name, inst in net_inst.components.items():
        if name in bom.get('bulkInd', []): inductors['bulk'].append(inst)
        elif name in bom.get('beadInd', []): inductors['bead'].append(inst)
        elif name not in designator_list and name in bom.get('Designators', []):
            if name in bom.get('DCDC', []): dcdc_list.append(inst)
            elif name in bom.get('LDO', []): LDO_list.append(inst)
            elif name in bom.get('FET', []) or name in bom.get('TR', []): fet_list.append(inst)
            elif name in bom.get('analogSwitch', []): switch_list.append(inst)
    return LDO_list, inductors, fet_list, switch_list, dcdc_list

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
    min_dist = float("inf")
    best_pin = None
    for comp in edb._components.components.values():
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
        "Source_Component": case.get("DCDC_name", ""),
        "Source_Pin": case.get("DCDC_pin", ""),
        "Net_Chain": case.get("DCDC_net", []),
        "Full_Net_Chain": case.get("Full_Net_Chain", []),
        "Voltage_V": case.get("Vmag", 0.0),
        "Current_A": case.get("Imag", 0.0),
        "Min_Spec_V": case.get("MinSpec", 0.0),
        "Max_Spec_V": case.get("MaxSpec", 0.0),
    }

def export_pre_stage_reports(output_dir: Path, spec_file: Path, pre_edb_path: Path, cases, inner_caps, logger: Logger):
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

def resolve_siwave_executable(aedt_version: str):
    clean_version = aedt_version.replace('20', '', 1).replace('.', '')
    env_var = f"ANSYSEM_ROOT{clean_version}"
    install_dir_str = os.environ.get(env_var)
    if not install_dir_str:
        raise DCIRSessionException(ErrorCode.SIWAVE_EXECUTABLE_NOT_FOUND, f"Environment variable {env_var} not found.")
    aedt_install_dir = Path(install_dir_str)
    siw_execute_file = aedt_install_dir / 'siwave_ng.exe'
    if not siw_execute_file.exists():
        raise DCIRSessionException(ErrorCode.SIWAVE_EXECUTABLE_NOT_FOUND, siw_execute_file)
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
        "Full_Net_Chain": case.get('Full_Net_Chain', case.get('DCDC_net', [])),
        "Source_Component": case.get('DCDC_name', ''),
        "Source_Pin": case.get('DCDC_pin', ''),
        "Net_Chain": case.get('DCDC_net', []),
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
    post_processor.set_DCIR_results(
        analysis_start or state.analysis_start,
        analysis_end or state.analysis_end,
    )
    state.viewer_artifacts = post_processor.extract_results(conf_manager.data['DCIR']['version'])
    append_post_detail(output_dir, state)
    failed_viewers = [
        artifact for artifact in state.viewer_artifacts
        if artifact.get("Edb_Status") == "Error" or artifact.get("Viewer_Status") == "Error"
    ]
    if failed_viewers:
        failed_cases = ", ".join(str(item.get("Case_Index")) for item in failed_viewers)
        raise PostStageError(f"Post AEDB/Viewer generation failed for case(s): {failed_cases}")
    return state

def run_dcir_case(
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
    src_comp_name = case.get('DCDC_name', '')
    logger.log(f"[{idx + 1}/{total_cases}] Processing Case: IC={ic_designator}, Net={original_net_name}", level=LogLevel.DETAIL1)
    case['is_done'] = False

    if idx != 0 and mode == 1:
        return None

    safe_sanitize = lambda s, extra: "".join(c for c in str(s or "") if c.isalnum() or c in extra).strip()
    
    full_net_chain = case.get('Full_Net_Chain', case.get('DCDC_net', []) + [original_net_name])
    
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

        inductor_prefix = conf_data.get('DCIR', {}).get('inductorPrefix', 'L')

        pos_coord, pos_layer, neg_coord, neg_layer, src_name = case_data_app.prepare_vrm_connection(
            target_net=original_net_name,
            dcdc_name=src_comp_name,
            dcdc_pin=case.get('DCDC_pin'),
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
            conf_data['DCIR']['setup']['Vsource_Res'], v_mag
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
                        conf_data['DCIR']['setup']['Isource_Res'], i_mag
                    )
                    logger.log(f"  -> [Current Source] {i_port_name} : {i_mag}A @ {ic_designator} ({ic_layer} to {neg_layer})", level=LogLevel.DETAIL2)
                except Exception as e:
                    logger.log(f"  -> [경고] 전류원 인가 실패: {e}", level=LogLevel.WARNING)

        if conf_data['DCIR'].get('doValchk', False):
            case_app.oproject.ScrRunValidationCheck()

        dcir_sim_name = f'DCIR - {case["IC"]} : {best_net_name}'
        case_app.oproject.ScrSetSimulationName('dc', dcir_sim_name)
        
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
                raise DCIRSessionException(ErrorCode.DCIR_COMMAND_SIMULATION_FAIL, result.returncode)
            case['is_done'] = True

            logger.log("  -> Exporting DCIR results...", level=LogLevel.DETAIL2)
            dcir_result_file_path = net_siw_file.with_suffix('.siwaveresults') / '0000' / '0000.ced'
            if not dcir_result_file_path.exists():
                raise DCIRSessionException(ErrorCode.DCIR_RESULT_NOT_FOUND, dcir_result_file_path)
            viewer_siw_file_path = dcir_result_file_path.with_suffix('.siw')
            if not viewer_siw_file_path.exists():
                raise DCIRSessionException(ErrorCode.DCIR_RESULT_NOT_FOUND, viewer_siw_file_path)
            case['_viewer_siw'] = viewer_siw_file_path

            with open(str(dcir_result_file_path), 'r') as f:
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
    DCIRSessionException.OUTPUT_DIR = OUTPUT_DIR
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    step += 1
except Exception: logger.fatal(f"An error occurred while initializing: {traceback.format_exc()}")

try:
    logger.log(f"Step {step}. Get Configurations for DCIR", level=LogLevel.INFO)
    CONF_FILE = WORKING_DIR / 'core' / 'config.json'
    conf_manager = SettingsManager(CONF_FILE)
    AEDT_VERSION = conf_manager.data['DCIR']['version']
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
    logger.log(f"Step {step}. Get Settings for DCIR", level=LogLevel.INFO)
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
        if conf_manager.data['DCIR']['isZuken']:
            temp_preconv = resolve_temp_preconverted_files(INPUT_CAD_FILE, logger)
            if temp_preconv:
                DSGN_FILE = temp_preconv.get("DSGN_FILE")
                EDB_FILE_PATH = temp_preconv.get("EDB_FILE_PATH")
            else:
                DSGN_FILE = None

            ZUKEN_BIN_DIR = Path(conf_manager.data['DCIR']['DF_path'])
            pcb_files = list(INPUT_CAD_FILE.parent.glob('*.pcb'))
            if len(pcb_files) == 1: PCB_FILE = pcb_files[0]
            else: raise DCIRSessionException(ErrorCode.INVALID_PCB_FILE_NUM, pcb_files)

            if not temp_preconv:
                CR5_EXEC = ZUKEN_BIN_DIR / 'DFevolv.cr5.exe'
                result = subprocess.run([str(CR5_EXEC), str(PCB_FILE.parent)], capture_output=True, text=True)
                if result.returncode: raise DCIRSessionException(ErrorCode.CONVERT_PCB_TO_DSGN_FAIL, result.returncode)
                DSGN_FILE = PCB_FILE.with_suffix('.dsgn')
            elif not DSGN_FILE:
                # keep flow robust when only EDB is preconverted
                DSGN_FILE = PCB_FILE.with_suffix('.dsgn')

            # [PDN 수정] ANF/CMP 변환 로직 복구 (파일 추출 목적)
            if conf_manager.data['DCIR']['exportANF'] and not temp_preconv:
                DSGN2ANF_EXEC = ZUKEN_BIN_DIR / 'DFdsgn2anf.exe'
                ANF_FILE = DSGN_FILE.with_suffix('.anf')
                
                for attempt in range(MAX_RETRIES):
                    result = subprocess.run([str(DSGN2ANF_EXEC), '-r', str(DSGN_FILE), '-o', str(ANF_FILE)], capture_output=True, text=True)
                    
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
                            raise DCIRSessionException(ErrorCode.CONVERT_DSGN_TO_ANF_FAIL, result.returncode)

                cmp_files = list(INPUT_CAD_FILE.parent.glob('*.cmp'))
                if len(cmp_files) == 1: CMP_FILE = cmp_files[0]
                else: raise DCIRSessionException(ErrorCode.INVALID_CMP_FILE_NUM, cmp_files)

            if conf_manager.data['DCIR']['exportODB'] and not temp_preconv:
                DSGN2ODB_EXEC = ZUKEN_BIN_DIR / 'DFodbout.exe'
                result = subprocess.run([str(DSGN2ODB_EXEC), '-r', str(DSGN_FILE), '-o', str(DSGN_FILE.parent)], capture_output=True, text=True)
                make_tgz(INPUT_CAD_FILE.parent / INPUT_CAD_FILE.stem, logger=logger)
                if result.stderr.strip(): raise DCIRSessionException(ErrorCode.CONVERT_DSGN_TO_ODB_FAIL, result.returncode)

            # [PDN 수정] EDB 직접 추출
            force_export_edb_in_temp = bool(temp_preconv) and (EDB_FILE_PATH is None) and bool(DSGN_FILE)
            if force_export_edb_in_temp:
                logger.log(
                    "[TEMP] .aedb is missing. Forcing DSGN->EDB export even when config exportEDB=false.",
                    level=LogLevel.WARNING,
                )

            if EDB_FILE_PATH is None and (conf_manager.data['DCIR']['exportEDB'] or force_export_edb_in_temp):
                DSGN2EDB_EXEC = ZUKEN_BIN_DIR / 'DFaedbout.exe'
                EDB_FILE_PATH = DSGN_FILE.with_suffix('.aedb')
                result = subprocess.run([str(DSGN2EDB_EXEC), '-r', str(DSGN_FILE), '-o', str(EDB_FILE_PATH)], capture_output=True, text=True)
                if result.returncode: raise DCIRSessionException(ErrorCode.CONVERT_DSGN_TO_EDB_FAIL, result.returncode)
                logger.log("Zuken에서 원본 EDB 직접 추출 완료", level=LogLevel.DETAIL1)

            if EDB_FILE_PATH is None:
                fallback_edb = DSGN_FILE.with_suffix('.aedb')
                if fallback_edb.exists():
                    EDB_FILE_PATH = fallback_edb
                    logger.log(f"[TEMP] Reusing existing EDB without conversion: {EDB_FILE_PATH}", level=LogLevel.WARNING)
                else:
                    raise DCIRSessionException(
                        ErrorCode.INPUT_FILE_NOT_FOUND,
                        f"EDB file not found after conversion skip: {fallback_edb}",
                    )
        else:
            # Zuken이 아닌 경우 기존 파일 매핑
            anf_files = list(INPUT_CAD_FILE.parent.glob('*.anf'))
            cmp_files = list(INPUT_CAD_FILE.parent.glob('*.cmp'))
            if len(anf_files) == 1 and len(cmp_files) == 1: ANF_FILE, CMP_FILE = anf_files[0], cmp_files[0]
            else: raise DCIRSessionException(ErrorCode.INVALID_ANF_FILE_NUM, anf_files)
            EDB_FILE_PATH = INPUT_CAD_FILE.with_suffix('.aedb')
    else:
        raise DCIRSessionException(ErrorCode.INVALID_CAD_FILE, INPUT_CAD_FILE)

    # [PDN 수정] app.create_project(ANF_FILE, CMP_FILE, ...) 호출을 생략하여 
    # ANF/CMP를 통한 SIwave 프로젝트 생성을 방지합니다.
    base_name = INPUT_CAD_FILE.stem.split('-')[0]
    SIwave_FILE_PATH = INPUT_DIR / f'{base_name}.siw'

except Exception:
    logger.fatal(f"An error occurred while getting CAD database: {traceback.format_exc()}")
    raise DCIRSessionException(ErrorCode.CAD_IMPORT_FAIL)
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

    logger.log(f"Find Ground Net", level=LogLevel.DETAIL1)
    power_net_areas = {
        net_name: sum(p.area() for p in net_inst.primitives if p.type != 'Path')
        for net_name, net_inst in app.edb._nets.power.items()
        if conf_manager.data['DCIR']['dcShort']['shortKey'] not in net_name and '+' not in net_name
    }
    if power_net_areas: GND_NET = max(power_net_areas, key=power_net_areas.get)
    else: raise DCIRSessionException(ErrorCode.GND_NET_DETECT_FAIL)

    BOM_FILE = input_valchk._default_inputFiles['BOM']
    settings_manager.parse_bom_and_partlist(BOM_FILE)
    bom_info = settings_manager.get_bom() 

    delComp, missing_in_bom = {}, []
    exclude_prefixes = tuple(conf_manager.data['DCIR']['dcShort'].get('excludePrefixes', ['AR', 'JK', 'P', 'IC', 'X', 'D']))
    delete_types = conf_manager.data['DCIR']['dcShort'].get('deleteCompTypes', ['IC', 'IO', 'Other'])

    for comp_name, comp_inst in app.edb._components.components.items():
        # [PDN 수정] 커패시터(C) 무조건 보존
        if str(comp_name).upper().startswith('C'):
            continue
            
        if comp_name in bom_info['Designators']: continue
        elif not comp_name.startswith(exclude_prefixes) and comp_inst.component_def in conf_manager.data['DCIR']['dcShort']['shortedComp']: continue
        else:
            if comp_inst.type in delete_types:
                delComp[comp_name] = comp_inst
                missing_in_bom.append(comp_name) 
            else: comp_inst.enabled = False

    SHORT_CORRECTION, DEL_COMP = {}, set()
    for comp_name, comp_inst in app.edb._components.components.items():
        if comp_name.startswith(exclude_prefixes) or comp_inst.component_def not in conf_manager.data['DCIR']['dcShort']['shortedComp']: continue
        target_nets = app.edb.nets.nets_by_components[comp_name]
        if len(target_nets) != 2: continue
        net1, net2 = target_nets
        short_key = conf_manager.data['DCIR']['dcShort']['shortKey']
        primary, secondary = (net2, net1) if short_key in net1 or (short_key not in net2 and len(net1) > len(net2)) else (net1, net2)
        SHORT_CORRECTION.setdefault(primary, []).append(secondary)
        DEL_COMP.add(comp_name)

    SPEC_FILE = input_valchk._default_inputFiles['Spec']
    settings_manager.parse_spec(SPEC_FILE)
    spec_info = settings_manager.get_spec()
    
    dcir_cases_info = []
    inner_cap_audit = []
    designator_list = {case['Designator'] for case in spec_info}
    
    time.sleep(3.0)
    try:
        _dummy_count = len(app.edb._components.components)
        logger.log(f"Successfully loaded {_dummy_count} components from EDB.", level=LogLevel.DETAIL1)
    except Exception as e:
        logger.log(f"[WARNING] Failed to pre-load EDB components: {e}", level=LogLevel.WARNING)

    def normalize_name(name):
        return re.sub(r'[^A-Za-z0-9]', '', str(name)).upper()

    # ... (기존 코드: normalized_edb_components 생성 완료 부분) ...
    normalized_edb_components = {
        normalize_name(c_name): c_inst 
        for c_name, c_inst in app.edb._components.components.items()
    }

    # =================================================================
    # [PDN 수정] Inner Cap 생성 및 연결 로직 추가 (방어 코드 적용)
    # =================================================================
    INNER_CAP_FILE = settings_manager.data.get('CAE', {}).get('SOC', {}).get('Inner_cap')
    GND_NET = settings_manager.get_gnd_net()  # 💡 [방어 3] 하드코딩 제거 및 동적 할당

    if INNER_CAP_FILE:
        inner_cap_path = INPUT_DIR / INNER_CAP_FILE
        
        # 💡 [방어 4] 스크립트 재실행 시 이름 중복(Collision) 방지를 위한 사전 삭제
        for comp_name in list(app.edb.components.components.keys()):
            if comp_name.startswith("C_INNER_"):
                try:
                    app.edb.components.components[comp_name].delete()
                except Exception as e:
                    logger.log(f"[WARNING] 기존 Inner Cap({comp_name}) 삭제 실패: {e}", level=LogLevel.WARNING)

        # SettingsManager를 통해 파일 파싱 및 데이터 가져오기
        if settings_manager.parse_inner_cap(inner_cap_path):
            inner_caps = settings_manager.get_inner_cap()
            
            for idx, icap in enumerate(inner_caps):
                ic_refdes = icap['Designator']
                pin_no = icap['Pin_Number']
                cap_val = icap['Cap_Value']
                cap_name = f"C_INNER_{ic_refdes}_{pin_no}_{idx}"
                audit_item = {
                    "index": idx + 1,
                    "component_name": cap_name,
                    "designator": ic_refdes,
                    "pin_number": pin_no,
                    "cap_value": cap_val,
                    "status": "pending",
                    "message": "",
                }
                
                # 1. 타겟 IC 부품 찾기
                norm_ic_name = normalize_name(ic_refdes)
                ic_inst = normalized_edb_components.get(norm_ic_name)
                
                if not ic_inst:
                    logger.log(f"[WARNING] Inner Cap 타겟 IC({ic_refdes})를 EDB에서 찾을 수 없습니다.", level=LogLevel.WARNING)
                    audit_item["status"] = "target_ic_not_found"
                    audit_item["message"] = "Target IC not found in EDB"
                    inner_cap_audit.append(audit_item)
                    continue
                
                # 2. 타겟 핀 찾기 및 위치/Net 정보 추출
                pin_inst = ic_inst.pins.get(pin_no)
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
        
        comp_inst = normalized_edb_components.get(norm_comp_name)
        
        if comp_inst is None:
            logger.log(f"[ERROR] Component '{comp_name}' (Normalized: {norm_comp_name}) not found in EDB. Skipping this case.", level=LogLevel.ERROR)
            continue

        target_pin_name = str(case['Pin_number']).strip()
        pin_inst, actual_pin_name = comp_inst.pins.get(target_pin_name), target_pin_name

        if not pin_inst:
            available_pins_info = {p_name: p_inst.net_name for p_name, p_inst in comp_inst.pins.items() if p_inst.net_name}
            target_net_from_spec = case.get('Net_Name') or case.get('Target_Net') or case.get('Net_name') or case.get('Net')
            if target_net_from_spec:
                for p_name, net_name in available_pins_info.items():
                    if net_name == target_net_from_spec:
                        pin_inst, actual_pin_name = comp_inst.pins[p_name], p_name
                        break
            if not pin_inst: raise DCIRSessionException(ErrorCode.TARGET_NET_TRACE_FAIL, target_pin_name, comp_name)

        db.other_nets[pin_inst.net.name] = []
        
        result, net_chain = find_bulkInd(app.edb, pin_inst.net, designator_list, bom_info, GND_NET, target_ic=comp_name)
        if isinstance(result, ErrorCode): net_chain = []

        dcdc_pin_name = next((p_name for s_net in reversed(net_chain + [pin_inst.net.name]) 
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

        if 'Voltage_(V)' not in case or 'Current_(A)' not in case:
            logger.log(
                f"[WARNING] Spec format without DCIR V/I columns detected for {comp_name}:{actual_pin_name}. "
                f"Fallback V={vmag}, I={imag} applied.",
                level=LogLevel.WARNING,
            )

        dcir_cases_info.append({
            'IC': comp_name, 'IC_pin': actual_pin_name, 'Net': pin_inst.net.name,
            'DCDC_name': result.name if not isinstance(result, ErrorCode) else "",
            'DCDC_pin': dcdc_pin_name, 'DCDC_net': net_chain, 
            'Full_Net_Chain': full_chain, 
            'Vmag': vmag, 'Imag': imag,
            'MinSpec': min_spec, 'MaxSpec': max_spec
        })

    target_nets = {GND_NET} | {n for case in dcir_cases_info for n in case.get('Full_Net_Chain', [])}

    if not app.sanitize_nets(target_nets): raise DCIRSessionException(ErrorCode.SANITIZE_FAIL)

    dcir_logic = DCIR(logger=logger)

    traced_net_pairs = set()
    for case in dcir_cases_info:
        full_chain = case.get('Full_Net_Chain', []) 
        for i in range(len(full_chain) - 1):
            net1, net2 = full_chain[i], full_chain[i+1]
            if net1 != net2:
                traced_net_pairs.add(tuple(sorted([net1, net2])))

    zero_ohm_candidates = [k for k, v in conf_manager.data['DCIR']['BOM']['compProp'].items() if 'connectPin' in v]

    for comp_type in zero_ohm_candidates:
        target_comps = bom_info.get(comp_type, [])
        if not target_comps: continue
        
        original_prop = conf_manager.data['DCIR']['BOM']['compProp'][comp_type]
        connect_pins_config = original_prop.get('connectPin', [])
        
        pin_pairs = connect_pins_config if (connect_pins_config and isinstance(connect_pins_config[0], list)) else [connect_pins_config]
            
        for comp_name in target_comps:
            norm_name = re.sub(r'[^A-Za-z0-9]', '', str(comp_name)).upper()
            comp_inst = normalized_edb_components.get(norm_name)
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
                        
                        dcir_logic.install_0ohm_resistors(
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
    step += 1

except Exception:
    logger.fatal(f"Modify CAD Data using EDB database : {traceback.format_exc()}")
    raise
finally:
    if app:
        app.quit_application()
# endregion

if STAGE == "pre":
    try:
        logger.log(
            f"Step {step}. PRE report export (DCIR setup and simulation steps are skipped by design)",
            level=LogLevel.INFO,
        )
        spec_file_for_report = Path(input_valchk._default_inputFiles['Spec']) if input_valchk else INPUT_JSON
        export_pre_stage_reports(
            OUTPUT_DIR,
            spec_file_for_report,
            PRE_EDB_FILE_PATH,
            dcir_cases_info,
            inner_cap_audit,
            logger,
        )
    except Exception:
        logger.fatal(f"Failed to export pre-stage reports: {traceback.format_exc()}")
        raise SystemExit(1)
    END_TIME = time.strftime('%Y.%m.%d, %H:%M:%S')
    raise SystemExit(0)

# region 5. Modify CAD Data using SIwave and Set DCIR Simulation
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

        dcir_logic = DCIR(logger=logger)
        dcir_logic.apply_dc_shorts(
            app=app, 
            shorted_comp_defs=conf_manager.data['DCIR']['dcShort']['shortedComp'], 
            del_comps=DEL_COMP, 
            short_correction=SHORT_CORRECTION
        )

        PMAP_FILE = INPUT_DIR / settings_manager.data['CAE']['PCB']['Pmap'] if settings_manager.data['CAE']['PCB']['Pmap'] else None
        SWS_FILE = WORKING_DIR / 'core' / conf_manager.data['DCIR']['sws']
        
        app.setup_simulation(PMAP_FILE, SWS_FILE)

        REF_SIwave_FILE_PATH = SIwave_FILE_PATH.parent / f"{SIwave_FILE_PATH.stem}_ref{SIwave_FILE_PATH.suffix}"
        app.save_project_as(REF_SIwave_FILE_PATH)

        base_cad_name = INPUT_CAD_FILE.stem.split('-')[0]
        FINAL_EDB_FILE_PATH = OUTPUT_DIR / f"{base_cad_name}_ref.aedb"
        app.export_edb(FINAL_EDB_FILE_PATH)
    finally:
        if app:
            app.quit_application()

    try:
        image_app = SIwave(version=AEDT_VERSION, logger=logger)
        image_app.set_cad_file(str(FINAL_EDB_FILE_PATH))
        image_app.export_layer_images(REF_SIwave_FILE_PATH, OUTPUT_DIR, GND_NET)
        image_app.close_edb()
    finally:
        if image_app:
            image_app.quit_application()

    step += 1

except Exception:
    logger.fatal(f"An error occurred while CAD modification process : {traceback.format_exc()}")
# endregion

# region 6. Generate Files and Run DCIR Simulation
app = None
try:
    logger.log(f"Step {step}. Generate Files and Run DCIR Simulation", level=LogLevel.INFO)
    preprocessing_data = []
    MODEL_NAME = INPUT_CAD_FILE.stem.split('-')[0]

    app = SIwave(version=AEDT_VERSION, logger=logger)
    app.set_cad_file(str(PRE_EDB_FILE_PATH))
    signal_layers = list(app.edb.stackup.signal_layers.keys())

    siw_execute_file = resolve_siwave_executable(AEDT_VERSION)
    exec_file = WORKING_DIR / 'core' / 'DCIR.exec'

    for idx, case in enumerate(dcir_cases_info):
        case_record = run_dcir_case(
            case=case,
            idx=idx,
            total_cases=len(dcir_cases_info),
            mode=MODE,
            model_name=MODEL_NAME,
            output_dir=OUTPUT_DIR,
            ref_siwave_file_path=REF_SIwave_FILE_PATH,
            gnd_net=GND_NET,
            aedt_version=AEDT_VERSION,
            case_data_app=app,
            signal_layers=signal_layers,
            conf_data=conf_manager.data,
            siw_execute_file=siw_execute_file,
            exec_file=exec_file,
            bulk_inductor_list=bom_info.get('bulkInd', []), 
            run_solve=(STAGE != "pre")
        )
        if case_record:
            preprocessing_data.append(case_record)

    app.close_edb()

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
        app.quit_application()
    END_TIME = time.strftime('%Y.%m.%d, %H:%M:%S')
# endregion

# region 8. Post-Processing
if STAGE == "pre":
    logger.log(f"Step {step}. Post-processing skipped (stage=pre)", level=LogLevel.INFO)
else:
    try:
        logger.log(f"Step {step}. Post-processing : Extracting DCIR results", level=LogLevel.INFO)
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
        logger.fatal(f"An error occurred while performing DCIR results extracting : {traceback.format_exc()}")
# endregion
