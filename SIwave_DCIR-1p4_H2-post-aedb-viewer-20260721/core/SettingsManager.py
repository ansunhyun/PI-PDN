# coding=utf-8
# <2025> ANSYS, Inc. Unauthorized use, distribution, or duplication is prohibited

import json
import csv
import gc
from pathlib import Path
from core.logger import LogLevel  # 💡 [추가] 로깅 레벨 임포트

# =========================================================================
# Design Force 라이선스 문제 대비 재시도 설정
# =========================================================================
MAX_RETRIES = 3      # 최대 재시도 횟수
RETRY_DELAY = 120    # 재시도 대기 시간 (초 단위, 120초 = 2분)

class SettingsManager:
    """
    Parses and manages configuration settings from a JSON file provided by an upper-level platform.

    Handles schema changes gracefully and provides safe access to key application settings.
    """

    def __init__(self, json_path, configuration=None, logger=None):
        self.json_path = Path(json_path)
        self.conf = configuration
        self.logger = logger
        self.data = self._load_json()
        
        # 💡 [추가] 파싱된 데이터를 보관할 내부 변수
        self.bom_data = None
        self.spec_data = None

    def _load_json(self):
        if not self.json_path.exists():
            raise FileNotFoundError(f"JSON file does not exist: {self.json_path}")
        try:
            with open(self.json_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"JSON parsing error: {e}")

    def _load_bom(self, bom_file=None):
        try:
            with open(bom_file, newline='', encoding='utf-8') as csvfile:
                reader = list(csv.reader(csvfile))
                # Remove line numbers and colons
                data = [
                    row[1:] if row and row[0].strip().endswith(':') else row
                    for row in reader if row
                ]
                # Transpose rows to columns
                columns = list(zip(*data))
                result = {}
                col_keys = set(self.conf.data['DCIR']['BOM'].get('colKey', []))
                for col in columns:
                    key = col[0].strip()
                    if key not in col_keys:
                        continue
                    values = [v.strip().replace("'", "") for v in col[1:]]
                    if not values:
                        if self.logger:
                            self.logger.log("BOM Test", level=LogLevel.SECTION, line_change=False)
                        continue

                    # Only keep the first non-empty value for each column
                    result[key] = values[0] if len(values) == 1 else values
            return result
        except Exception as e:
            raise ValueError(f"BOM parsing error: {e}")

    def _load_spec(self, spec_file=None):
        try:
            spec_info = []
            # 💡 [추가] 미실장 부품을 판별하기 위한 키워드 리스트
            dni_keywords = ['DNI', 'DNP', 'NC', 'NOT FITTED', 'NO MOUNT']
            
            with open(spec_file, newline='', encoding='utf-8') as csvfile:
                reader = list(csv.reader(csvfile))
                for row in reader[2:]:
                    contents = {}
                    is_dni = False  # 💡 [추가] 미실장 부품 여부 플래그
                    
                    for key in reader[1][1:]:
                        clean_key = key.strip().replace("'", "")
                        clean_val = row[reader[1].index(key)].strip().replace("'", "")
                        
                        if clean_key:
                            contents[clean_key] = clean_val
                            
                            # 💡 [추가] Description이나 Part Number에 미실장 키워드가 있는지 검사
                            if clean_key.upper() in ['DESCRIPTION', 'PART NUMBER', 'REMARK', 'SPEC']:
                                if any(kw in clean_val.upper() for kw in dni_keywords):
                                    is_dni = True
                    
                    # 💡 [추가] 미실장 부품이 아닌 경우에만 spec_info에 추가
                    if not is_dni:
                        spec_info.append(contents)
                        
            return spec_info
        except Exception as e:
            raise ValueError(f"SPEC parsing error: {e}")

    # =========================================================================
    # 💡 [신규 추가] 전처리 코드에서 분리된 파싱 및 데이터 관리 메서드들
    # =========================================================================
    def parse_bom_and_partlist(self, bom_file):
        """BOM과 Partlist를 파싱하여 내부에 저장합니다."""
        bom_info = {}
        file_type, encoding_used, delimiter = "UNKNOWN", 'utf-8-sig', ','

        try:
            # 💡 [추가된 로직] BOM 파일 확장자 확인 및 Excel -> CSV 변환
            bom_file_path = Path(bom_file)
            if bom_file_path.suffix.lower() in ['.xlsx', '.xls']:
                if self.logger:
                    self.logger.log("Excel format detected for BOM. Converting to CSV...", level=LogLevel.DETAIL1)
                try:
                    import pandas as pd
                    csv_bom_path = bom_file_path.with_suffix('.csv')
                    
                    # Excel 파일을 읽어 CSV로 저장 (한글 깨짐 방지를 위해 utf-8-sig 인코딩 사용)
                    df = pd.read_excel(bom_file_path)
                    df.to_csv(csv_bom_path, index=False, encoding='utf-8-sig')
                    
                    # 이후 로직에서 변환된 CSV 파일을 사용하도록 경로 업데이트
                    bom_file = str(csv_bom_path)
                    if self.logger:
                        self.logger.log(f"Successfully converted BOM to: {csv_bom_path.name}", level=LogLevel.DETAIL1)
                        
                except ImportError:
                    if self.logger:
                        self.logger.log("[ERROR] Pandas library is required to read Excel files. Please install it (pip install pandas openpyxl).", level=LogLevel.ERROR)
                    raise
                except Exception as e:
                    if self.logger:
                        self.logger.log(f"[ERROR] Failed to convert Excel to CSV: {e}", level=LogLevel.ERROR)
                    raise

            # 1. 인코딩 및 구분자 판별
            try:
                with open(bom_file, 'r', encoding='utf-8-sig') as f: sample = f.read(4096)
            except UnicodeDecodeError:
                encoding_used = 'cp949'
                with open(bom_file, 'r', encoding='cp949') as f: sample = f.read(4096)            

            # 2. 파일 타입 판별 (BOM vs PARTLIST)
            with open(bom_file, 'r', encoding=encoding_used) as f:
                cleaned_headers = [str(h).strip() for h in next(csv.reader(f, delimiter=delimiter)) if h]

            # 💡 [수정됨] Partlist와 BOM을 명확히 구분하기 위한 고유 식별 인자 적용
            # 양쪽 양식에 모두 존재할 수 있는 'Part No.' 등은 제외하고 확실한 인자만 배치했습니다.
            partlist_keywords = ['Assy Type', 'Site Specification', 'Obsoleteness', 'Reference Designator', 'Use for Schematic', 'Component Type']
            bom_keywords = ['Lvl', 'Rev', 'State', 'Qty', 'UOM', 'Supply Type', 'Designator/Split Qty']

            if any(sig in cleaned_headers for sig in partlist_keywords):
                file_type = "PARTLIST"
            elif any(sig in cleaned_headers for sig in bom_keywords):
                file_type = "BOM"
            else:
                # 명확한 키워드가 없을 경우의 예외 처리 (첫 번째 열 이름 기준)
                file_type = "PARTLIST" if cleaned_headers and cleaned_headers[0].upper() in ['DESIGNATOR', 'REFERENCE DESIGNATOR'] else "BOM"

            if self.logger: self.logger.log(f"Detected File Type: {file_type}", level=LogLevel.DETAIL2)

            # 3. 데이터 추출 헬퍼 함수
            def find_column_data(data_dict, keys):
                for k in keys:
                    match = next((rk for rk in data_dict if rk.replace(" ", "").replace(".", "").lower() == k.replace(" ", "").replace(".", "").lower()), None)
                    if match: return data_dict[match]
                return []

            def find_combined_column_data(data_dict, keys):
                matched_keys = [rk for k in keys for rk in data_dict if rk.replace(" ", "").replace(".", "").lower() == k.replace(" ", "").replace(".", "").lower()]
                matched_keys = list(dict.fromkeys(matched_keys)) 
                if not matched_keys: return []
                return [" ".join(str(data_dict[k][i]).strip() for k in matched_keys if str(data_dict[k][i]).strip()) for i in range(len(data_dict[matched_keys[0]]))]

            # 4. Raw 데이터 로드
            if file_type == "BOM":
                raw_bom_info = self._load_bom(bom_file)
            else:
                raw_bom_info = {}
                with open(bom_file, 'r', encoding=encoding_used) as f:
                    reader = csv.DictReader(f, delimiter=delimiter)
                    cleaned_headers = [str(h).strip() for h in reader.fieldnames if h]
                    reader.fieldnames = cleaned_headers
                    for header in cleaned_headers: raw_bom_info[header] = []
                    for row in reader:
                        for header in cleaned_headers: raw_bom_info[header].append(row.get(header, ""))

            # 5. 필수 컬럼 매핑
            possible_des_keys = ['Designator', 'RefDes', 'Reference', 'PartReference', 'Designators', 'Ref', 'Reference Designator']
            possible_desc_keys = ['Description', 'Desc', 'PartDescription', 'Site Specification', 'SiteSpec', 'Specification', 'Spec']
            possible_pn_keys = ['PartNumber', 'Part_Number', 'PN', 'Part Number', 'Part No.', 'Part No']

            bom_info['Designator'] = find_column_data(raw_bom_info, possible_des_keys)
            bom_info['Description'] = find_combined_column_data(raw_bom_info, possible_desc_keys)
            bom_info['PartNumber'] = find_column_data(raw_bom_info, possible_pn_keys)

            if not bom_info['Designator']:
                raise KeyError(f"Designator column not found. Available columns: {list(raw_bom_info.keys())}")

            bom_info['Designators'] = {comp.strip() for v in bom_info['Designator'] for comp in str(v).split(',') if comp.strip()}
            bom_info['config'] = self.conf.data['DCIR']['BOM']['compProp']

            # 6. 부품 카테고리 분류 (UNIVERSAL_RULES)
            UNIVERSAL_RULES = {
                'analogSwitch': ['analog switch', 'spdt', 'multiplexer', 'mux', 'load switch', 'power switch'],
                'DCDC': ['reg buck', 'reg boost', 'dc/dc', 'dc-dc', 'dc,dc', 'step-down', 'step-up', 'converter', 'regulator'],
                'LDO': ['ldo', 'linear regulator', 'low drop'],
                'bulkInd': ['power inductor', 'choke', 'coil', 'ind'],
                'beadInd': ['bead', 'ferrite'],
                'zeroOhm': ['0 ohm', '0ohm', 'jumper', 'short']
            }

            for idx, description in enumerate(bom_info.get('Description', [])):
                desc_lower = str(description).lower()
                part_num_lower = str(bom_info.get('PartNumber', [])[idx]).lower() if idx < len(bom_info.get('PartNumber', [])) else ""
                
                if idx >= len(bom_info.get('Designator', [])): continue
                designators = {item.strip() for item in str(bom_info['Designator'][idx]).split(',') if item.strip()}
                    
                for key, val in self.conf.data['DCIR']['BOM']['compProp'].items():
                    if str(val.get("Description", "")).lower() in desc_lower and str(val.get("Description", "")):
                        bom_info.setdefault(key, set()).update(designators)
                        
                for category, keywords in UNIVERSAL_RULES.items():
                    if any(kw in desc_lower or kw in part_num_lower for kw in keywords):
                        bom_info.setdefault(category, set()).update(designators)
                        break 
                        
                for des in designators:
                    prefix = ''.join(filter(str.isalpha, des)).upper()
                    if prefix == 'L' and des not in bom_info.get('beadInd', set()): bom_info.setdefault('bulkInd', set()).add(des)
                    elif prefix == 'F': bom_info.setdefault('bulkInd', set()).add(des)
                    elif prefix == 'R' and any(kw in desc_lower for kw in UNIVERSAL_RULES['zeroOhm']): bom_info.setdefault('bulkInd', set()).add(des)
                            
            for key in ['analogSwitch', 'DCDC', 'LDO', 'bulkInd', 'beadInd', 'FET', 'TR']:
                bom_info.setdefault('Designators', set()).update(bom_info.get(key, set()))

            # 파싱 완료된 데이터를 클래스 내부에 저장
            self.bom_data = bom_info
            return True

        except Exception as e:
            if self.logger: self.logger.log(f"Failed to parse BOM/Partlist: {e}", level=LogLevel.ERROR)
            return False

    def parse_spec(self, spec_file):
        """SPEC 파일을 파싱하여 내부에 저장합니다."""
        try:
            self.spec_data = self._load_spec(spec_file)
            return True
        except Exception as e:
            if self.logger: self.logger.log(f"Failed to parse SPEC: {e}", level=LogLevel.ERROR)
            return False

    def get_bom(self):
        """파싱된 BOM 데이터를 반환합니다."""
        if self.bom_data is None:
            raise ValueError("BOM data has not been parsed yet.")
        return self.bom_data

    def get_spec(self):
        """파싱된 SPEC 데이터를 반환합니다."""
        if self.spec_data is None:
            raise ValueError("SPEC data has not been parsed yet.")
        return self.spec_data
    # =========================================================================

    def get_version(self):
        # return self.data.get("version", "unknown")
        pass

    def get_setting(self, key, default=None):
        # return self.data.get("settings", {}).get(key, default)
        pass

    def get_metadata(self):
        # return self.data.get("metadata", {})
        pass

    def is_logging_enabled(self):
        # return self.get_setting("enable_logging", False)
        pass

    def validate_required_settings(self, required_keys):
        # missing = [k for k in required_keys if k not in self.data.get("settings", {})]
        # if missing:
        #     raise KeyError(f"raise KeyError(f"Required settings are missing: {missing}"): {missing}")
        pass