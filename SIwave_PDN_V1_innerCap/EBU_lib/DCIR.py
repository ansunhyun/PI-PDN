# coding=utf-8
# <2025> ANSYS, Inc. Unauthorized use, distribution, or duplication is prohibited
from core.logger import LogLevel

class DCIR:
    """Base class for DCIR applications.
    Handles DCIR-specific business logic, BOM parsing, and workflows.
    """

    def __init__(self, logger=None):
        """Initialize the DCIR application."""
        self.logger = logger

    def install_0ohm_resistors(self, app, comp_type, target_comp, comp_prop, exclude_prefixes):
        """
        BOM 속성을 기반으로 특정 부품 핀 사이에 0옴 저항을 설치합니다.
        
        Args:
            app: SIwave 범용 제어 인스턴스 (EDB가 로드되어 있어야 함)
            comp_type: 부품 타입 (예: 'analogSwitch', 'FET' 등)
            target_comp: 대상 부품 이름 리스트
            comp_prop: 연결 속성 ('connectPin', 'connectType' 등)
            exclude_prefixes: 제외할 부품 Prefix 튜플
        """
        try:
            connect_pins_str = [str(p).strip() for p in comp_prop.get('connectPin', [])]
            connect_types_upper = {str(t).strip().upper() for t in comp_prop.get('connectType', [])}
            
            for idx, comp_name in enumerate(target_comp):
                if str(comp_name).startswith(tuple(exclude_prefixes)):
                    continue

                comp_inst = app.edb._components.components.get(comp_name, None)
                if not comp_inst: 
                    continue
                
                target_pins = []
                
                # 1. connectType (IN, OUT 등) 기반 핀 매칭
                if connect_types_upper:
                    matched_pins = {}
                    for pin_name, pin_inst in comp_inst.pins.items():
                        pin_upper = str(pin_name).upper()
                        net_upper = str(pin_inst.net_name).upper() if pin_inst.net_name else ""
                        for c_type in connect_types_upper:
                            if c_type not in matched_pins and (c_type in pin_upper or c_type in net_upper):
                                matched_pins[c_type] = pin_inst
                                break
                    
                    if len(matched_pins) >= 2:
                        target_pins = list(matched_pins.values())[:2]
                        
                # 2. connectPin (1, 3 등) 번호 기반 핀 매칭
                elif connect_pins_str:
                    temp_pins = [comp_inst.pins.get(str(p)) for p in connect_pins_str]
                    if all(temp_pins) and len(temp_pins) >= 2:
                        target_pins = temp_pins[:2]

                # 매칭된 핀이 있으면 SIwave 객체에 RLC 생성 명령 하달
                if target_pins:
                    app.create_rlc_component(
                        pins=target_pins, 
                        comp_name=f'R_{comp_type}_{idx}', 
                        part_name=f'R_{comp_type}', 
                        r_value=0
                    )
                    
            return True
        except Exception as e:
            if self.logger: 
                # LogLevel.ERROR에 해당하는 값 사용 (환경에 맞게 수정 가능)
                self.logger.log(f"0-ohm Install Error : {e}", level=LogLevel.ERROR)
            return False

    def apply_dc_shorts(self, app, shorted_comp_defs, del_comps, short_correction):
        """
        DC Short 해석을 위해 부품 타입을 변경하고, 불필요 부품을 삭제하며, Net을 병합합니다.
        
        Args:
            app: SIwave 범용 제어 인스턴스 (SIwave 프로젝트가 열려 있어야 함)
            shorted_comp_defs: 'Capacitor'로 변경할 부품 정의 리스트
            del_comps: 삭제할 부품 이름 Set/List
            short_correction: 병합할 Target Net과 Secondary Net들의 Dictionary
        """
        try:
            # 1. 특정 부품들을 Capacitor 타입으로 변경
            for part_name in shorted_comp_defs:
                app.change_part_type(part_name, 'Capacitor')
                
            # 2. 불필요한 부품 삭제
            for comp_name in del_comps:
                app.delete_circuit_element(comp_name)
                
            # 3. Net 병합 (Short)
            for target_net, merged_net in short_correction.items():
                merged_net.insert(0, target_net)
                app.merge_connected_nets(merged_net)
                
            return True
        except Exception as e:
            if self.logger: 
                self.logger.log(f"Apply DC Shorts Failed: {e}", level=LogLevel.ERROR)
            return False
