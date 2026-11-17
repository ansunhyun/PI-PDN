"""모델 처리에 필요한 설정만 포함한다. GUI와 고정 ANSYS 경로는 제외한다."""

import os

import re

from pathlib import Path



BASE_DIR = Path(__file__).resolve().parent

OUTPUT_DIR = Path(os.getenv('PDN_S2P_OUTPUT_DIR', str(Path.cwd() / 'pdn_s2p_output')))

APPROVED_MODEL_LIBRARY_DIR = OUTPUT_DIR / 'Approved_S2P_Library'

SPARAMETER_REGISTRY_PATH = OUTPUT_DIR / 'sparameter_registry.json'

ORIGINAL_SPECIFICATION_PATH = BASE_DIR / 'settings' / 'original-component-specifications.json'

MODEL_RUNTIME_MIRROR_DIRS = ()

MODEL_LIBRARY_SEARCH_DIRS = (APPROVED_MODEL_LIBRARY_DIR,)

MOCK_DOWNLOAD_DELAY_SEC = 0.2

DEFAULT_UNKNOWN_MAKER_TOKEN = "generic"

EXPECTED_SPARAMETER_PORTS = 2

EXPECTED_RLC_PINS = 2

MIN_SPARAMETER_COVERAGE_HZ = 1_000_000_000

MODEL_REPLACEMENT_SCAN_LIMIT = 200

ENABLE_VENDOR_DOWNLOAD = True

REFRESH_OFFICIAL_VENDOR_MODEL = False

ENABLE_SIMILAR_MODEL_REPLACEMENT = False

VENDOR_URL_TEMPLATES_JSON = os.getenv('PDN_S2P_VENDOR_URL_TEMPLATES_JSON', '')

VENDOR_DOWNLOAD_TIMEOUT_SEC = float(os.getenv('PDN_S2P_TIMEOUT_SEC', '20'))

DC_IMAG_TOLERANCE = 1e-9

REQUIRE_DC_POINT = True

REQUIRE_PASSIVITY_ENFORCEMENT = True

REQUIRE_CAUSALITY_FITTING = True

REQUIRE_RECIPROCITY_CHECK = False

SUPPORTED_SPARAMETER_DATA_FORMATS = ("ri", "ma", "db")

LOW_FREQ_REGION_MAX_HZ = 5_000_000

MIN_LOW_FREQ_POINT_COUNT = 2

ENABLE_LOW_FREQ_NOISE_CHECK = True

LOW_FREQ_MAX_MAG_DELTA = 0.8

ENABLE_LUMPED_FALLBACK = False

FALLBACK_CAPACITANCE_F = 1e-12

FALLBACK_ESR_OHM = 0.05

FALLBACK_ESL_H = 0.6e-9

FALLBACK_REFERENCE_IMPEDANCE_OHM = 50.0

RLC_CAPACITANCE_REL_TOLERANCE = 0.10

BOM_COMPONENT_INCLUDE_REGEX = r"(?i)\bcapacitor\b"

BOM_COMPONENT_EXCLUDE_REGEX = r"(?i)\b(resistor|inductor|ferrite|bead|coil)\b"

BOM_MAKER_PN_REGEX = r"\b[A-Za-z0-9][A-Za-z0-9._-]{5,}\b"

CAPACITOR_FALLBACK_KEYWORDS = (
    "capacitor",
    "cap",
    "mlcc",
    "ceramic",
    "electrolytic",
    "tantalum",
)

CAPACITANCE_UNIT_REGEX = r"(?i)\b\d+(?:\.\d+)?\s*(?:pf|nf|uf|mf)\b"

MAKER_PN_NOISE_TOKENS = {
    "OHM",
    "ROHS",
    "AECQ200",
    "AEC-Q200",
    "X5R",
    "X7R",
    "C0G",
    "NP0",
    "0402",
    "0603",
    "0805",
    "1206",
}

MIN_SPARAMETER_POINT_COUNT = 10

RESISTOR_MAKER_PREFIXES = ("MCR", "RK73", "RC", "ERJ", "CRG", "LR")

CAPACITOR_MAKER_PREFIXES = (
    "GRM",
    "GCM",
    "GQM",
    "LLA",
    "LLL",
    "LLM",
    "NFM",
    "AMK",
    "JMK",
    "LMK",
    "TMK",
    "UMK",
    "EMK",
    "GMK",
    "SMK",
    "AWK",
    "JWK",
    "EWK",
    "TWK",
    "CL",
    "C",
    "CS",
    "CQ",
    "CE",
    "CC",
)

MODEL_FILE_ENCODING = "utf-8"
