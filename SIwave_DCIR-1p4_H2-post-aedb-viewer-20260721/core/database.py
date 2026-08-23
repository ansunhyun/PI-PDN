# coding=utf-8
# © <2025> ANSYS, Inc. Unauthorized use, distribution, or duplication is prohibited
import json
import re
from enum import Enum
from pathlib import Path
from dataclasses import dataclass, asdict
from typing import List, Dict, Any, Optional

from core.logger import LogLevel

# ==========================================
# 1. Data Models (전처리 코드에서 이관)
# ==========================================
@dataclass
class DCIRCase:
    """DCIR 시뮬레이션 케이스 데이터를 구조화하는 클래스"""
    case_index: int
    ic_designator: str
    target_net: str
    source_component: str
    net_chain: List[str]
    voltage_v: float
    current_a: float
    min_spec_v: float
    max_spec_v: float
    project_path: str
    v_port_name: str
    i_port_name: str


# ==========================================
# 2. Utility Functions (전처리 코드에서 이관)
# ==========================================
def extract_voltage(net_name: str) -> Optional[float]:
    """Net 이름에서 전압 값을 추출합니다."""
    match = re.search(r'(\d+\.\d+)V', net_name, re.IGNORECASE)
    if match: return float(match.group(1))
    
    match_alt = re.search(r'(\d+)V(\d+)', net_name, re.IGNORECASE)
    if match_alt: return float(f"{match_alt.group(1)}.{match_alt.group(2)}")
    
    match_vdd = re.search(r'VDD(\d)(\d)', net_name, re.IGNORECASE)
    if match_vdd: return float(f"{match_vdd.group(1)}.{match_vdd.group(2)}")
    
    return None

def sanitize_str(s: Any, extra: tuple = (' ', '.', '_', '-', '+')) -> str:
    """문자열에서 특수문자를 제거하고 안전한 이름으로 변환합니다."""
    return "".join(c for c in str(s or "") if c.isalnum() or c in extra).strip()


# ==========================================
# 3. Database State Manager (확장)
# ==========================================
class Database:
    """프로젝트의 상태와 파싱된 데이터를 관리하는 중앙 컨테이너 클래스"""

    def __init__(self, input_dir: Path = None):
        """Initialize the database application."""
        self._version = "1.4"
        self.input_dir = Path(input_dir) if input_dir else Path(".")
        self.output_dir = self.input_dir / 'outputs'
        
        # 전처리 코드의 전역 변수들을 Database 속성으로 이관
        self.bom_info: Dict[str, Any] = {}
        self.spec_info: List[Dict[str, Any]] = []
        self.dcir_cases: List[DCIRCase] = []
        self.other_nets: Dict[str, List[str]] = {}

    def add_dcir_case(self, case: DCIRCase):
        """DCIR 케이스를 추가합니다."""
        self.dcir_cases.append(case)

    def export_preprocessing_result(self):
        """전처리 결과를 JSON 파일로 출력합니다."""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        result_file = self.output_dir / 'preprocessing_result.json'
        
        # DataClass 리스트를 JSON 직렬화 가능한 딕셔너리로 변환
        data_to_dump = [asdict(case) for case in self.dcir_cases]
        
        with open(result_file, 'w', encoding='utf-8') as f:
            json.dump(data_to_dump, f, indent=4, ensure_ascii=False)
        return result_file


# ==========================================
# 4. Error Codes & Exceptions (LocalErrorCode 병합)
# ==========================================
class ErrorCode(Enum):
    """Provides EDB exception types."""

    UNKNOWN = "Unknown exception: {}."

    # Validation Check for Input JSON file
    INPUT_FILE_NOT_FOUND = "Input file not found: {}."

    # for CAD file Converting
    INVALID_CAD_FILE = "Invalid CAD file: {}."
    INVALID_PCB_FILE_NUM = 'More than one PCB files are detected : {}.'
    CR5_EXECUTABLE_NOT_FOUND = 'CR5 executable not found: {}'
    CONVERT_PCB_TO_DSGN_FAIL = 'Fail to convert *.pcb file to *.dsgn file: {}'
    DSGN2ANF_EXECUTABLE_NOT_FOUND = 'DSGN2ANF executable not found: {}'
    CONVERT_DSGN_TO_ANF_FAIL = 'Fail to convert *.dsgn to *.anf file: {}'
    DSGN2ODB_EXECUTABLE_NOT_FOUND = 'DSGN2ODB executable not found: {}'
    CONVERT_DSGN_TO_ODB_FAIL = 'Fail to convert *.dsgn to *.tgz file: {}'
    DSGN2EDB_EXECUTABLE_NOT_FOUND = 'DSGN2EDB executable not found: {}'
    CONVERT_DSGN_TO_EDB_FAIL = 'Fail to convert *.dsgn to *.aedb file: {}'
    INVALID_ANF_FILE_NUM = 'More than one ANF files are detected : {}.'
    INVALID_CMP_FILE_NUM = 'More than one CMP files are detected : {}.'

    # 3. Get ECAD data fail
    SIWAVE_LAUNCH_FAILURE = "Cannot Launch SIwave {}"
    AEDT_LAUNCH_FAILURE = "Cannot Launch Ansys Electronics Desktop {}"
    ANF_IMPORT_FAIL = "Fail to import ANF file : {}"
    CMP_IMPORT_FAIL = "Fail to import CMF file : {}"
    STK_IMPORT_FAIL = "Fail to import stackup file : {}"
    SAVE_SIWAVE_FAIL = "Fail to save SIwave project : {}"
    CAD_IMPORT_FAIL = "Fail to import CAD file : {}"

    # 4. Modify CAD Data fail
    EDB_DATABASE_OPEN_FAIL = "Fail to open EDB database: {}"
    GND_NET_DETECT_FAIL = "Fail to detect ground net"
    SANITIZE_FAIL = "Fail to sanitize target net"
    INVALID_ANALOG_SWITCH = "Cannot find analog switch : {}"
    INVALID_ANALOG_SWITCH_PIN = "Analog switch pin is not valid: {} - {}"
    ZERO_OHM_INSTALL_FAIL = "Fail to install zero-ohm resistor: {}"

    # Find Bulk Ind. Fail (전처리 코드의 LocalErrorCode 병합)
    TARGET_NET_TRACE_FAIL = "Fail to trace target net: The pin {} does not exist in component {}."
    INVALID_BULK_IND_NUM = "More than one bulk inductor detected: {}"
    INVALID_FET_CONNECT = "FET connection is not valid: {}"
    INVALID_FET_COMPONENT = "FET component does not have 3-pins: {}"
    INVALID_FET_NUM = "More than one FET/TR detected : {}"
    INVALID_BEAD_NUM = "More than one bead inductor detected : {}"
    NO_LDO = "No LDO was detected : {}"
    NO_LDO_GND_PIN = "No LDO ground pin"
    INVALID_LDO_NUM = "More than one LDOs were detected : {}"
    INVALID_SW_NUM = "More than one or no LDO was detected : {}"
    INVALID_DCDC_NUM = "More than one DCDC detected : {}"  # 추가됨
    NO_SOURCE_FOUND = "No source found for net : {}"       # 추가됨
    LOOP_DETECTED = "Loop detected at net : {}"            # 추가됨
    INVALID_BULK_IND_PIN = "Bulk inductor pin is not valid: {} - {}"
    INVALID_BULK_IND_PLACE = "Bulk inductor placement is not valid: {}"
    NO_INCLUDED_NET = "No included net is detected : {}"
    INVALID_VOLTAGE_SOURCE_NEG_TERMINAL_PLACE = "Negative Terminal of the Voltage source is not GND: {} - {} - {} - {}"

    # DCIR simulation setting fail
    VOLTAGE_SOURCE_INSTALL_FAIL = "Fail to install voltage source: {}"
    CURRENT_SOURCE_INSTALL_FAIL = "Fail to install current source: {}"

    # Run DCIR fail
    SIWAVE_EXECUTABLE_NOT_FOUND = "SIwave executable not found: {}"
    DCIR_COMMAND_SIMULATION_FAIL = "DCIR simulation failed: {}"

    # HFSS 3D Layout Field Creation Fail
    MESH_FIELD_PLOT_FAIL = "Fail to create mesh field plot: {}"

    # Load DCIR results fail
    DCIR_RESULT_NOT_FOUND = "DCIR simulation result not found: {}"
    PYTHON_INTERPRETER_NOT_FOUND = "Python interpreter not found: {}"
    PY_FILE_NOT_FOUND = "Python file not found: {}"
    SIWZ_FILE_NOT_FOUND = "SIwave archive file not found: {}"
    CASE_IMAGE_EXPORT_FAIL = "Exporting Case & Image files failed: {}"


class InValChk:
    """Base class for Input JSON validation check."""

    def __init__(self, input_data, base_path, logger):
        self._inputData = input_data
        self._basePath = base_path
        self._logger = logger
        self._default_inputFiles = {
            'Spec': Path(base_path) / input_data['CAE']['SOC']['Spec'],
            'cadFile': Path(base_path) / input_data['CAE']['PCB']['cadFile'],
            'Stackup': Path(base_path) / input_data['CAE']['PCB']['Stackup'],
            'BOM': Path(base_path) / input_data['CAE']['PCB']['BOM']
        }
        self._optional_inputFiles = {
            'Pmap': Path(base_path) / input_data['CAE']['PCB']['Pmap'],
            'Inner_cap': Path(base_path) / input_data['CAE']['SOC']['Inner_cap']
        }

    def is_valid(self):
        """Set the input file path."""
        for key, file in self._default_inputFiles.items():
            if not (file.is_file() and file.exists()):
                file = file.parent / key / file.name
                if file.is_file() and file.exists():
                    self._default_inputFiles[key] = file
                else:
                    raise DCIRSessionException(ErrorCode.INPUT_FILE_NOT_FOUND, file)
            self._logger.log(f"Pass - {key} : {file}", level=LogLevel.DETAIL2)

        for key, file in self._optional_inputFiles.items():
            if file.is_file() and file.exists():
                self._logger.log(f"Pass - {key} : {file}", level=LogLevel.DETAIL2)
            else:
                file = file.parent / key / file.name
                if file.is_file() and file.exists():
                    self._optional_inputFiles[key] = file
                    self._logger.log(f"Pass - {key} : {file}", level=LogLevel.DETAIL2)
                else:
                    self._optional_inputFiles[key] = ''
                    self._logger.log(f"Skip - {key} : {file}", level=LogLevel.DETAIL2)

        return self._default_inputFiles, self._optional_inputFiles


def _message(code, output_path, *args):
    try:
        formatted_value = f'{code.value}'.format(*args)
    except IndexError:
        formatted_value = f'{code.value} (args: {args})'

    template = f'[{code.name}] - {formatted_value}'
    error_dict = {code.name: formatted_value}
    
    if output_path is None:
        out_dir = Path(".")
    else:
        out_dir = Path(output_path)
        
    out_dir.mkdir(parents=True, exist_ok=True)
    error_file = out_dir / "ERROR.json"
    
    with open(error_file, "w", encoding="utf-8") as f:
        json.dump(error_dict, f, indent=4, ensure_ascii=False)
        
    return template


class DCIRSessionException(Exception):
    """Provides the base class for exceptions related to EDB sessions."""
    OUTPUT_DIR = None

    def __init__(self, code, *args):
        """Initialize EDBSessionException."""
        super().__init__(_message(code, self.OUTPUT_DIR, *args))
        self._code = code