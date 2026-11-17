"""수동 RLC 회로의 핀 정규화와 Touchstone 응답 대조. GUI·제조사에 의존하지 않는다."""
from __future__ import annotations

import cmath
import math
import re
from pathlib import Path


def _netlist_text(payload: bytes) -> str:
    try:
        return payload.decode('utf-8-sig')
    except UnicodeDecodeError:
        # 삼성 공식 파일의 CP949 주석을 보존한다. 소자 수치와 회로 구조는 바꾸지 않는다.
        return payload.decode('cp949')


def parse_rlc_netlist(payload: bytes):
    elements = []
    pins = None
    name = None
    ended = False
    for raw in _netlist_text(payload).splitlines():
        line = raw.strip()
        if not line or line.startswith("*"):
            continue
        tokens = line.split()
        if tokens[0].lower() == ".subckt":
            if pins is not None or len(tokens) != 4:
                raise ValueError("rlc_requires_one_two_terminal_subcircuit")
            name, pins = tokens[1], tokens[2:]
        elif tokens[0].lower() == ".ends":
            if pins is None or ended or len(tokens) > 2 or (len(tokens) == 2 and tokens[1] != name):
                raise ValueError("rlc_subcircuit_end_mismatch")
            ended = True
        else:
            if (len(tokens) == 5 and tokens[0][0].upper() == 'L'
                    and re.fullmatch(r'Rser=0(?:\.0*)?', tokens[4], re.I)):
                # 공식 LTspice의 명시적 0Ω 직렬 저항은 이상 인덕터와 동일하다.
                tokens = tokens[:4]
            if pins is None or ended or len(tokens) != 4 or tokens[0][0].upper() not in "RLC":
                raise ValueError("unsupported_rlc_statement")
            match = re.fullmatch(r"([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)(meg|[fpnumkg]?)", tokens[3], re.I)
            if not match:
                raise ValueError("unsupported_rlc_value")
            scale = {"": 1, "f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "meg": 1e6, "g": 1e9}
            value = float(match[1]) * scale[match[2].lower()]
            if not math.isfinite(value) or value <= 0 or tokens[1] == tokens[2] or "0" in tokens[1:3]:
                raise ValueError("invalid_passive_rlc_element")
            elements.append((tokens[0], tokens[1], tokens[2], value, tokens[3]))
    if not ended or not elements or pins[0] == pins[1]:
        raise ValueError("incomplete_rlc_circuit")
    if len({e[0].lower() for e in elements}) != len(elements):
        raise ValueError("duplicate_rlc_element")
    return name, pins, elements


def normalize_rlc_netlist(payload: bytes) -> bytes:
    _, pins, elements = parse_rlc_netlist(payload)
    nodes = {pins[0]: "1", pins[1]: "2"}
    for _, a, b, _, _ in elements:
        for node in (a, b):
            if node not in nodes:
                nodes[node] = f"n_{len(nodes)}"
    comments = [line for line in _netlist_text(payload).splitlines() if line.lstrip().startswith("*")]
    lines = ["* Official vendor RLC netlist; subcircuit/pin names normalized by Pmap Generator.", *comments, ".SUBCKT X 1 2"]
    lines.extend(f"{name} {nodes[a]} {nodes[b]} {token}" for name, a, b, _, token in elements)
    lines.append(".ENDS X")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _impedance(pins, elements, frequency):
    nodes = sorted({n for e in elements for n in e[1:3]} - {pins[1]})
    if pins[0] not in nodes:
        raise ValueError("rlc_terminal_disconnected")
    index = {node: i for i, node in enumerate(nodes)}
    size = len(nodes)
    matrix = [[0j] * (size + 1) for _ in nodes]
    matrix[index[pins[0]]][-1] = 1
    omega = 2 * math.pi * frequency
    for name, a, b, value, _ in elements:
        admittance = {"R": lambda: 1 / value, "L": lambda: 1 / (1j * omega * value), "C": lambda: 1j * omega * value}[name[0].upper()]()
        for node, other in ((a, b), (b, a)):
            if node in index:
                matrix[index[node]][index[node]] += admittance
                if other in index:
                    matrix[index[node]][index[other]] -= admittance
    for col in range(size):
        pivot = max(range(col, size), key=lambda row: abs(matrix[row][col]))
        if abs(matrix[pivot][col]) == 0:
            raise ValueError("rlc_terminal_disconnected")
        matrix[col], matrix[pivot] = matrix[pivot], matrix[col]
        divisor = matrix[col][col]
        for row in range(col + 1, size):
            factor = matrix[row][col] / divisor
            for j in range(col + 1, size + 1):
                matrix[row][j] -= factor * matrix[col][j]
    solution = [0j] * size
    for row in range(size - 1, -1, -1):
        solution[row] = (matrix[row][-1] - sum(matrix[row][j] * solution[j] for j in range(row + 1, size))) / matrix[row][row]
    return solution[index[pins[0]]]


def compare_rlc_to_s2p(rlc_payload: bytes, s2p_payload: bytes, max_error: float = 0.01) -> dict:
    """직렬 연결의 복소 S값 네 개를 모든 양수 주파수점에서 대조한다."""
    _, pins, elements = parse_rlc_netlist(rlc_payload)
    units, fmt, z0 = "HZ", "RI", 50.0
    points = []
    for raw in s2p_payload.splitlines():
        # Touchstone 숫자와 옵션은 ASCII, 제조사 주석은 별도 문자 인코딩일 수 있다.
        line = raw.split(b"!", 1)[0].decode('ascii', errors='strict').strip()
        if not line:
            continue
        if line.startswith("#"):
            option = line[1:].upper().split()
            if len(option) != 5 or option[1] != "S" or option[3] != "R":
                raise ValueError("unsupported_touchstone_option")
            units, fmt, z0 = option[0], option[2], float(option[4])
        else:
            values = [float(token) for token in line.split()]
            if len(values) != 9:
                raise ValueError("touchstone_line_shape_invalid")
            frequency = values[0] * {"HZ": 1, "KHZ": 1e3, "MHZ": 1e6, "GHZ": 1e9}[units]
            if frequency <= 0:
                continue
            if fmt == "RI":
                s = [complex(values[i], values[i + 1]) for i in range(1, 9, 2)]
            elif fmt in {"MA", "DB"}:
                s = [cmath.rect(values[i] if fmt == "MA" else 10 ** (values[i] / 20), math.radians(values[i + 1])) for i in range(1, 9, 2)]
            else:
                raise ValueError("unsupported_touchstone_format")
            points.append((frequency, s))
    if not points or not math.isfinite(z0) or z0 <= 0:
        raise ValueError("invalid_touchstone_reference_or_points")
    errors = []
    for frequency, s in points:
        z = _impedance(pins, elements, frequency)
        s11, s21 = z / (z + 2 * z0), 2 * z0 / (z + 2 * z0)
        errors.extend(abs(actual - predicted) for actual, predicted in zip(s, [s11, s21, s21, s11]))
    error = max(errors)
    if not math.isfinite(error) or error > max_error:
        raise ValueError(f"rlc_s2p_response_mismatch(maxComplexSError={error:.6g},limit={max_error})")
    return {"method": "passive_two_terminal_nodal_analysis_vs_series_s2p", "points": len(points),
            "frequencyRangeHz": [min(p[0] for p in points), max(p[0] for p in points)],
            "maxComplexSError": error, "maxComplexSErrorLimit": max_error}


def rlc_impedance_samples(payload: bytes, frequencies) -> list[complex]:
    """검증된 수동 2단자 회로의 주파수별 임피던스. 전체 PCB PDN 해석과 구분한다."""
    _, pins, elements = parse_rlc_netlist(payload)
    if any(not math.isfinite(f) or f <= 0 for f in frequencies):
        raise ValueError("positive_finite_frequencies_required")
    return [_impedance(pins, elements, f) for f in frequencies]


def validate_rlc_sidecar(s2p_path: Path, rlc_path: Path) -> dict | None:
    """출처 및 두 파일의 SHA가 연결된 공식 netlist에 한해서 회로를 다시 대조한다."""
    import hashlib
    import json
    source = rlc_path.with_suffix(".rlc.source.json")
    if not source.exists():
        return None
    data = json.loads(source.read_text(encoding="utf-8"))
    if data.get("sourceType") not in {"official_vendor_netlist_normalized", "official_s2p_derived_rlc"}:
        return None
    if (data.get("partNumber") != s2p_path.stem or data.get("sha256") != hashlib.sha256(rlc_path.read_bytes()).hexdigest()
            or data.get("s2pSha256") != hashlib.sha256(s2p_path.read_bytes()).hexdigest()):
        raise ValueError("rlc_source_hash_or_pn_mismatch")
    original = s2p_path.with_suffix(".mod")
    s2p_source = json.loads(s2p_path.with_suffix(".source.json").read_text(encoding="utf-8"))
    if data.get('sourceType') == 'official_s2p_derived_rlc':
        from .approximate_rlc import validate_derived_rlc
        if (s2p_source.get('sourceType') not in {'official_vendor_download', 'official_vendor_manual_import'}
                or data.get('modelPartNumber') != s2p_source.get('modelPartNumber')
                or data.get('modelConditions') != s2p_source.get('modelConditions')
                or data.get('approximation', {}).get('sourceS2pSha256') != data.get('s2pSha256')
                or data.get('originalFile') is not None or data.get('officialVendorNetlist') is not False):
            raise ValueError('derived_rlc_source_identity_mismatch')
        return validate_derived_rlc(rlc_path.read_bytes(), s2p_path.read_bytes())
    if (data.get("originalFile") != original.name or not original.exists()
            or data.get("originalSha256") != hashlib.sha256(original.read_bytes()).hexdigest()
            or normalize_rlc_netlist(original.read_bytes()) != rlc_path.read_bytes()
            or data.get("modelPartNumber") != s2p_source.get("modelPartNumber")
            or data.get("modelConditions") != s2p_source.get("modelConditions")):
        raise ValueError("rlc_original_or_pair_identity_mismatch")
    return compare_rlc_to_s2p(rlc_path.read_bytes(), s2p_path.read_bytes())
