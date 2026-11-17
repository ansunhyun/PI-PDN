# coding=utf-8

import json
import csv
import re
import os
import math
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


def _touchstone_freq_multiplier_from_header(line: str):
    tok = [t.strip().upper() for t in str(line or "").split() if t.strip()]
    for unit, mul in (("GHZ", 1e9), ("MHZ", 1e6), ("KHZ", 1e3), ("HZ", 1.0)):
        if unit in tok:
            return mul
    return 1.0


def detect_touchstone_freq_max_hz(s2p_file: Path | str):
    try:
        path = Path(s2p_file)
        if not path.exists():
            return None
        freq_mul = 1.0
        fmax = None
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for raw in f:
                line = str(raw).strip()
                if not line or line.startswith("!"):
                    continue
                if line.startswith("#"):
                    freq_mul = _touchstone_freq_multiplier_from_header(line)
                    continue
                vals = [v for v in re.split(r"\s+", line) if v]
                if not vals:
                    continue
                try:
                    freq_hz = float(vals[0]) * freq_mul
                except Exception:
                    continue
                if fmax is None or freq_hz > fmax:
                    fmax = freq_hz
        return fmax
    except Exception:
        return None



def detect_touchstone_dc_imag_at_zero_hz(s2p_file: Path | str, imag_tol: float = 1e-9, freq_tol_hz: float = 1.0):
    """
    Return diagnostic dict for DC (0Hz) imaginary-part validity of touchstone data.
    - ok=True  : no zero-Hz row found, or zero-Hz row has imag ~= 0 for all pairs
    - ok=False : zero-Hz row exists and at least one pair has |imag| > imag_tol
    """
    try:
        path = Path(s2p_file)
        if not path.exists():
            return {"ok": False, "reason": "file_not_found"}

        freq_mul = 1.0
        data_format = "RI"
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for raw in f:
                line = str(raw).strip()
                if not line or line.startswith("!"):
                    continue
                if line.startswith("#"):
                    freq_mul = _touchstone_freq_multiplier_from_header(line)
                    tok = [t.strip().upper() for t in line.split() if t.strip()]
                    for fmt in ("RI", "MA", "DB"):
                        if fmt in tok:
                            data_format = fmt
                            break
                    continue

                vals = [v for v in re.split(r"\s+", line) if v]
                if len(vals) < 3:
                    continue
                try:
                    freq_hz = float(vals[0]) * freq_mul
                except Exception:
                    continue

                # Only inspect true DC row (or numeric near-zero)
                if abs(freq_hz) > float(freq_tol_hz):
                    continue

                pair_vals = vals[1:]
                if len(pair_vals) < 2:
                    return {"ok": False, "reason": "invalid_data_row", "format": data_format, "freq_hz": freq_hz}

                max_imag = 0.0
                for i in range(0, len(pair_vals) - 1, 2):
                    try:
                        a = float(pair_vals[i])
                        b = float(pair_vals[i + 1])
                    except Exception:
                        continue

                    if data_format == "RI":
                        imag = b
                    elif data_format == "MA":
                        imag = a * math.sin(math.radians(b))
                    else:  # DB
                        mag = 10.0 ** (a / 20.0)
                        imag = mag * math.sin(math.radians(b))
                    if abs(imag) > max_imag:
                        max_imag = abs(imag)

                if max_imag > float(imag_tol):
                    return {
                        "ok": False,
                        "reason": "dc_imag_nonzero",
                        "format": data_format,
                        "freq_hz": freq_hz,
                        "max_imag": max_imag,
                        "imag_tol": float(imag_tol),
                    }
                return {
                    "ok": True,
                    "reason": "dc_ok",
                    "format": data_format,
                    "freq_hz": freq_hz,
                    "max_imag": max_imag,
                    "imag_tol": float(imag_tol),
                }

        # No zero-Hz row -> pass (let solver handle interpolation/extrapolation)
        return {"ok": True, "reason": "no_dc_row"}
    except Exception as e:
        return {"ok": False, "reason": f"dc_check_exception:{e}"}
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




def _is_transient_s2p_source(path_obj: Path) -> bool:
    """Exclude transient solver/result folders from S2P model source candidates."""
    try:
        low = str(path_obj).lower()
        if ".siwaveresults" in low or "\\siwaveresults" in low:
            return True
        if "\\outputs\\" in low and (".siw" in low or "siwaveresults" in low):
            return True
    except Exception:
        return False
    return False


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


_MODEL_MAPPING_SOURCE_KEYS = {"source", "original", "bompartnumber", "makerpartnumber", "requestedpartnumber", "from", "sourcepn", "originalpn"}
_MODEL_MAPPING_TARGET_KEYS = {"target", "substitute", "replacement", "modelpartnumber", "s2ppartnumber", "to", "targetpn", "filename", "s2pfile", "selectedpn"}


def _mapping_field_key(value):
    return re.sub(r"[^a-z0-9]", "", str(value or "").lower())


def _mapping_target_part(value):
    text = str(value or "").strip().strip("'\"")
    return Path(text).stem if text.lower().endswith(".s2p") else text


def _mapping_pair_from_record(record):
    if not isinstance(record, dict):
        return None
    normalized = {_mapping_field_key(key): value for key, value in record.items()}
    if 'originalpn' in normalized and normalized.get('status') not in ('exact_model_selected', 'selected_bom_spec_substitute'):
        return None
    source = next((normalized[key] for key in _MODEL_MAPPING_SOURCE_KEYS if key in normalized), "")
    target = next((normalized[key] for key in _MODEL_MAPPING_TARGET_KEYS if key in normalized), "")
    source, target = str(source or "").strip(), _mapping_target_part(target)
    return (source, target) if source and target else None


def load_model_mapping_files(search_dirs):
    """Load Pmap replacement relationships from JSON/CSV without one fixed schema."""
    filenames = (".model-mapping.json", ".model-mapping.csv", "model-mapping.json", "model-mapping.csv", "*.model-mapping.json")
    files, seen = [], set()
    for directory in search_dirs or []:
        root = Path(directory)
        for name in filenames:
            # Pmap generation stores the mapping beside the generated S2P.
            # S2P libraries commonly use one subdirectory per part number, so
            # searching only the configured library root misses those files.
            try:
                candidates = root.rglob(name) if root.is_dir() else []
                for path in candidates:
                    key = str(path.resolve(strict=False)).lower()
                    if key not in seen and path.is_file():
                        seen.add(key)
                        files.append(path)
            except OSError:
                # An unreadable library branch must not block other roots.
                continue

    mappings, diagnostics = {}, []
    for path in files:
        pairs = []
        try:
            if path.suffix.lower() == ".csv":
                with open(path, "r", encoding="utf-8-sig", errors="replace", newline="") as stream:
                    for row in csv.DictReader(stream):
                        pair = _mapping_pair_from_record(row)
                        if pair:
                            pairs.append(pair)
            else:
                with open(path, "r", encoding="utf-8-sig") as stream:
                    payload = json.load(stream)
                records = []
                if isinstance(payload, list):
                    records = payload
                elif isinstance(payload, dict):
                    records = next((value for key, value in payload.items() if _mapping_field_key(key) in {"mappings", "mapping", "records", "models"} and isinstance(value, list)), None)
                    if records is None:
                        records = []
                        for source, target in payload.items():
                            if isinstance(target, (str, int, float)):
                                pairs.append((str(source).strip(), _mapping_target_part(target)))
                for record in records:
                    pair = _mapping_pair_from_record(record)
                    if pair:
                        pairs.append(pair)

            accepted = 0
            for source, target in pairs:
                source_key = _norm_part_token(source)
                if not source_key or not target:
                    continue
                entry = {"source": source, "target": target, "file": str(path)}
                bucket = mappings.setdefault(source_key, [])
                if not any(_norm_part_token(item["target"]) == _norm_part_token(target) for item in bucket):
                    bucket.append(entry)
                    accepted += 1
            diagnostics.append({"file": str(path), "status": "Loaded", "mapping_count": accepted})
        except Exception as exc:
            diagnostics.append({"file": str(path), "status": "ParseError", "error": str(exc)})
    return mappings, files, diagnostics


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
    analysis_nets=None,
    model_aliases=None,
    model_generation=None,
):
    """
    BOM 및 Inner Cap 데이터를 기반으로 Capacitor에만 S-Parameter 모델을 할당합니다.
    Pmap을 거치지 않고 파싱된 데이터의 Maker PN을 직접 활용합니다.
    """
    # 1. S2P 디렉터리 결정 (해석기 버전/환경 우선)
    def _candidate_s2p_dirs():
        candidates = []
        if s2p_dir:
            candidates.append(Path(s2p_dir))

        # Config-driven candidates (from step5_service -> search_roots)
        for root in (search_roots or []):
            if not root:
                continue
            root_path = Path(root)
            candidates.append(root_path)
            candidates.append(root_path / "Unlocked" / "s2p_file")
            candidates.append(root_path / "s2p")
            candidates.append(root_path / "S2P")
            candidates.append(root_path / "s2p_file")
            candidates.append(root_path / "Unlocked" / "DeCap")
            candidates.append(root_path / "DeCap")

        # ANSYSEM_ROOT-driven fallback
        for key, val in os.environ.items():
            if key.upper().startswith("ANSYSEM_ROOT") and str(val).strip():
                env_root = Path(str(val).strip())
                candidates.append(env_root / "complib" / "Unlocked" / "s2p_file")
                candidates.append(env_root / "complib")

        # Legacy fallback
        candidates.extend([
            Path(r"C:\Program Files\AnsysEM\v252\Win64\complib\Unlocked\s2p_file"),
            Path(r"C:\Program Files\AnsysEM\v242\Win64\complib\Unlocked\s2p_file"),
            Path(r"C:\Program Files\AnsysEM\v241\Win64\complib\Unlocked\s2p_file"),
        ])

        uniq = []
        seen = set()
        for cand in candidates:
            try:
                norm = str(cand.resolve(strict=False)).lower()
            except Exception:
                norm = str(cand).lower()
            if norm in seen:
                continue
            seen.add(norm)
            uniq.append(cand)
        return uniq

    def _has_any_s2p(path_obj: Path) -> bool:
        try:
            if not path_obj.exists() or not path_obj.is_dir():
                return False
            for _ in path_obj.rglob("*.s2p"):
                return True
            return False
        except Exception:
            return False

    effective_s2p_dir = None
    valid_s2p_dirs = []
    skipped_transient = 0
    for cand in _candidate_s2p_dirs():
        if _is_transient_s2p_source(cand):
            skipped_transient += 1
            continue
        if _has_any_s2p(cand):
            if effective_s2p_dir is None:
                effective_s2p_dir = cand
            valid_s2p_dirs.append(cand)
    if effective_s2p_dir is None:
        effective_s2p_dir = Path(s2p_dir) if s2p_dir else Path(r"C:\Program Files\ANSYS Inc\v252\AnsysEM\complib\Unlocked\s2p_file")

    s2p_dir_exists = effective_s2p_dir.exists()
    s2p_indexes = [_build_s2p_index(path) for path in valid_s2p_dirs]
    s2p_exact, s2p_norm = s2p_indexes[0] if s2p_indexes else ({}, {})
    model_mapping_map, model_mapping_files, model_mapping_diagnostics = load_model_mapping_files(valid_s2p_dirs)

    def _touchstone_has_nonzero_network_data(path_obj: Path) -> bool:
        """Reject generated placeholder files whose complete S matrix is zero."""
        try:
            pending = []
            with open(path_obj, "r", encoding="utf-8", errors="ignore") as stream:
                for raw in stream:
                    line = raw.split("!", 1)[0].strip()
                    if not line or line.startswith("#") or line.startswith("["):
                        continue
                    for token in line.replace(",", " ").split():
                        try:
                            pending.append(float(token))
                        except ValueError:
                            pass
            # A 2-port Touchstone row is frequency + eight network values.
            for offset in range(0, len(pending) - 8, 9):
                if any(abs(value) > 1e-18 for value in pending[offset + 1:offset + 9]):
                    return True
        except OSError:
            return False
        return False

    def _find_valid_model(part_number):
        rejected = []
        for exact_index, norm_index in s2p_indexes:
            candidate = _find_s2p_by_maker(part_number, exact_index, norm_index)
            if not candidate:
                continue
            if _touchstone_has_nonzero_network_data(candidate):
                return candidate, rejected
            rejected.append(str(candidate))
        return None, rejected

    def _find_mapped_model(part_number, max_hops=8):
        start = str(part_number or "").strip()
        queue, visited, rejected = [(start, [start], [])], {_norm_part_token(start)}, []
        while queue:
            current, chain, provenance = queue.pop(0)
            if len(chain) > max_hops:
                continue
            for entry in model_mapping_map.get(_norm_part_token(current), []):
                target, target_key = entry["target"], _norm_part_token(entry["target"])
                if not target_key or target_key in visited:
                    continue
                visited.add(target_key)
                next_chain, next_provenance = chain + [target], provenance + [entry["file"]]
                candidate, candidate_rejected = _find_valid_model(target)
                rejected.extend(candidate_rejected)
                if candidate:
                    return candidate, target, next_chain, next_provenance, rejected
                queue.append((target, next_chain, next_provenance))
        return None, "", [start], [], rejected
    
    if logger:
        logger.log(f"[SParam] Effective S2P source dir: {effective_s2p_dir}", level=LogLevel.INFO)
        if skipped_transient:
            logger.log(f"[SParam] Skipped transient S2P source candidates: {skipped_transient}", level=LogLevel.DETAIL1)
        for diagnostic in model_mapping_diagnostics:
            level = LogLevel.WARNING if diagnostic["status"] == "ParseError" else LogLevel.DETAIL1
            logger.log(f"[SParam][MODEL_MAPPING] {diagnostic}", level=level)

    if not s2p_dir_exists and logger:
        logger.log(f"[WARNING] S2P directory not found: {effective_s2p_dir}", level=LogLevel.WARNING)

    # 2. Maker PN 매핑 딕셔너리 구성
    # BOM 데이터 매핑 (Designator -> Maker PN)
    bom_maker_map = {str(k): str(v) for k, v in bom_info.get("RefDesToMakerPartNumber", {}).items()}
    model_alias_map = {
        str(k).strip().upper(): str(v).strip()
        for k, v in (model_aliases or {}).items()
        if str(k).strip() and str(v).strip()
    }
    
    # Inner Cap 데이터 매핑 (Component Name -> Maker PN)
    innercap_maker_map = {}
    for item in inner_cap_audit:
        comp_name = item.get("component_name", "")
        maker_pn = item.get("maker_part_number", "")
        if comp_name and maker_pn:
            innercap_maker_map[comp_name] = maker_pn

    records = []
    reason_counter = {}
    min_required_hz = 1e9  # Cutout solve policy: require coverage up to 1GHz.
    analysis_net_set = {str(n).strip().upper() for n in (analysis_nets or []) if str(n).strip()}
    gnd_net_upper = str(gnd_net or "").strip().upper()
    # GND is a reference net, not a sufficient reason by itself to select a
    # capacitor.  Keeping it in the selection set used to make almost every
    # board capacitor a target simply because one terminal was grounded.
    target_power_net_set = set(analysis_net_set)
    if gnd_net_upper:
        target_power_net_set.discard(gnd_net_upper)

    def append_record(comp_name, category, maker_pn, s2p_path, status, reason="", assign_ok=False, port_count=None, pos_pin="", neg_pin="", model_maker_pn="", model_source="", mapping_files=None, mapping_chain=None):
        if reason:
            reason_counter[reason] = reason_counter.get(reason, 0) + 1
        records.append({
            "component": comp_name,
            "category": category,
            "maker_part_number": maker_pn,
            "requested_maker_part_number": maker_pn,
            "model_maker_part_number": model_maker_pn or maker_pn,
            "model_source": model_source,
            "model_mapping_file": (mapping_files or [""])[-1] if mapping_files else "",
            "model_mapping_files": list(mapping_files or []),
            "model_mapping_chain": list(mapping_chain or ([maker_pn] if maker_pn else [])),
            "s2p_file": str(s2p_path) if s2p_path else "",
            "status": status,
            "reason": reason,
            "assigned": bool(assign_ok),
            "port_count": port_count if port_count is not None else "",
            "pos_pin": pos_pin,
            "neg_pin": neg_pin,
            "rlc_file": generated_models.get(comp_name, {}).get('rlcFile', ''),
            "model_provenance_file": generated_models.get(comp_name, {}).get('sourceFile', ''),
            "model_generation_status": generated_models.get(comp_name, {}).get('status', ''),
        })

    # 3. 타깃 캡 후보 선별
    # General capacitor: BOM에 명시 + 비접지 해석 net/GND 양단 연결
    # Inner capacitor: Step 4에서 만든 직렬화 가능한 2-pin component에
    # 실제 Maker-PN S2P를 우선 할당하고, 유효 모델이 없을 때만 명목 C를 유지한다.
    # 모델 파일/핀 수/주파수 범위 검사는 아래 품질 게이트에서 별도로 수행한다.
    all_components = getattr(app.edb._components, "components", {}) if getattr(app, "edb", None) else {}
    innercap_component_set = {str(item.get("component_name", "")).strip() for item in (inner_cap_audit or []) if str(item.get("component_name", "")).strip()}
    bom_designator_set = {
        str(item).strip()
        for item in bom_info.get("Designators", set())
        if str(item).strip()
    }

    target_component_names = set()
    target_reason_map = {}
    target_net_map = {}
    excluded_component_map = {}

    for comp_name, comp_inst in all_components.items():
        comp_name_str = str(comp_name)
        comp_upper = comp_name_str.upper()
        if not comp_upper.startswith("C"):
            continue

        reasons = set()
        is_innercap = comp_name_str in innercap_component_set
        is_bom_cap = comp_name_str in bom_designator_set

        # A C_IC-like EDB name alone is not authoritative.  Inner capacitors
        # must be present in the parsed InnerCap input; general capacitors must
        # be present in the BOM.  This prevents stale/optional EDB parts from
        # silently entering the solve model.
        if is_innercap:
            reasons.add("InnerCapRegistered")
        elif is_bom_cap:
            reasons.add("BOMRegistered")
        else:
            excluded_component_map[comp_name_str] = ["NotListedInBOMOrInnerCap"]
            continue

        try:
            pin_nets_upper = {
                str(pin_obj.net_name or "").strip().upper()
                for pin_obj in getattr(comp_inst, "pins", {}).values()
                if str(pin_obj.net_name or "").strip()
            }
        except Exception:
            pin_nets_upper = set()

        connected_target_nets = sorted(pin_nets_upper & target_power_net_set)
        if not connected_target_nets:
            excluded_component_map[comp_name_str] = ["NoNonGroundAnalysisNet"]
            continue
        reasons.add("AnalysisNetConnected")

        if gnd_net_upper and gnd_net_upper not in pin_nets_upper:
            excluded_component_map[comp_name_str] = ["NoReferenceNetPin"]
            continue
        if gnd_net_upper:
            reasons.add("ReferenceNetConnected")

        target_component_names.add(comp_name_str)
        target_reason_map[comp_name_str] = sorted(reasons)
        target_net_map[comp_name_str] = connected_target_nets

    # 입력 범위가 제공된 경우에는 선택 결과가 0개여도 fail-closed로 동작한다.
    # 이전의 bool(target_component_names) 조건은 선택 실패 시 전체 C를 처리했다.
    use_target_filter = bool(analysis_net_set) or bool(innercap_component_set) or bool(bom_designator_set)
    exclusion_summary = {}
    for exclusion_reasons in excluded_component_map.values():
        for exclusion_reason in exclusion_reasons:
            exclusion_summary[exclusion_reason] = exclusion_summary.get(exclusion_reason, 0) + 1

    if logger:
        logger.log(
            f"[SParam][TARGET] analysis_nets={len(analysis_net_set)}, "
            f"non_ground_analysis_nets={len(target_power_net_set)}, bom_designators={len(bom_designator_set)}, "
            f"inner_caps={len(innercap_component_set)}, selected_caps={len(target_component_names)}, "
            f"excluded_caps={len(excluded_component_map)}, use_target_filter={use_target_filter}",
            level=LogLevel.INFO,
        )
        logger.log(
            f"[SParam][TARGET] exclusion_summary={exclusion_summary}",
            level=LogLevel.INFO,
        )

    if output_dir:
        try:
            target_out = output_dir / "sparam_target_caps.json"
            target_payload = {
                "analysis_nets": sorted(analysis_net_set),
                "target_power_nets": sorted(target_power_net_set),
                "reference_net": str(gnd_net or ""),
                "innercap_components": sorted(innercap_component_set),
                "use_target_filter": use_target_filter,
                "selection_policy": {
                    "general_capacitor": "BOM registered AND connected to a non-ground analysis net AND reference net",
                    "inner_capacitor": "InnerCap registered AND connected to a non-ground analysis net AND reference net; valid non-placeholder S2P preferred",
                    "ground_only_is_not_target": True,
                    "unregistered_edb_capacitor_is_ignored": True,
                    "empty_selection_fails_closed": True,
                },
                "selected_count": len(target_component_names),
                "excluded_count": len(excluded_component_map),
                "exclusion_summary": exclusion_summary,
                "selected_components": [
                    {
                        "component": name,
                        "reasons": target_reason_map.get(name, []),
                        "connected_target_nets": target_net_map.get(name, []),
                    }
                    for name in sorted(target_component_names)
                ],
                "excluded_components": [
                    {"component": name, "reasons": excluded_component_map.get(name, [])}
                    for name in sorted(excluded_component_map)
                ],
            }
            with open(target_out, "w", encoding="utf-8") as f:
                json.dump(target_payload, f, indent=2, ensure_ascii=False)
            if logger:
                logger.log(f"[SParam][TARGET] Exported target list: {target_out}", level=LogLevel.DETAIL1)
        except Exception as e:
            if logger:
                logger.log(f"[WARNING] Failed to write sparam target list: {e}", level=LogLevel.WARNING)

    generated_models, generation_summary = {}, None
    generation_options = dict(model_generation or {})
    generation_enabled = generation_options.get('enabled', False)
    if generation_enabled:
        from core.model_generation import generate_pdn_models
        if output_dir is None:
            raise ValueError('S2P generation requires output_dir')
        extra_libraries = [Path(p) for p in generation_options.get('libraryDirs', [])]
        generated_models, generation_summary = generate_pdn_models(
            components={name: all_components[name] for name in target_component_names},
            bom_info=bom_info, inner_cap_audit=inner_cap_audit,
            output_dir=output_dir, library_dirs=([s2p_dir] if s2p_dir else []) + extra_libraries,
            options=generation_options, logger=logger,
        )

    # 4. EDB 프로젝트 내 부품 순회 및 할당

    for comp_name, comp_inst in all_components.items():
        comp_name_str = str(comp_name)

        # Capacitor 필터링
        if not comp_name_str.upper().startswith("C"):
            continue

        # 타깃 필터 적용 시 선정된 캡만 처리
        if use_target_filter and comp_name_str not in target_component_names:
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
            
        generated = generated_models.get(comp_name_str)
        if generated:
            maker_pn = generated['originalPn']
        if generation_enabled and not generated:
            append_record(comp_name_str, category, maker_pn, None, 'Skipped', 'Official model generation pending')
            continue
        if not maker_pn:
            append_record(comp_name_str, category, "", None, "Skipped", "No Maker PN found")
            continue
            
        if not s2p_dir_exists and not generated:
            append_record(comp_name_str, category, maker_pn, None, "Skipped", "S2P directory not found")
            continue

        model_maker_pn = maker_pn
        model_source = "ExactMakerPN"
        mapping_files = []
        mapping_chain = [maker_pn]
        if generated:
            s2p_path, rejected_placeholders = Path(generated['s2pFile']), []
            model_maker_pn = generated['selectedPn']
            model_source = 'PDNS2PCore'
            mapping_files = [generated['mappingFile']]
            mapping_chain = list(dict.fromkeys([maker_pn, model_maker_pn]))
        else:
            s2p_path, rejected_placeholders = _find_valid_model(maker_pn)
        if not s2p_path and not generation_enabled:
            s2p_path, mapped_pn, mapping_chain, mapping_files, mapped_rejected = _find_mapped_model(maker_pn)
            rejected_placeholders.extend(mapped_rejected)
            if s2p_path:
                model_maker_pn = mapped_pn
                model_source = "PmapModelMapping"
                if logger:
                    logger.log(f"[SParam][MODEL_MAPPING] {comp_name_str}: {' -> '.join(mapping_chain)}; source={mapping_files[-1]}", level=LogLevel.INFO)
        if not s2p_path and not generation_enabled:
            alias_pn = model_alias_map.get(str(maker_pn).strip().upper(), "")
            if alias_pn:
                s2p_path, alias_rejected = _find_valid_model(alias_pn)
                rejected_placeholders.extend(alias_rejected)
                if s2p_path:
                    model_maker_pn = alias_pn
                    model_source = "ConfiguredLibraryAlias"
                    if logger:
                        logger.log(
                            f"[SParam][ALIAS] {comp_name_str}: {maker_pn} -> {alias_pn}",
                            level=LogLevel.WARNING,
                        )
        if not s2p_path:
            reason = "No valid .s2p file"
            if rejected_placeholders:
                reason = f"Only all-zero placeholder .s2p found ({len(rejected_placeholders)})"
            append_record(comp_name_str, category, maker_pn, None, "Skipped", reason)
            if category == "InnerCap" and logger:
                logger.log(
                    f"[INNER_CAP][SParam][WARNING] {comp_name_str}: {reason}; nominal C model retained",
                    level=LogLevel.WARNING,
                )
            continue

        # 핀 개수 및 포트 확인
        port_count = detect_s2p_port_count(s2p_path)
        pins = list(comp_inst.pins.keys())
        pin_count = len(pins)

        # 품질 게이트 #1: 2-pin capacitor만 S-parameter 할당 허용 (핀/포트 불일치 방지)
        if pin_count != 2:
            append_record(comp_name_str, category, maker_pn, s2p_path, "Skipped", f"Unsupported pin count: {pin_count} (only 2-pin caps)")
            continue
        if port_count != 2:
            append_record(comp_name_str, category, maker_pn, s2p_path, "Skipped", f"Port/pin mismatch: pin_count=2, sNp_ports={port_count}")
            continue

        # 품질 게이트 #1.5: 해석 포함 net + 참조단자(GND) 성립 가능한 캡만 허용
        pin_net_by_name = {str(pin_name): str(pin_obj.net_name or "").strip() for pin_name, pin_obj in comp_inst.pins.items()}
        pin_nets_upper = {net.upper() for net in pin_net_by_name.values() if net}
        if analysis_net_set and not (pin_nets_upper & analysis_net_set):
            append_record(comp_name_str, category, maker_pn, s2p_path, "Skipped", "Not connected to selected analysis nets", False, port_count)
            continue
        if gnd_net_upper and gnd_net_upper not in pin_nets_upper:
            append_record(comp_name_str, category, maker_pn, s2p_path, "Skipped", f"No reference net pin: {gnd_net}", False, port_count)
            continue

        # 품질 게이트 #2: 주파수 상한 1GHz 커버리지 확인
        fmax_hz = detect_touchstone_freq_max_hz(s2p_path)
        if fmax_hz is None:
            append_record(comp_name_str, category, maker_pn, s2p_path, "Skipped", "Failed to read touchstone frequency range", False, port_count)
            continue
        if fmax_hz + 1.0 < min_required_hz:
            append_record(
                comp_name_str,
                category,
                maker_pn,
                s2p_path,
                "Skipped",
                f"Frequency coverage insufficient: fmax={fmax_hz:.3e}Hz < 1.000e+09Hz",
                False,
                port_count,
            )
            continue

        pos_pin = next((p for p, net in pin_net_by_name.items() if net.upper() in analysis_net_set and net.upper() != gnd_net_upper), "")
        if not pos_pin:
            pos_pin = next((p for p, net in pin_net_by_name.items() if net.upper() != gnd_net_upper), pins[0] if pins else "")
        neg_pin = next((p for p, net in pin_net_by_name.items() if net.upper() == gnd_net_upper), pins[1] if len(pins) > 1 else "")
        
        # S-Parameter 할당 실행
        try:
            ok = app.assign_sparameter_model(comp_name_str, str(s2p_path), port_count=port_count, pos_pin=pos_pin, neg_pin=neg_pin)
            if ok:
                assigned_reason = f"Assigned via {model_source}: {model_maker_pn}"
                append_record(comp_name_str, category, maker_pn, s2p_path, "Done", assigned_reason, True, port_count, str(pos_pin), str(neg_pin), model_maker_pn, model_source, mapping_files, mapping_chain)
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
        "S2P_Index_Count": sum(len(index[0]) for index in s2p_indexes),
        "S2P_Search_Directories": [str(path) for path in valid_s2p_dirs],
        "Target_Filter_Enabled": use_target_filter,
        "Target_Analysis_Net_Count": len(analysis_net_set),
        "Target_InnerCap_Count": len(innercap_component_set),
        "Target_Capacitor_Count": len(target_component_names),
        "Configured_Model_Aliases": model_alias_map,
        "Model_Mapping_Files": [str(path) for path in model_mapping_files],
        "Model_Mapping_Count": sum(len(items) for items in model_mapping_map.values()),
        "Model_Mapping_Diagnostics": model_mapping_diagnostics,
        "Model_Generation": generation_summary,
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
    if generation_enabled and generation_options.get('requireAllModels', True):
        failures = [r for r in records if not r['assigned']]
        if failures:
            raise RuntimeError(f'[S2P_GENERATION] Model assignment failed for {len(failures)} component(s); solve stopped. See {output_dir / "sparam_assignment_result.json"}')
    return report


# =========================================================================
# Stackup 및 Profile 유틸리티 함수
# =========================================================================
def resolve_zparam_profile(conf_data, stackup_file: Path | None, layer_count: int | None):
    zconf = conf_data.get("PDN", {}).get("zParamSetup", {})
    profiles = zconf.get("profiles", {})
    default_profile = zconf.get("defaultProfile", "2L")

    profile_key = default_profile
    if layer_count is not None and layer_count >= 4:
        profile_key = "4L_plus" if "4L_plus" in profiles else "4L"
    elif layer_count == 2:
        profile_key = "2L"
    elif stackup_file:
        name = str(stackup_file).upper()
        if "4L" in name or "4LAYER" in name:
            profile_key = "4L_plus" if "4L_plus" in profiles else "4L"
        elif "2L" in name:
            profile_key = "2L"

    if profile_key not in profiles and "4L" in profile_key:
        profile_key = "4L" if "4L" in profiles else default_profile

    selected = profiles.get(profile_key, {})
    sws_name = selected.get("sws", conf_data.get("PDN", {}).get("sws", "PDN_Fast.sws"))
    sfsdf_name = selected.get("sfsdf", "")
    # Production 4-Layer and higher boards use the centrally managed setup
    # assets.  Keep 2-Layer on its dedicated local PSI/HFSS configuration.
    if profile_key in {"4L", "4L_plus"}:
        setup_dir = str(zconf.get("fourLayerSetupDirectory", "") or "").strip()
        if setup_dir:
            setup_root = Path(setup_dir)
            sws_name = str(setup_root / Path(str(sws_name)).name)
            if sfsdf_name:
                sfsdf_name = str(setup_root / Path(str(sfsdf_name)).name)
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



