import re
from pdn_s2p_core.pdn_pre_s2p import extract_capacitance_f

from pdn_s2p_core.config import (
    BOM_COMPONENT_EXCLUDE_REGEX,
    BOM_COMPONENT_INCLUDE_REGEX,
    BOM_MAKER_PN_REGEX,
    CAPACITANCE_UNIT_REGEX,
    CAPACITOR_FALLBACK_KEYWORDS,
    CAPACITOR_MAKER_PREFIXES,
    MAKER_PN_NOISE_TOKENS,
    RESISTOR_MAKER_PREFIXES,
)

# 공식 Walsin 검색 행에 연결된 ECD 본문에서 전체 주문 PN을 확인한 제품만 등록한다.
# /wp-content/uploads/ecd/0603B104K500CT-IX.pdf 및 0402B102K500CT-IN.pdf.
# 기본 PN/접미사 일치 추정으로 확대하지 않는다. 0402N470J500LT는 미확인이다.
WALSIN_VERIFIED_ORDERING_PNS = frozenset({"0603B104K500CT", "0402B102K500CT"})


def normalize_maker_pn(token: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "", str(token).strip()).upper()


def is_valid_maker_pn(pn: str) -> bool:
    pn = normalize_maker_pn(pn)
    if len(pn) < 6:
        return False
    if not re.match(r"^[A-Z]", pn):
        return False
    if not re.search(r"\d", pn):
        return False
    if pn in MAKER_PN_NOISE_TOKENS:
        return False
    if pn.startswith("C") and re.fullmatch(r"C\d{4}", pn):
        return False
    return True


def get_component_info(maker_pn: str):
    if maker_pn.upper() in WALSIN_VERIFIED_ORDERING_PNS:
        return "Walsin", maker_pn[:4]
    match = re.match(r"^([A-Z]+)(\d+)", maker_pn.upper())
    if not match:
        return "Unknown", "Unknown"

    prefix = match.group(1)
    numbers = match.group(2)
    if prefix in ["GRM", "GCM", "GQM", "LLA", "LLL", "LLM", "NFM"]:
        return "Murata", prefix + numbers[:2]
    if prefix in ["AMK", "JMK", "LMK", "TMK", "UMK", "EMK", "GMK", "SMK", "AWK", "JWK", "EWK", "TWK"]:
        return "TaiyoYuden", prefix + numbers[:3]
    if prefix == "CL":
        return "Samsung", prefix + numbers[:2]
    if prefix == "C" and len(numbers) >= 4:
        return "TDK", prefix + numbers[:4]
    if prefix in ["CS", "CQ", "CE"]:
        return "Samwha", prefix + numbers[:2]
    if prefix == "CC":
        return "Yageo", prefix + numbers[:4]
    return f"Maker_{prefix}", (prefix + numbers[:2] if len(numbers) >= 2 else prefix + numbers)


def get_maker_abbr(maker: str) -> str:
    abbr_map = {
        "Murata": "M",
        "TaiyoYuden": "TY",
        "Samsung": "S",
        "TDK": "T",
        "Samwha": "SW",
        "Yageo": "Y",
        "Walsin": "W",
    }
    return abbr_map.get(maker, maker[0] if maker else "U")


def extract_capacitance(maker_pn: str) -> str:
    value = extract_capacitance_f(maker_pn)
    return "Unknown" if value is None else f"{value / 1e-6:.10g}uF"


def manufacturer_from_text(text: str) -> str:
    normalized = re.sub(r"[^A-Z0-9]", "", str(text).upper())
    for token, maker in (("MURATA", "Murata"), ("TAIYOYUDEN", "TaiyoYuden"),
                         ("SAMSUNG", "Samsung"), ("SAMWHA", "Samwha"), ("SAMYOUNG", "Samyoung"),
                         ("TDK", "TDK"), ("PILKOR", "Pilkor"), ("DARFON", "Darfon"),
                         ("COWELL", "Cowell"), ("FENGHUA", "Fenghua"), ("YAGEO", "Yageo"),
                         ("WALSIN", "Walsin")):
        if token in normalized:
            return maker
    return ""


def identify_bom_part(raw_spec: str, raw_manufacturer: str) -> dict:
    """BOM 내부 일관성을 확인한다. 공식 모델 제공/전체 PN의 외부 검증과는 별개다."""
    spec = str(raw_spec).strip()
    raw_maker = str(raw_manufacturer).strip()
    if raw_maker.lower() in {"nan", "none"}:
        raw_maker = ""
    maker = manufacturer_from_text(raw_maker)
    spec_maker = manufacturer_from_text(spec)
    manufacturer_match = re.search(r"\b(?:MURATA|TAIYO\s*YUDEN|SAMSUNG|SAMWHA|SAMYOUNG|TDK|PILKOR|DARFON|COWELL|(?:GUANG\s+DONG\s+)?FENG\s*HUA|YAGEO|WALSIN)\b", spec, re.I)
    spec_manufacturer_raw = spec[manufacturer_match.start():].strip() if manufacturer_match else ""
    pn = extract_best_maker_pn(spec)
    if maker in {"Pilkor", "Walsin"} or spec_maker in {"Pilkor", "Walsin"}:
        numeric_pn = re.search(r"\b(?:0402|0603)[BN]\d{3}[A-Z]\d{3}[A-Z]{2}\b", spec, re.I)
        if numeric_pn:
            pn = numeric_pn[0].upper()
    inferred, _ = get_component_info(pn)
    issues = []
    notes = []
    if maker and spec_maker and maker != spec_maker:
        notes.append(f"manufacturer_column_spec_conflict({maker}/{spec_maker}); technical_spec_preferred")
    # C1005 같은 범용 크기 접두어는 TDK/Darfon을 구분하지 못한다.
    pn_maker = inferred if inferred not in {"Unknown", "TDK"} and not inferred.startswith("Maker_") else ""
    selected_maker = pn_maker or spec_maker or maker
    basis = "pn_rule" if pn_maker else "technical_spec" if spec_maker else "maker_column" if maker else "unresolved"
    if pn in WALSIN_VERIFIED_ORDERING_PNS:
        basis = "official_exact_pn_document"
        notes.append(f"official_manufacturer_identity_verified(Walsin/{pn}); official_ECD; S2P_availability_separate")
    if pn_maker and (spec_maker or maker) and pn_maker != (spec_maker or maker):
        notes.append(f"manufacturer_pn_text_conflict({pn_maker}/{spec_maker or maker}); pn_rule_preferred; official_exact_validation_required")
    if not selected_maker:
        issues.append("manufacturer_not_explicit")
        selected_maker = inferred
    raw_pn = next((token for token in spec.split() if pn and token.upper().startswith(pn)), pn)
    suggested = ""
    split = re.search(r"\b((?:AMK|JMK|LMK|TMK|UMK|EMK|GMK|SMK)\d{3})\s+([A-Z0-9]+-[A-Z])\b", spec, re.I)
    if split:
        raw_pn = split[0]
        suggested = split[1].upper() + split[2].upper()
        issues.append("split_pn_confirmation_required")
    elif raw_pn.upper() != pn:
        issues.append("pn_annotation_confirmation_required")
    if pn.startswith("GRM") and len(pn) < 17:
        issues.append("incomplete_grm_pn_full_ordering_code_required")
    if not pn:
        issues.append("part_number_missing")
    cap = re.search(r"(?i)(\d+(?:\.\d+)?)\s*(pF|nF|uF|µF|μF|mF)\b", spec)
    capacitance = float(cap[1]) * {"pf": 1e-12, "nf": 1e-9, "uf": 1e-6, "µf": 1e-6, "μf": 1e-6, "mf": 1e-3}[cap[2].lower()] if cap else None
    # 용량부터 제조사 앞까지의 특성을 그대로 보존하고 구분용 '-'만 제거한다.
    characteristics = spec[cap.start():manufacturer_match.start() if manufacturer_match else len(spec)].strip().rstrip(" -") if cap else ""
    return {"maker_pn": pn, "manufacturer": selected_maker, "raw_manufacturer": raw_maker,
            "spec_manufacturer": spec_maker, "raw_maker_pn": raw_pn, "suggested_maker_pn": suggested,
            "source_spec": spec, "nominal_capacitance_f": capacitance,
            "identity_status": "needs_confirmation" if issues else "bom_consistent",
            "identity_detail": "; ".join(issues), "identity_notes": "; ".join(notes),
            "spec_manufacturer_raw": spec_manufacturer_raw, "product_characteristics": characteristics,
            "manufacturer_basis": basis}


def get_prefix_and_num(soc_name: str):
    match = re.match(r"^([A-Za-z]+)(\d*)", soc_name)
    if match:
        prefix = match.group(1)
        num_str = match.group(2)
        num = int(num_str) if num_str else 0
        return prefix, num
    return soc_name, 0


def extract_best_maker_pn(raw_spec: str) -> str:
    for pn in WALSIN_VERIFIED_ORDERING_PNS:
        if re.search(r"(?<![A-Za-z0-9])" + re.escape(pn) + r"(?![A-Za-z0-9])", str(raw_spec), re.I):
            return pn
    candidates = []
    for match in re.findall(BOM_MAKER_PN_REGEX, str(raw_spec)):
        token = normalize_maker_pn(match)
        if not is_valid_maker_pn(token):
            continue
        upper = token.upper()
        if upper.startswith(RESISTOR_MAKER_PREFIXES):
            continue
        score = 0
        if upper.startswith(CAPACITOR_MAKER_PREFIXES):
            score += 100
        if re.search(r"\d", upper):
            score += 10
        score += min(len(upper), 40)
        candidates.append((score, token))

    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def is_likely_capacitor_row(desc_text: str, spec_text: str, maker_pn: str) -> bool:
    full_text = f"{desc_text} {spec_text}".lower()
    has_cap_keyword = bool(re.search(BOM_COMPONENT_INCLUDE_REGEX, desc_text, flags=0))
    has_fallback_keyword = any(k in full_text for k in CAPACITOR_FALLBACK_KEYWORDS)
    has_cap_unit = bool(re.search(CAPACITANCE_UNIT_REGEX, full_text))
    has_exclude_keyword = bool(re.search(BOM_COMPONENT_EXCLUDE_REGEX, full_text))
    has_cap_prefix = maker_pn.upper().startswith(CAPACITOR_MAKER_PREFIXES) if maker_pn else False
    has_res_prefix = maker_pn.upper().startswith(RESISTOR_MAKER_PREFIXES) if maker_pn else False

    if has_exclude_keyword or has_res_prefix:
        return False
    if has_cap_keyword:
        return True
    if has_cap_prefix and (has_fallback_keyword or has_cap_unit):
        return True
    return has_fallback_keyword and has_cap_unit
