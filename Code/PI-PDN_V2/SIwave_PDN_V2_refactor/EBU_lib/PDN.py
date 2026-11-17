# coding=utf-8
# <2025> ANSYS, Inc. Unauthorized use, distribution, or duplication is prohibited
from core.logger import LogLevel

class PDN:
    """Base class for PDN applications.
    Handles PDN-specific business logic, BOM parsing, and workflows.
    """

    def __init__(self, logger=None):
        """Initialize the PDN application."""
        self.logger = logger

    def install_0ohm_resistors(self, app, comp_type, target_comp, comp_prop, exclude_prefixes):
        """
        BOM 속성을 기반으로 특정 부품 핀 사이에 0옴 저항을 설치합니다.
        (AC PDN 해석 시 스위치나 점퍼 등으로 분리된 전원 넷을 하나로 연결하기 위해 사용됩니다.)
        
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
                self.logger.log(f"0-ohm Install Error : {e}", level=LogLevel.ERROR)
            return False

    def apply_dc_shorts(self, app, shorted_comp_defs=None, del_comps=None, short_correction=None):
        """
        Apply short-net replacement on designated components for PDN preprocessing.
        - Create 0-ohm RLC across the original 2-pin short component.
        - Disable the original component to avoid duplicate connection.
        """
        shorted_comp_defs = set(shorted_comp_defs or [])
        del_comps = set(del_comps or [])
        created_count = 0
        disabled_count = 0
        skipped_count = 0

        try:
            for comp_name in del_comps:
                comp_inst = app.edb._components.components.get(comp_name)
                if not comp_inst:
                    skipped_count += 1
                    continue

                if shorted_comp_defs and comp_inst.component_def not in shorted_comp_defs:
                    skipped_count += 1
                    continue

                pins = list(comp_inst.pins.values())
                if len(pins) != 2:
                    skipped_count += 1
                    continue

                rlc_name = f"R_SHORT_{comp_name}"
                created = app.create_rlc_component(
                    pins=pins,
                    comp_name=rlc_name,
                    part_name="R_SHORT",
                    r_value=0.0,
                )
                if created:
                    created_count += 1
                    try:
                        comp_inst.enabled = False
                        disabled_count += 1
                    except Exception:
                        pass
                else:
                    skipped_count += 1

            if self.logger:
                self.logger.log(
                    f"[PDN][Short] Applied={created_count}, Disabled={disabled_count}, Skipped={skipped_count}, "
                    f"AliasGroups={len(short_correction or {})}",
                    level=LogLevel.DETAIL1,
                )
            return True
        except Exception as e:
            if self.logger:
                self.logger.log(f"[PDN][Short][ERROR] apply_dc_shorts failed: {e}", level=LogLevel.ERROR)
            return False
