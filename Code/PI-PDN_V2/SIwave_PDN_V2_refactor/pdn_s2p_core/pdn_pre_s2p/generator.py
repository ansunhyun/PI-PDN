"""PDN Pre 단계에서 사용할 수 있는 독립적인 S2P/RLC 생성기.

표준 라이브러리만 사용하며 GUI, Pmap, pandas에 의존하지 않는다.
실측/vendor 모델이 없을 때 사용할 직렬 R-L-C 추정 모델을 생성한다.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from pathlib import Path


DEFAULT_CAPACITANCE_F = 1e-12
DEFAULT_ESR_OHM = 0.05
DEFAULT_ESL_H = 0.6e-9
DEFAULT_REFERENCE_IMPEDANCE_OHM = 50.0
DEFAULT_FREQUENCIES_HZ = (
    0,
    1,
    10,
    100,
    1_000,
    10_000,
    100_000,
    1_000_000,
    5_000_000,
    10_000_000,
    50_000_000,
    100_000_000,
    250_000_000,
    500_000_000,
    1_000_000_000,
    1_500_000_000,
    2_000_000_000,
)


@dataclass(frozen=True)
class SeriesRlcModel:
    part_number: str
    capacitance_f: float
    esr_ohm: float
    esl_h: float
    package: str
    reference_impedance_ohm: float = DEFAULT_REFERENCE_IMPEDANCE_OHM


@dataclass(frozen=True)
class GeneratedModel:
    model: SeriesRlcModel
    folder: Path
    s2p_path: Path
    rlc_path: Path


def normalize_part_number(part_number: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]", "", str(part_number).strip()).upper()
    if not normalized:
        raise ValueError("part_number is empty after normalization")
    return normalized


def extract_capacitance_f(part_number: str) -> float | None:
    """일반적인 MLCC 3자리 capacitance code를 Farad로 변환한다."""
    pn = normalize_part_number(part_number)
    if pn.startswith("BXJ"):
        return None  # 전해 커패시터의 명시 용량을 MLCC 코드로 해석하지 않는다.
    if pn.startswith("GRM") and len(pn) >= 14:
        code = pn[10:13]
        if "R" in code and re.fullmatch(r"[0-9R]{3}", code) and code.count("R") == 1:
            return float(code.replace("R", ".")) * 1e-12
        if not code.isdigit():
            return None
        return int(code[:2]) * 10 ** int(code[2]) * 1e-12 if int(code[2]) <= 6 else None
    matches = re.findall(r"(\d{3})[BCDFGJKMZ]", pn)
    if not matches:
        return None
    code = matches[-1]
    base = int(code[:2])
    multiplier = int(code[2])
    if multiplier > 6:
        return None
    return base * (10**multiplier) * 1e-12


def infer_capacitor_package(part_number: str) -> str:
    """Maker PN에서 imperial package 명칭을 추정한다."""
    pn = normalize_part_number(part_number)
    patterns = (
        (r"^(?:GRM|GCM|GQM|LL[ALM])033", "0201"),
        (r"^(?:GRM|GCM|GQM|LL[ALM])155", "0402"),
        (r"^(?:GRM|GCM|GQM|LL[ALM])188", "0603"),
        (r"^(?:GRM|GCM|GQM|LL[ALM])21", "0805"),
        (r"^(?:GRM|GCM|GQM|LL[ALM])31", "1206"),
        (r"^CL03", "0201"),
        (r"^CL05", "0402"),
        (r"^CL10", "0603"),
        (r"^CL21", "0805"),
        (r"^CL31", "1206"),
        (r"^C0603", "0201"),
        (r"^C1005", "0402"),
        (r"^C1608", "0603"),
        (r"^C2012", "0805"),
        (r"^C3216", "1206"),
        (r"^(?:AMK|JMK|LMK|TMK|UMK|EMK|GMK|SMK|AWK|JWK|EWK|TWK)063", "0201"),
        (r"^(?:AMK|JMK|LMK|TMK|UMK|EMK|GMK|SMK|AWK|JWK|EWK|TWK)105", "0402"),
        (r"^(?:AMK|JMK|LMK|TMK|UMK|EMK|GMK|SMK|AWK|JWK|EWK|TWK)107", "0603"),
        (r"^(?:AMK|JMK|LMK|TMK|UMK|EMK|GMK|SMK|AWK|JWK|EWK|TWK)212", "0805"),
        (r"^(?:AMK|JMK|LMK|TMK|UMK|EMK|GMK|SMK|AWK|JWK|EWK|TWK)316", "1206"),
        (r"^CC0201", "0201"),
        (r"^CC0402", "0402"),
        (r"^CC0603", "0603"),
        (r"^CC0805", "0805"),
        (r"^CC1206", "1206"),
    )
    for pattern, package in patterns:
        if re.search(pattern, pn):
            return package
    return "0603"


def estimate_series_rlc(
    part_number: str,
    *,
    capacitance_f: float | None = None,
    package: str | None = None,
    default_capacitance_f: float = DEFAULT_CAPACITANCE_F,
    default_esr_ohm: float = DEFAULT_ESR_OHM,
    default_esl_h: float = DEFAULT_ESL_H,
    reference_impedance_ohm: float = DEFAULT_REFERENCE_IMPEDANCE_OHM,
) -> SeriesRlcModel:
    """Part number 또는 명시 입력으로 양수 직렬 R-L-C 값을 산정한다."""
    pn = normalize_part_number(part_number)
    cap_f = capacitance_f if capacitance_f is not None else extract_capacitance_f(pn)
    cap_f = max(float(cap_f if cap_f is not None else default_capacitance_f), 1e-15)
    package_name = package or infer_capacitor_package(pn)

    if cap_f >= 10e-6:
        base_esr = 0.003
    elif cap_f >= 1e-6:
        base_esr = 0.008
    elif cap_f >= 100e-9:
        base_esr = 0.015
    elif cap_f >= 10e-9:
        base_esr = 0.030
    elif cap_f >= 1e-9:
        base_esr = 0.080
    else:
        base_esr = default_esr_ohm

    esr_factor = {"0201": 1.6, "0402": 1.3, "0603": 1.0, "0805": 0.85, "1206": 0.7}
    esl_by_package = {
        "0201": 0.20e-9,
        "0402": 0.40e-9,
        "0603": 0.60e-9,
        "0805": 0.80e-9,
        "1206": 1.00e-9,
    }
    return SeriesRlcModel(
        part_number=pn,
        capacitance_f=cap_f,
        esr_ohm=max(base_esr * esr_factor.get(package_name, 1.0), 1e-6),
        esl_h=max(esl_by_package.get(package_name, default_esl_h), 1e-12),
        package=package_name,
        reference_impedance_ohm=float(reference_impedance_ohm),
    )


def write_model_files(
    s2p_path: str | Path,
    rlc_path: str | Path,
    model: SeriesRlcModel,
    *,
    frequencies_hz: tuple[float, ...] = DEFAULT_FREQUENCIES_HZ,
) -> None:
    """동일한 직렬 R-L-C 값으로 Touchstone S2P와 SPICE RLC를 생성한다."""
    s2p = Path(s2p_path)
    rlc = Path(rlc_path)
    s2p.parent.mkdir(parents=True, exist_ok=True)
    rlc.parent.mkdir(parents=True, exist_ok=True)

    with open(s2p, "w", encoding="utf-8") as f:
        f.write("! passivity on\n! causality on\n! reciprocity on\n")
        f.write(
            f"! estimated series RLC: C={model.capacitance_f:.6e} F, "
            f"ESR={model.esr_ohm:.6e} ohm, ESL={model.esl_h:.6e} H, package={model.package}\n"
        )
        f.write(f"# Hz S RI R {model.reference_impedance_ohm:g}\n")
        for hz in frequencies_hz:
            if hz < 0:
                raise ValueError("frequency must be non-negative")
            if hz == 0:
                s11, s21 = complex(1.0, 0.0), complex(0.0, 0.0)
            else:
                omega = 2.0 * math.pi * hz
                impedance = complex(
                    model.esr_ohm,
                    (omega * model.esl_h) - (1.0 / (omega * model.capacitance_f)),
                )
                denominator = impedance + (2.0 * model.reference_impedance_ohm)
                s11 = impedance / denominator
                s21 = (2.0 * model.reference_impedance_ohm) / denominator
            f.write(
                f"{hz:g} {s11.real:.9e} {s11.imag:.9e} "
                f"{s21.real:.9e} {s21.imag:.9e} "
                f"{s21.real:.9e} {s21.imag:.9e} "
                f"{s11.real:.9e} {s11.imag:.9e}\n"
            )

    with open(rlc, "w", encoding="utf-8") as f:
        f.write("* Estimated series RLC fallback; replace with vendor/measurement data when available.\n")
        f.write(f"* Part={model.part_number} Package={model.package}\n")
        f.write(".SUBCKT X 1 2\n")
        f.write(f"R_ESR 1 n_esr {model.esr_ohm:.6e}\n")
        f.write(f"L_ESL n_esr n_cap {model.esl_h:.6e}\n")
        f.write(f"C_MAIN n_cap 2 {model.capacitance_f:.6e}\n")
        f.write(".ENDS\n")


def write_model_pair(
    output_root: str | Path,
    part_number: str,
    *,
    capacitance_f: float | None = None,
    package: str | None = None,
) -> GeneratedModel:
    """`output_root/<PN>/<PN>.s2p/.rlc` 구조로 모델 한 쌍을 생성한다."""
    model = estimate_series_rlc(part_number, capacitance_f=capacitance_f, package=package)
    folder = Path(output_root) / model.part_number
    s2p_path = folder / f"{model.part_number}.s2p"
    rlc_path = folder / f"{model.part_number}.rlc"
    write_model_files(s2p_path, rlc_path, model)
    return GeneratedModel(model=model, folder=folder, s2p_path=s2p_path, rlc_path=rlc_path)
