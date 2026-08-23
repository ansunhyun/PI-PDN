# coding=utf-8
# <2025> ANSYS, Inc. Unauthorized use, distribution, or duplication is prohibited

import json
import csv
import gc
import re
from pathlib import Path
from core.logger import LogLevel

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
        
        self.bom_data = None
        self.spec_data = None
        self.inner_cap_data = None

    def _load_json(self):
        if not self.json_path.exists():
            raise FileNotFoundError(f"JSON file does not exist: {self.json_path}")
        try:
            with open(self.json_path, 'r', encoding='utf-8-sig') as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            raise ValueError(f"JSON parsing error: {e}")

    @staticmethod
    def _norm_token(text):
        return re.sub(r"[^A-Za-z0-9]", "", str(text or "").upper())

    def _get_engine_conf(self):
        conf_data = getattr(self.conf, "data", {}) if self.conf is not None else {}
        return conf_data.get("PDN", {})

    def _load_bom(self, bom_file=None, encoding='utf-8-sig', delimiter=','):
        try:
            with open(bom_file, newline='', encoding=encoding) as csvfile:
                reader = list(csv.reader(csvfile, delimiter=delimiter))
                data = [
                    row[1:] if row and row[0].strip().endswith(':') else row
                    for row in reader if row
                ]
                columns = list(zip(*data))
                result = {}
                
                # 1. JSON 설정 파일에 정의된 컬럼 키워드 가져오기 및 정규화
                engine_conf = self._get_engine_conf()
                col_keys = set(engine_conf.get('BOM', {}).get('colKey', []))
                norm_col_keys = {k.replace(" ", "").replace("_", "").replace(".", "").lower() for k in col_keys}
                
                # 2. 주요 4대 컬럼(Part No, Description, Tech Spec, Designator) 및 유사 명칭 키워드 풀
                essential_keywords = {
                    'designator', 'refdes', 'reference', 'partreference', 'ref', 'referencedesignator',
                    # Maker PN 관련 키워드 대폭 확장 (MPN, Vendor, Supplier 등 추가)
                    'partno', 'partnumber', 'pn', 'makerpn', 'makerpartnumber', 'makerpartno', 'mfrpn', 'mfrpartnumber', 'mfgpn',
                    'mpn', 'vendorpn', 'vendorpartnumber', 'supplierpn', 'supplierpartnumber', 'manufacturerpn',
                    'description', 'desc', 'partdescription',
                    'technicalspec', 'techspec', 'specification', 'spec', 'sitespecification', 'sitespec'
                }
                
                # 3. JSON 설정 키워드와 필수 키워드를 통합
                valid_keywords = norm_col_keys.union(essential_keywords)

                for col in columns:
                    key = col[0].strip()
                    if not key:
                        continue
                        
                    # 헤더 정규화 (공백, 특수문자 제거 및 소문자 변환)
                    norm_key = key.replace(" ", "").replace("_", "").replace(".", "").lower()
                    
                    # 정규화된 키가 유효한 키워드 풀에 포함될 때만 메모리에 적재
                    if norm_key not in valid_keywords:
                        continue
                        
                    values = [v.strip().replace("'", "") for v in col[1:]]
                    if not values:
                        if self.logger:
                            self.logger.log("BOM Test", level=LogLevel.SECTION, line_change=False)
                        continue
                    result[key] = values[0] if len(values) == 1 else values
            return result
        except Exception as e:
            raise ValueError(f"BOM parsing error: {e}")

    # 💡 [원복] Net Searching을 위한 PDN Spec 파싱 로직 복구
    def _load_spec(self, spec_file=None):
        try:
            spec_info = []
            dni_keywords = ['DNI', 'DNP', 'NC', 'NOT FITTED', 'NO MOUNT']
            
            with open(spec_file, newline='', encoding='utf-8-sig') as csvfile:
                reader = list(csv.reader(csvfile))
                if not reader:
                    return spec_info

                headers = [str(h).strip().replace("'", "") for h in reader[0]]
                
                for row in reader[1:]:
                    if not row or not any(row):
                        continue
                        
                    contents = {}
                    is_dni = False
                    
                    for i, key in enumerate(headers):
                        if not key or i >= len(row):
                            continue
                            
                        clean_val = str(row[i]).strip().replace("'", "")
                        contents[key] = clean_val
                        
                        if key.upper() in ['DESCRIPTION', 'PART NUMBER', 'REMARK', 'SPEC']:
                            if any(kw in clean_val.upper() for kw in dni_keywords):
                                is_dni = True
                                
                    if not is_dni and contents:
                        spec_info.append(contents)
                        
            return spec_info
        except Exception as e:
            raise ValueError(f"SPEC parsing error: {e}")

    # =========================================================================
    # 파싱 및 데이터 관리 메서드들
    # =========================================================================
    def parse_bom_and_partlist(self, bom_file):
        """BOM과 Partlist를 파싱하여 내부에 저장합니다."""
        bom_info = {}
        file_type, encoding_used, delimiter = "UNKNOWN", 'utf-8-sig', ','

        try:
            bom_file_path = Path(bom_file)
            if bom_file_path.suffix.lower() in ['.xlsx', '.xls']:
                if self.logger:
                    self.logger.log("Excel format detected for BOM. Converting to CSV...", level=LogLevel.DETAIL1)
                try:
                    import pandas as pd
                    csv_bom_path = bom_file_path.with_suffix('.csv')
                    df = pd.read_excel(bom_file_path)
                    df.to_csv(csv_bom_path, index=False, encoding='utf-8-sig')
                    bom_file = str(csv_bom_path)
                    if self.logger:
                        self.logger.log(f"Successfully converted BOM to: {csv_bom_path.name}", level=LogLevel.DETAIL1)
                except ImportError:
                    if self.logger:
                        self.logger.log("[ERROR] Pandas library is required to read Excel files.", level=LogLevel.ERROR)
                    raise
                except Exception as e:
                    if self.logger:
                        self.logger.log(f"[ERROR] Failed to convert Excel to CSV: {e}", level=LogLevel.ERROR)
                    raise

            try:
                with open(bom_file, 'r', encoding='utf-8-sig') as f: sample = f.read(4096)
            except UnicodeDecodeError:
                encoding_used = 'cp949'
                with open(bom_file, 'r', encoding='cp949') as f: sample = f.read(4096)            

            try:
                sniff = csv.Sniffer().sniff(sample, delimiters=",;\t|")
                delimiter = sniff.delimiter
            except Exception:
                delimiter = ','

            with open(bom_file, 'r', encoding=encoding_used) as f:
                cleaned_headers = [str(h).strip() for h in next(csv.reader(f, delimiter=delimiter)) if h]

            partlist_keywords = ['Assy Type', 'Site Specification', 'Obsoleteness', 'Reference Designator', 'Use for Schematic', 'Component Type']
            bom_keywords = ['Lvl', 'Rev', 'State', 'Qty', 'UOM', 'Supply Type', 'Designator/Split Qty']

            if any(sig in cleaned_headers for sig in partlist_keywords):
                file_type = "PARTLIST"
            elif any(sig in cleaned_headers for sig in bom_keywords):
                file_type = "BOM"
            else:
                file_type = "PARTLIST" if cleaned_headers and cleaned_headers[0].upper() in ['DESIGNATOR', 'REFERENCE DESIGNATOR'] else "BOM"

            if self.logger: self.logger.log(f"Detected File Type: {file_type}", level=LogLevel.DETAIL2)

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

            if file_type == "BOM":
                raw_bom_info = self._load_bom(bom_file, encoding=encoding_used, delimiter=delimiter)
            else:
                raw_bom_info = {}
                
                # 1. JSON 설정 파일에 정의된 컬럼 키워드 가져오기 및 정규화
                engine_conf = self._get_engine_conf()
                col_keys = set(engine_conf.get('BOM', {}).get('colKey', []))
                norm_col_keys = {k.replace(" ", "").replace("_", "").replace(".", "").lower() for k in col_keys}
                
                # 2. Partlist 주요 컬럼 및 유사 명칭 키워드 풀
                essential_keywords = {
                    'referencedesignator', 'designator', 'refdes', 'reference', 'partreference', 'ref',
                    'description', 'desc', 'partdescription',
                    'sitespecification', 'sitespec', 'specification', 'spec', 'technicalspec', 'techspec',
                    # Maker PN 관련 키워드 대폭 확장
                    'partno', 'partnumber', 'pn', 'makerpn', 'makerpartnumber', 'makerpartno', 'mfrpn', 'mfrpartnumber', 'mfgpn',
                    'mpn', 'vendorpn', 'vendorpartnumber', 'supplierpn', 'supplierpartnumber', 'manufacturerpn'
                }
                
                # 3. JSON 설정 키워드와 필수 키워드를 통합
                valid_keywords = norm_col_keys.union(essential_keywords)

                with open(bom_file, 'r', encoding=encoding_used) as f:
                    reader = csv.DictReader(f, delimiter=delimiter)
                    cleaned_headers = [str(h).strip() for h in reader.fieldnames if h]
                    reader.fieldnames = cleaned_headers
                    
                    # 4. 유효한 컬럼만 필터링하여 타겟 헤더로 지정
                    target_headers = []
                    for header in cleaned_headers:
                        norm_header = header.replace(" ", "").replace("_", "").replace(".", "").lower()
                        if norm_header in valid_keywords:
                            target_headers.append(header)
                            raw_bom_info[header] = []
                            
                    # 5. 필터링된 주요 컬럼의 데이터만 메모리에 적재
                    for row in reader:
                        for header in target_headers:
                            raw_bom_info[header].append(row.get(header, ""))

            possible_des_keys = ['Designator', 'RefDes', 'Reference', 'PartReference', 'Designators', 'Ref', 'Reference Designator']
            
            # 💡 Description과 Technical Spec 키워드 분리
            possible_desc_keys = ['Description', 'Desc', 'PartDescription']
            possible_tech_spec_keys = ['Technical Spec', 'TechnicalSpec', 'Tech Spec', 'TechSpec', 'Site Specification', 'SiteSpec', 'Specification', 'Spec']
            
            possible_pn_keys = ['PartNumber', 'Part_Number', 'PN', 'Part Number', 'Part No.', 'Part No']
            
            # 실무에서 사용되는 다양한 제조사 품번 컬럼명 패턴 대폭 추가
            possible_maker_pn_keys = [
                'Maker PN', 'Maker Part Number', 'MakerPN', 'Maker_PN', 'Maker Part No',
                'Mfr PN', 'Mfr Part Number', 'Manufacturer Part Number', 'MFG PN', 'MFG Part No',
                'MPN', 'Vendor PN', 'Vendor Part Number', 'Supplier PN', 'Supplier Part Number', 'Manufacturer PN'
            ]

            bom_info['Designator'] = find_column_data(raw_bom_info, possible_des_keys)
            bom_info['Description'] = find_combined_column_data(raw_bom_info, possible_desc_keys)
            bom_info['TechnicalSpec'] = find_column_data(raw_bom_info, possible_tech_spec_keys)
            bom_info['PartNumber'] = find_column_data(raw_bom_info, possible_pn_keys)
            bom_info['MakerPartNumber'] = find_column_data(raw_bom_info, possible_maker_pn_keys)
            
            # Part Number 컬럼을 찾지 못한 경우의 예외 처리
            if not bom_info['PartNumber']:
                for k, v in raw_bom_info.items():
                    nk = str(k).replace(" ", "").replace(".", "").replace("_", "").lower()
                    if nk in ("partno", "partnumber", "pn"):
                        bom_info['PartNumber'] = v
                        break

            if not bom_info['Designator']:
                raise KeyError(f"Designator column not found. Available columns: {list(raw_bom_info.keys())}")

            # 💡 [핵심 추가] Maker PN 보정 및 Technical Spec 기반 추출 로직
            num_rows = len(bom_info['Designator'])
            maker_pns = list(bom_info['MakerPartNumber'])
            
            # 1. Maker PN 컬럼이 아예 없는 경우, 일반 Part Number를 Maker PN으로 대체
            if not maker_pns:
                if bom_info['PartNumber']:
                    if self.logger:
                        self.logger.log("[WARNING] Maker PN column not found. Falling back to Part Number for S-Parameter mapping.", level=LogLevel.WARNING)
                    maker_pns = list(bom_info['PartNumber'])
                else:
                    maker_pns = ["" for _ in range(num_rows)]
            
            # 리스트 길이 맞추기 (IndexError 방지)
            while len(maker_pns) < num_rows:
                maker_pns.append("")

            # 2. Capacitor인 경우 Technical Spec에서 제조사 품번(첫 번째 단어) 추출
            tech_specs = bom_info.get('TechnicalSpec', [])
            descriptions = bom_info.get('Description', [])
            
            for i in range(num_rows):
                desc = str(descriptions[i]).lower() if i < len(descriptions) else ""
                tech_spec = str(tech_specs[i]).strip() if i < len(tech_specs) else ""
                
                # Description에 capacitor 관련 키워드가 있는지 확인
                is_capacitor = any(kw in desc for kw in ['cap', 'capacitor', 'mlcc'])
                
                if is_capacitor and tech_spec:
                    # Technical Spec의 첫 번째 단어를 추출 (공백 기준 분리)
                    extracted_pn = tech_spec.split()[0]
                    if extracted_pn:
                        maker_pns[i] = extracted_pn
                        
            bom_info['MakerPartNumber'] = maker_pns

            bom_info['Designators'] = {comp.strip() for v in bom_info['Designator'] for comp in str(v).split(',') if comp.strip()}
            
            refdes_to_part = {}
            refdes_to_maker = {}
            part_rows = list(bom_info.get('PartNumber', []))
            maker_rows = list(bom_info.get('MakerPartNumber', []))
            
            for idx, des_group in enumerate(bom_info.get('Designator', [])):
                part = str(part_rows[idx]).strip() if idx < len(part_rows) else ""
                maker_pn = str(maker_rows[idx]).strip() if idx < len(maker_rows) else ""
                
                if not part and not maker_pn:
                    continue
                for des in [d.strip() for d in str(des_group).split(',') if d.strip()]:
                    if part:
                        refdes_to_part[des] = part
                    if maker_pn:
                        refdes_to_maker[des] = maker_pn

            bom_info['RefDesToPartNumber'] = refdes_to_part
            bom_info['RefDesToMakerPartNumber'] = refdes_to_maker

            engine_conf = self._get_engine_conf()
            bom_info['config'] = engine_conf.get('BOM', {}).get('compProp', {})

            # 💡 [원복] Net Searching을 위한 직렬 부품(Inductor, Bead, Resistor 등) 분류 로직 복구
            UNIVERSAL_RULES = {
                'analogSwitch': ['analog switch', 'spdt', 'multiplexer', 'mux', 'load switch', 'power switch'],
                'sourceComp': ['reg buck', 'reg boost', 'dc/dc', 'dc-dc', 'dc,dc', 'step-down', 'step-up', 'converter', 'regulator'],
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
                    
                for key, val in engine_conf.get('BOM', {}).get('compProp', {}).items():
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
                            
            for key in ['analogSwitch', 'sourceComp', 'LDO', 'bulkInd', 'beadInd', 'FET', 'TR']:
                bom_info.setdefault('Designators', set()).update(bom_info.get(key, set()))

            self.bom_data = bom_info
            return True

        except Exception as e:
            if self.logger: self.logger.log(f"Failed to parse BOM/Partlist: {e}", level=LogLevel.ERROR)
            return False

    # 💡 [원복] Spec 파싱 함수 복구
    def parse_spec(self, spec_file):
        """SPEC 파일을 파싱하여 내부에 저장합니다."""
        try:
            self.spec_data = self._load_spec(spec_file)
            return True
        except Exception as e:
            if self.logger: self.logger.log(f"Failed to parse SPEC: {e}", level=LogLevel.ERROR)
            return False

    def parse_inner_cap(self, file_path):
        """
        [PDN 전용] Inner Cap 파일을 파싱하여 내부에 저장합니다.
        Excel 파일인 경우 CSV로 자동 변환 후 파싱하며, 공백을 완벽히 제거합니다.
        """
        self.inner_cap_data = []
        if not file_path:
            return False
            
        file_path = Path(file_path)
        if not file_path.exists():
            if self.logger:
                self.logger.log(f"[WARNING] Inner Cap 파일을 찾을 수 없습니다: {file_path}", level=LogLevel.WARNING)
            return False

        try:
            if file_path.suffix.lower() in ['.xlsx', '.xls']:
                if self.logger:
                    self.logger.log("Excel format detected for Inner Cap. Converting to CSV...", level=LogLevel.DETAIL1)
                try:
                    import pandas as pd
                    csv_path = file_path.with_suffix('.csv')
                    df = pd.read_excel(file_path)
                    df.to_csv(csv_path, index=False, encoding='utf-8-sig')
                    file_path = csv_path
                    if self.logger:
                        self.logger.log(f"Successfully converted Inner Cap to: {csv_path.name}", level=LogLevel.DETAIL1)
                except ImportError:
                    if self.logger:
                        self.logger.log("[ERROR] Pandas library is required to read Excel files.", level=LogLevel.ERROR)
                    raise
                except Exception as e:
                    if self.logger:
                        self.logger.log(f"[ERROR] Failed to convert Excel to CSV: {e}", level=LogLevel.ERROR)
                    raise

            with open(file_path, mode='r', encoding='utf-8-sig') as f:
                reader = csv.reader(f)
                raw_headers = next(reader, None)
                if not raw_headers:
                    return False
                
                headers = [str(h).strip() for h in raw_headers]
                
                for row_idx, row in enumerate(reader, start=2):
                    clean_row = {headers[i]: str(val).strip() for i, val in enumerate(row) if i < len(headers)}
                    
                    designator = clean_row.get('Designator', '')
                    soc_net = clean_row.get('SoC net name', '')
                    pcb_net = clean_row.get('PCB net name', '')
                    pin_number = clean_row.get('Pin_number', '')
                    cap_value = clean_row.get('Cap.', '')
                    qty_text = clean_row.get('quantity', clean_row.get('Quantity', clean_row.get('QTY', '1')))
                    
                    # 정규화된 키 검색을 통해 유연하게 Maker PN 추출
                    maker_pn = ""
                    for k, v in clean_row.items():
                        norm_k = str(k).replace(" ", "").replace("_", "").replace(".", "").lower()
                        if norm_k in ['makerpn', 'makerpartnumber', 'makerpartno', 'mfrpn', 'mfrpartnumber', 'mfgpn']:
                            maker_pn = str(v).strip()
                            break
                    
                    if not designator or not pin_number or not cap_value:
                        continue 
                        
                    try:
                        qty_val = int(float(str(qty_text).strip())) if str(qty_text).strip() else 1
                    except Exception:
                        qty_val = 1
                    qty_val = max(1, qty_val)

                    for q_idx in range(qty_val):
                        self.inner_cap_data.append({
                            'component_name': designator,
                            'maker_part_number': maker_pn,
                            'Designator': designator,
                            'SoC_Net': soc_net,
                            'PCB_Net': pcb_net,
                            'Pin_Number': pin_number,
                            'Cap_Value': cap_value,
                            'Part_Number': clean_row.get('Part_number', ''),
                            'Decap_Type': clean_row.get('Decap type', ''),
                            'Quantity': qty_val,
                            'Quantity_Index': q_idx + 1,
                        })
                    
            if self.logger:
                self.logger.log(f"Successfully parsed {len(self.inner_cap_data)} Inner Caps from {file_path.name}", level=LogLevel.DETAIL1)
            return True
                
        except Exception as e:
            if self.logger:
                self.logger.log(f"[ERROR] Inner Cap 파일 파싱 실패: {e}", level=LogLevel.ERROR)
            return False

    # =========================================================================
    # 데이터 반환 메서드들
    # =========================================================================
    def get_bom(self):
        if self.bom_data is None:
            raise ValueError("BOM data has not been parsed yet.")
        return self.bom_data

    def get_spec(self):
        if self.spec_data is None:
            raise ValueError("SPEC data has not been parsed yet.")
        return self.spec_data

    def get_inner_cap(self):
        if self.inner_cap_data is None:
            raise ValueError("Inner Cap data has not been parsed yet.")
        return self.inner_cap_data

    def get_gnd_net(self):
        return self.data.get('CAE', {}).get('SOC', {}).get('GND_Net', 'GND')

    # =========================================================================
    # 기타 유틸리티 메서드 (더미)
    # =========================================================================
    def get_version(self):
        pass

    def get_setting(self, key, default=None):
        pass

    def get_metadata(self):
        pass

    def is_logging_enabled(self):
        pass

    def validate_required_settings(self, required_keys):
        pass