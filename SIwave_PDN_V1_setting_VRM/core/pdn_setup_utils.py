# coding=utf-8

import json
import re
import os
from pathlib import Path

from core.logger import LogLevel

# =========================================================================
# S-Parameter 유틸리티 함수
# =========================================================================
def find_s2p_file_for_maker(maker_part_number: str, s2p_dir: Path | None):
    if not maker_part_number or not s2p_dir or not s2p_dir.exists():
        return None
    direct = s2p_dir / f"{maker_part_number}.s2p"
    if direct.exists():
        return direct
    for p in s2p_dir.glob("*.s2p"):
        if p.stem.strip().lower() == maker_part_number.strip().lower():
            return p
    return None


def detect_s2p_port_count(s2p_file: Path | str):
    try:
        s2p_path = Path(s2p_file)
        name = s2p_path.name.lower()
        if ".s1p" in name:
            return 1
        if ".s2p" in name:
            return 2
        with open(s2p_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.lstrip().startswith("!"):
                    continue
                vals = [v for v in re.split(r"\s+", line.strip()) if v]
                if len(vals) == 3:
                    return 1
                if len(vals) >= 9:
                    return 2
                break
    except Exception:
        return 2
    return 2


def _norm_part_token(text: str):
    return re.sub(r"[^A-Za-z0-9]", "", str(text or "").upper())


def _maker_candidates(maker_part_number: str):
    raw = str(maker_part_number or "").strip().strip("'").strip('"')
    if not raw:
        return []
    cands = [raw]
    no_space = re.sub(r"\s+", "", raw)
    if no_space != raw:
        cands.append(no_space)
    if "-" in raw:
        cands.append(raw.split("-", 1)[0])
    # unique preserve order
    out = []
    seen = set()
    for c in cands:
        key = c.lower()
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


def _build_s2p_index(s2p_dir: Path):
    exact = {}
    norm = {}
    if not s2p_dir or not s2p_dir.exists():
        return exact, norm
    for p in s2p_dir.rglob("*.s2p"):
        stem = p.stem.strip()
        if stem:
            exact.setdefault(stem.lower(), p)
            n = _norm_part_token(stem)
            if n and n not in norm:
                norm[n] = p
    return exact, norm


def _find_s2p_by_maker(maker_part_number: str, exact_index, norm_index):
    for cand in _maker_candidates(maker_part_number):
        p = exact_index.get(cand.lower())
        if p:
            return p
        p = norm_index.get(_norm_part_token(cand))
        if p:
            return p
    return None


def export_innercap_s2p_registry(output_dir: Path, inner_caps, pmap_file: Path | None = None, logger=None, s2p_dir: Path | None = None):
    """
    Inner Cap의 S-Parameter 매핑 결과를 JSON 리포트로 출력합니다.
    (Pmap 로직이 제거되어 파싱된 Maker PN을 직접 사용합니다.)
    """
    records = []
    for item in inner_caps:
        src_part = item.get("part_number", "")
        maker_pn = item.get("maker_part_number", "")
        s2p = find_s2p_file_for_maker(maker_pn, s2p_dir) if maker_pn else None
        
        records.append(
            {
                "component_name": item.get("component_name", ""),
                "designator": item.get("designator", ""),
                "pin_number": item.get("pin_number", ""),
                "source_part_number": src_part,
                "maker_part_number": maker_pn,
                "s2p_file": str(s2p) if s2p else "",
                "status": item.get("status", ""),
            }
        )
        
    out = {
        "Schema_Version": 2,
        "S2P_Directory": str(s2p_dir) if s2p_dir else "",
        "Records": records,
    }
    
    out_file = output_dir / "innercap_s2p_linkage.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=4, ensure_ascii=False)
        
    if logger:
        logger.log(f"[PRE] Exported inner cap S2P linkage report: {out_file}", level=LogLevel.DETAIL1)


def assign_sparameter_models(
    app,
    bom_info,
    inner_cap_audit,
    pmap_file: Path | None = None,
    s2p_dir: Path | None = None,
    gnd_net: str = "GND",
    output_dir: Path = None,
    logger=None,
    innercap_csv_path: Path | None = None,
    bom_file_path: Path | None = None,
    search_roots=None,
):
    """
    BOM 및 Inner Cap 데이터를 기반으로 Capacitor에만 S-Parameter 모델을 할당합니다.
    Pmap을 거치지 않고 파싱된 데이터의 Maker PN을 직접 활용합니다.
    """
    # 1. 고정된 S2P 파일 경로 지정
    fixed_s2p_dir = Path(r"C:\Program Files\AnsysEM\v242\Win64\complib\Unlocked\s2p_file")
    effective_s2p_dir = Path(s2p_dir) if s2p_dir else fixed_s2p_dir
    if not effective_s2p_dir.exists() and fixed_s2p_dir.exists():
        effective_s2p_dir = fixed_s2p_dir
    s2p_dir_exists = effective_s2p_dir.exists()
    s2p_exact, s2p_norm = _build_s2p_index(effective_s2p_dir) if s2p_dir_exists else ({}, {})
    
    if not s2p_dir_exists and logger:
        logger.log(f"[WARNING] S2P directory not found: {effective_s2p_dir}", level=LogLevel.WARNING)

    # 2. Maker PN 매핑 딕셔너리 구성
    # BOM 데이터 매핑 (Designator -> Maker PN)
    bom_maker_map = {str(k): str(v) for k, v in bom_info.get("RefDesToMakerPartNumber", {}).items()}
    
    # Inner Cap 데이터 매핑 (Component Name -> Maker PN)
    innercap_maker_map = {}
    for item in inner_cap_audit:
        comp_name = item.get("component_name", "")
        maker_pn = item.get("maker_part_number", "")
        if comp_name and maker_pn:
            innercap_maker_map[comp_name] = maker_pn

    records = []
    reason_counter = {}

    def append_record(comp_name, category, maker_pn, s2p_path, status, reason="", assign_ok=False, port_count=None, pos_pin="", neg_pin=""):
        if reason:
            reason_counter[reason] = reason_counter.get(reason, 0) + 1
        records.append({
            "component": comp_name,
            "category": category,
            "maker_part_number": maker_pn,
            "s2p_file": str(s2p_path) if s2p_path else "",
            "status": status,
            "reason": reason,
            "assigned": bool(assign_ok),
            "port_count": port_count if port_count is not None else "",
            "pos_pin": pos_pin,
            "neg_pin": neg_pin,
        })

    # 3. EDB 프로젝트 내 부품 순회 및 할당
    for comp_name, comp_inst in app.edb._components.components.items():
        comp_name_str = str(comp_name)
        
        # Capacitor 필터링 (C로 시작하지 않거나 C_IC인 경우 스킵)
        if not comp_name_str.upper().startswith("C"):
            continue

        category = "GeneralCap"
        maker_pn = ""
        
        # Inner Cap에서 먼저 검색 후, 없으면 BOM에서 검색
        if comp_name_str in innercap_maker_map:
            category = "InnerCap"
            maker_pn = innercap_maker_map[comp_name_str]
        elif comp_name_str.upper().startswith("C_IC"):
            category = "InnerCap"
        elif comp_name_str in bom_maker_map:
            maker_pn = bom_maker_map[comp_name_str]
            
        if not maker_pn:
            append_record(comp_name_str, category, "", None, "Skipped", "No Maker PN found")
            continue
            
        if not s2p_dir_exists:
            append_record(comp_name_str, category, maker_pn, None, "Skipped", "S2P directory not found")
            continue

        s2p_path = _find_s2p_by_maker(maker_pn, s2p_exact, s2p_norm)
        if not s2p_path:
            append_record(comp_name_str, category, maker_pn, None, "Skipped", "No .s2p file")
            continue

        # 핀 개수 및 포트 확인
        port_count = detect_s2p_port_count(s2p_path)
        pins = list(comp_inst.pins.keys())
        pin_count = len(pins)
        
        if pin_count < 2:
            append_record(comp_name_str, category, maker_pn, s2p_path, "Skipped", "Pin count < 2")
            continue
        if category == "InnerCap" and pin_count > 2:
            append_record(comp_name_str, category, maker_pn, s2p_path, "Skipped", f"Unsupported pin count: {pin_count}")
            continue

        pos_pin = next((p for p, pin in comp_inst.pins.items() if pin.net_name != gnd_net), pins[0] if pins else "")
        neg_pin = next((p for p, pin in comp_inst.pins.items() if pin.net_name == gnd_net), pins[1] if len(pins) > 1 else "")
        
        # S-Parameter 할당 실행
        try:
            ok = app.assign_sparameter_model(comp_name_str, str(s2p_path), port_count=port_count, pos_pin=pos_pin, neg_pin=neg_pin)
            if ok:
                append_record(comp_name_str, category, maker_pn, s2p_path, "Done", "Assigned", True, port_count, str(pos_pin), str(neg_pin))
            else:
                append_record(comp_name_str, category, maker_pn, s2p_path, "Skipped", "Assign API failed", False, port_count, str(pos_pin), str(neg_pin))
        except Exception as e:
            append_record(comp_name_str, category, maker_pn, s2p_path, "Skipped", f"Exception: {e}", False, port_count, str(pos_pin), str(neg_pin))

    # 4. 결과 리포트 생성 및 저장
    done_count = sum(1 for r in records if r["status"] == "Done")
    report = {
        "Schema_Version": 2,
        "S2P_Directory": str(effective_s2p_dir),
        "S2P_Directory_Exists": s2p_dir_exists,
        "S2P_Index_Count": len(s2p_exact),
        "Total_Capacitors_Processed": len(records),
        "Done": done_count,
        "Skipped": len(records) - done_count,
        "Reason_Summary": reason_counter,
        "Records": records,
    }
    
    if output_dir:
        out_file = output_dir / "sparam_assignment_result.json"
        try:
            with open(out_file, "w", encoding="utf-8") as f:
                json.dump(report, f, indent=4, ensure_ascii=False)
            if logger:
                logger.log(f"[SParam] Exported assignment report: {out_file}", level=LogLevel.DETAIL1)
        except Exception as e:
            if logger:
                logger.log(f"[WARNING] Failed to write report: {e}", level=LogLevel.WARNING)

    if logger:
        logger.log(
            f"[SParam] Diagnostics: s2p_dir_exists={s2p_dir_exists}, "
            f"total_processed={len(records)}, assigned={done_count}, reasons={reason_counter}",
            level=LogLevel.INFO,
        )
    return report


# =========================================================================
# Stackup 및 Profile 유틸리티 함수
# =========================================================================
def resolve_zparam_profile(conf_data, stackup_file: Path | None, layer_count: int | None):
    zconf = conf_data.get("PDN", {}).get("zParamSetup", {})
    profiles = zconf.get("profiles", {})
    default_profile = zconf.get("defaultProfile", "2L")

    profile_key = default_profile
    if layer_count == 4:
        profile_key = "4L"
    elif layer_count == 2:
        profile_key = "2L"
    elif stackup_file:
        name = str(stackup_file).upper()
        if "4L" in name:
            profile_key = "4L"
        elif "2L" in name:
            profile_key = "2L"

    selected = profiles.get(profile_key, {})
    sws_name = selected.get("sws", conf_data.get("PDN", {}).get("sws", "PDN_Fast.sws"))
    sfsdf_name = selected.get("sfsdf", "")
    return profile_key, sws_name, sfsdf_name


def prepare_stackup_for_project(app, stackup_path: Path | None, work_dir: Path, logger):
    """
    Read stackup text, normalize thickness numeric format, and
    align layer-name casing with the current EDB signal layer names.
    If any patch is applied, write a temporary stackup file and return it.
    """
    if not stackup_path or not Path(stackup_path).exists(): 
        return stackup_path
        
    try: 
        src_text = Path(stackup_path).read_text(encoding="utf-8-sig", errors="ignore")
    except Exception as e: 
        if logger:
            logger.log(f"[WARNING] Failed to read stackup file: {e}", level=LogLevel.WARNING)
        return stackup_path

    changed = False

    # 1) Normalize thickness/elevation numeric string to plain numbers only.
    # Legacy import path may provide numeric fields without unit suffix.
    # Keep the same behavior to avoid SIwave parsing 0 mm thickness.
    thickness_pattern = re.compile(r"(?i)(Thickness\s*=\s*['\"]?)([+-]?\d*\.?\d+(?:[eE][+-]?\d+)?)\s*([a-zA-Z]*)(['\"]?)")
    elevation_pattern = re.compile(r"(?i)(Elevation\s*=\s*['\"]?)([+-]?\d*\.?\d+(?:[eE][+-]?\d+)?)\s*([a-zA-Z]*)(['\"]?)")
    
    def _fix_thickness(match):
        nonlocal changed
        prefix = match.group(1)
        val_str = match.group(2)
        _unit_str = match.group(3).strip()
        suffix = match.group(4)
        
        try:
            val_float = float(val_str)
            clean_val_str = f"{val_float:.6f}".rstrip('0').rstrip('.')
            if not clean_val_str:
                clean_val_str = "0"
            
            new_val = f"{prefix}{clean_val_str}{suffix}"
            
            if new_val != match.group(0):
                changed = True
                
            return new_val
            
        except ValueError:
            return match.group(0)

    patched_text = thickness_pattern.sub(_fix_thickness, src_text)
    patched_text = elevation_pattern.sub(_fix_thickness, patched_text)

    # 2) Normalize LayerName token casing to match EDB layer names.
    try: 
        edb_layers = list(app.edb.stackup.signal_layers.keys()) if app and app.edb else []
    except Exception: 
        edb_layers = []
        
    if edb_layers:
        case_map = {str(name).upper(): str(name) for name in edb_layers}
        
        def _replace_layer_name(match):
            nonlocal changed
            original = match.group(1)
            mapped = case_map.get(str(original).upper(), original)
            if mapped != original: 
                changed = True
            return f"LayerName='{mapped}'"

        patched_text = re.sub(r"LayerName='([^']+)'", _replace_layer_name, patched_text)

    # 3. 변경사항이 없으면 원본 stackup을 그대로 사용
    if not changed:
        return stackup_path

    patched_path = work_dir / f"{Path(stackup_path).stem}_autocase.stk"
    try:
        patched_path.write_text(patched_text, encoding="utf-8")
        if logger:
            logger.log(f"[PRE] Stackup file patched and saved to: {patched_path}", level=LogLevel.DETAIL1)
        return patched_path
    except Exception as e:
        if logger:
            logger.log(f"[WARNING] Failed to write patched stackup: {e}", level=LogLevel.WARNING)
        return stackup_path
