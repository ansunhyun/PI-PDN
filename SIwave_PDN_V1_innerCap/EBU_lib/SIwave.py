# coding=utf-8
# <2025> ANSYS, Inc. Unauthorized use, distribution, or duplication is prohibited

import clr
import os
import time
from pathlib import Path

clr.AddReference("System.Core")
from System.Collections.Generic import List

from pyaedt import Edb
from pyedb.siwave import Siwave
from core.database import DCIRSessionException, ErrorCode
from core.logger import LogLevel  

class SIwave:
    """Provides the SIwave application interface for general automation.
    Uses Composition to manage EDB and SIwave COM objects safely.
    """

    def __init__(self, version="2025.1", logger=None):
        self._version = version
        self.logger = logger
        self.edb = None
        self._siw_app = None

    @property
    def siw_app(self):
        if self._siw_app is None:
            try:
                self._siw_app = Siwave(specified_version=self._version)
                if self.logger:
                    self.logger.log(f"SIwave {self._version} launched successfully.", level=LogLevel.DETAIL2)
            except Exception as e:
                if self.logger:
                    self.logger.log(f"Failed to launch SIwave: {e}", level=LogLevel.ERROR)
                raise RuntimeError(f"SIwave COM Initialization Failed: {e}")
        return self._siw_app

    @property
    def oproject(self):
        return self.siw_app.oproject

    @property
    def oSiwave(self):
        return self.siw_app.oSiwave

    def quit_application(self, wait_time=2.0):
        """Quit SIwave application safely."""
        try:
            if self._siw_app:
                self._siw_app.quit_application()
                self._siw_app = None
                time.sleep(wait_time)  
        except Exception as e:
            if self.logger:
                self.logger.log(f"Failed to quit SIwave safely: {e}", level=LogLevel.WARNING)

    # ==========================================
    # Utility Methods
    # ==========================================
    @staticmethod
    def get_representative_net_name(net_chain):
        if not net_chain:
            return ""
            
        meaningful_nets = [net for net in net_chain if not net.startswith("SIGN")]
        
        if not meaningful_nets:
            return net_chain[0]
            
        target_net = meaningful_nets[0]
        for net in meaningful_nets:
            if net.startswith("+") and not net.startswith("SW_"):
                target_net = net
                break
                
        clean_name = target_net.replace("+", "").replace("SW_", "")
        return clean_name

    # ==========================================
    # EDB Manipulation Methods
    # ==========================================
    def set_cad_file(self, cad_file):
        try:
            self.edb = Edb(str(cad_file), edbversion=self._version)
            if self.edb.db.IsNull():
                raise ValueError("EDB database is null.")
            if self.logger:
                self.logger.log(f"EDB initialized with CAD file: {cad_file}", level=LogLevel.DETAIL2)
        except Exception as e:
            if self.logger:
                self.logger.log(f"EDB Open Error Details: {e}", level=LogLevel.ERROR)
            raise DCIRSessionException(ErrorCode.EDB_DATABASE_OPEN_FAIL, cad_file)

    def close_edb(self):
        try:
            if self.edb:
                self.edb.close_edb()
                self.edb = None
        except Exception as e:
            if self.logger:
                self.logger.log(f"Failed to close EDB safely: {e}", level=LogLevel.WARNING)

    def sanitize_nets(self, targetNetNameList):
        try:
            for net_name, net_inst in self.edb.nets.nets.items():
                if net_name in targetNetNameList:
                    prims = net_inst.primitives
                    layers = {i.layer_name for i in prims} 
                    
                    for layer in layers:
                        if layer in self.edb.stackup.stackup_layers:
                            polyInstList = List[self.edb._edb.Geometry.PolygonData]()
                            for prim in prims:
                                if not prim.is_void and prim.layer_name == layer:
                                    polygonData = prim.polygon_data._edb_object
                                    for void in prim.voids:
                                        polygonData.AddHole(void.polygon_data._edb_object)
                                    polyInstList.Add(polygonData)

                            if polyInstList.Count > 0:
                                united_poly = self.edb._edb.Geometry.PolygonData.Unite(polyInstList)
                                for poly in united_poly:
                                    try: self.edb._edb.Cell.Primitive.Polygon.Create(self.edb.active_layout, layer, net_inst.net_obj, poly)
                                    except: pass
                    for primitive in prims: primitive.delete()
            return True
        except Exception as e:
            if self.logger: self.logger.log(f"Sanitize Error : {e}", level=LogLevel.ERROR)
            return False

    def create_rlc_component(self, pins, comp_name, part_name, r_value=0):
        try:
            self.edb._components.create(pins, is_rlc=True, component_name=comp_name, component_part_name=part_name, r_value=r_value)
            return True
        except Exception as e:
            if self.logger: self.logger.log(f"Failed to create RLC {comp_name}: {e}", level=LogLevel.WARNING)
            return False

    def find_nearest_gnd(self, ref_coord, gnd_net):
        min_dist, best_coord, best_layer = float('inf'), None, None
        try:
            for comp in self.edb._components.components.values():
                for pin in comp.pins.values():
                    if pin.net_name == gnd_net:
                        dist = (pin.position[0] - ref_coord[0])**2 + (pin.position[1] - ref_coord[1])**2
                        if dist < min_dist:
                            min_dist, best_coord, best_layer = dist, pin.position, comp.placement_layer
        except Exception as e:
            if self.logger: self.logger.log(f"Find Nearest GND Error: {e}", level=LogLevel.WARNING)
        return best_coord, best_layer
        
    # 💡 [수정] 파라미터에 bulk_inductor_list=None 추가
    def prepare_vrm_connection(self, target_net, dcdc_name, dcdc_pin, gnd_net, net_chain, inductor_prefix='L', bulk_inductor_list=None):
        """
        전압원(VRM)을 인가할 최적의 좌표와 레이어를 찾습니다. (범용 탐색 알고리즘)
        1. DCDC/LDO 출력단에 Power Inductor가 있으면 -> 인덕터의 IC 방향 패드에 전압원 인가 (인덕터는 이후 Main에서 DNI 처리됨)
        2. Power Inductor가 없으면 -> DCDC/LDO의 출력 핀에 직접 전압원 인가
        """
        try:
            dcdc_comp = self.edb._components.components.get(dcdc_name)
            if not dcdc_comp:
                if self.logger: self.logger.log(f"[WARNING] DCDC component '{dcdc_name}' not found.", level=LogLevel.WARNING)
                return None, None, None, None, None

            # 1. DCDC 출력 핀 및 네트 확인
            dcdc_pin_inst = dcdc_comp.pins.get(dcdc_pin)
            if not dcdc_pin_inst:
                # 핀 이름이 누락되었거나 안 맞을 경우, net_chain에 포함된 핀을 자동 탐색 (Fallback)
                for p_name, p_inst in dcdc_comp.pins.items():
                    if p_inst.net_name in net_chain:
                        dcdc_pin_inst = p_inst
                        dcdc_pin = p_name
                        break
            
            if not dcdc_pin_inst:
                if self.logger: self.logger.log(f"[WARNING] Valid output pin for '{dcdc_name}' not found in net_chain.", level=LogLevel.WARNING)
                return None, None, None, None, None

            dcdc_out_net = dcdc_pin_inst.net_name
            
            # 2. Power Inductor 탐색 (DCDC 출력 네트와 net_chain 내의 다른 네트를 연결하는 직렬 소자)
            target_inductor = None
            ic_side_pin = None
            
            for comp_name, comp_inst in self.edb._components.components.items():
                # 인덕터(L) 또는 비드(B, FB) 필터링
                if comp_name.startswith(inductor_prefix) or comp_name.startswith(('B', 'FB')):
                    
                    # 💡 [추가] BOM에 정의된 Power Inductor(bulkInd)가 아니면 건너뜀 (비드나 2차 인덕터 무시)
                    if bulk_inductor_list is not None and comp_name not in bulk_inductor_list:
                        continue

                    comp_nets = {p.net_name: p for p in comp_inst.pins.values() if p.net_name}
                    
                    # 해당 소자가 DCDC 출력 네트(예: SIGN00374)에 연결되어 있는지 확인
                    if dcdc_out_net in comp_nets:
                        # 소자의 다른 핀이 net_chain에 포함되어 있는지 확인 (이것이 IC 방향 네트임)
                        for net_name, pin_inst in comp_nets.items():
                            if net_name != dcdc_out_net and net_name in net_chain:
                                target_inductor = comp_inst
                                ic_side_pin = pin_inst
                                break
                if target_inductor:
                    break

            # 3. 위치 결정 및 반환
            if target_inductor and ic_side_pin:
                if self.logger: self.logger.log(f"[INFO] Found Power Inductor '{target_inductor.name}'. Placing VRM on IC-side pad (Net: {ic_side_pin.net_name}).", level=LogLevel.DETAIL2)
                pos_coord = ic_side_pin.position
                pos_layer = target_inductor.placement_layer
                neg_coord, neg_layer = self.find_nearest_gnd(pos_coord, gnd_net)
                
                # 💡 [추가] GND 좌표를 찾지 못했을 경우의 방어 코드
                if not neg_coord:
                    if self.logger: self.logger.log(f"[ERROR] GND Net '{gnd_net}' not found near Inductor '{target_inductor.name}'.", level=LogLevel.ERROR)
                    return None, None, None, None, None

                # Main.py에서 이 이름을 보고 인덕터를 DNI(삭제) 처리함
                src_name = f"Inductor_{target_inductor.name}" 
                return pos_coord, pos_layer, neg_coord, neg_layer, src_name
                
            else:
                if self.logger: self.logger.log(f"[INFO] No Power Inductor found. Placing VRM directly on '{dcdc_name}' Pin '{dcdc_pin}'.", level=LogLevel.DETAIL2)
                pos_coord = dcdc_pin_inst.position
                pos_layer = dcdc_comp.placement_layer
                neg_coord, neg_layer = self.find_nearest_gnd(pos_coord, gnd_net)
                
                # 💡 [추가] GND 좌표를 찾지 못했을 경우의 방어 코드
                if not neg_coord:
                    if self.logger: self.logger.log(f"[ERROR] GND Net '{gnd_net}' not found near DCDC '{dcdc_name}'.", level=LogLevel.ERROR)
                    return None, None, None, None, None

                src_name = f"DCDC_{dcdc_name}"
                return pos_coord, pos_layer, neg_coord, neg_layer, src_name

        except Exception as e:
            if self.logger: self.logger.log(f"[ERROR] Failed to prepare VRM connection: {e}", level=LogLevel.ERROR)
            return None, None, None, None, None  

    # ==========================================
    # SIwave Manipulation Methods
    # ==========================================
    def open_project(self, project_path):
        try:
            self.siw_app.open_project(str(project_path))
        except Exception as e:
            if self.logger: self.logger.log(f"Failed to open project: {e}", level=LogLevel.ERROR)
            raise

    def import_edb(self, edb_path):
        try:
            self.siw_app.import_edb(str(edb_path))
        except Exception as e:
            if self.logger: self.logger.log(f"Failed to import EDB: {e}", level=LogLevel.ERROR)
            raise

    def save_project_as(self, path):
        self.oproject.ScrSaveProjectAs(str(path))

    def export_edb(self, path):
        self.oproject.ScrExportEDB(str(path))

    def create_project(self, anf_file, cmp_file, stk_file, siw_path, edb_path):
        try:
            self.oSiwave.ImportAnfFile(str(anf_file))
            self.oproject.ScrImportComponentFile(str(cmp_file))
            self.oproject.ScrImportLayerStackupFile(str(stk_file))
            self.save_project_as(siw_path)
            self.export_edb(edb_path)
        except Exception as e:
            if self.logger: self.logger.log(f"SIwave Project Creation Failed: {e}", level=LogLevel.ERROR)
            raise

    def change_part_type(self, part_name, part_type):
        self.oproject.ScrChangePartType(part_name, part_type)

    def delete_circuit_element(self, comp_name):
        self.oproject.ScrDeleteCktElem(comp_name)

    def merge_connected_nets(self, net_list):
        self.oproject.ScrMergeConnectedNets(net_list)

    def setup_simulation(self, pmap_file, sws_file):
        try:
            if pmap_file:
                self.oproject.ScrImportPmap(str(pmap_file))
            self.oproject.ScrImportSIwaveSimulationOptions(str(sws_file))
        except Exception as e:
            if self.logger: self.logger.log(f"Setup Simulation Failed: {e}", level=LogLevel.ERROR)
            raise

    def place_voltage_source(self, name, pos, layer, neg_pos, neg_layer, res, mag, scale_factor=1000.0):
        try:
            self.oproject.ScrPlaceCircuitElement(
                name, 'VoltageSource', 5, 2,
                pos[0] * scale_factor, pos[1] * scale_factor, layer,
                2, neg_pos[0] * scale_factor, neg_pos[1] * scale_factor, neg_layer,
                0, 0, res, 0, mag, 0
            )
        except Exception as e:
            if self.logger: self.logger.log(f"Place Voltage Source Failed ({name}): {e}", level=LogLevel.ERROR)
            raise

    def place_current_source(self, name, pos, layer, neg_pos, neg_layer, res, mag, scale_factor=1000.0):
        try:
            self.oproject.ScrPlaceCircuitElement(
                name, 'CurrentSource', 4, 2,
                pos[0] * scale_factor, pos[1] * scale_factor, layer,
                2, neg_pos[0] * scale_factor, neg_pos[1] * scale_factor, neg_layer,
                0, 0, res, 0, mag, 0
            )
        except Exception as e:
            if self.logger: self.logger.log(f"Place Current Source Failed ({name}): {e}", level=LogLevel.ERROR)
            raise

    def export_layer_images(self, ref_siw_path, output_dir, gnd_net, gnd_color='0x004000'):
        try:
            with open(str(ref_siw_path), 'r') as f: lines = f.readlines()

            layer_configs = [
                ('_top', list(self.edb.stackup.signal_layers.keys())[0]),
                ('_btm', list(self.edb.stackup.signal_layers.keys())[-1])
            ]

            for suffix, target_layer in layer_configs:
                out_path = Path(ref_siw_path).with_stem(Path(ref_siw_path).stem + suffix)
                
                net_flag, layer_flag = False, False
                with open(out_path, 'w') as f:
                    for line in lines:
                        stripped = line.strip()
                        if stripped in ('B_LAYERS', 'E_LAYERS', 'B_NETS', 'E_NETS'):
                            layer_flag = (stripped == 'B_LAYERS')
                            net_flag = (stripped == 'B_NETS')
                            f.write(line)
                            continue

                        if line.startswith('VIEW_GRID') or line.startswith('VIEW_PIN_NAMES'):
                            f.write(f"{line.split()[0]} 0\n")
                            continue

                        line_data = line.split()
                        if layer_flag and len(line_data) > 15:
                            if line_data[2] == 'METAL':
                                line_data[7] = '0' 
                                line_data[11:16] = ['1'] * 5 if line_data[1][1:-1] == target_layer else ['0'] * 5
                            f.write(' '.join(line_data) + '\n')
                        elif net_flag and len(line_data) > 5:
                            if line_data[1] == gnd_net: line_data[4] = gnd_color
                            line_data[5] = '1' 
                            f.write(' '.join(line_data) + '\n')
                        else:
                            f.write(line)

                # 💡 [수정] 임시 객체 생성 시에도 로거를 상속받도록 수정
                temp_app = SIwave(version=self._version, logger=self.logger)
                temp_app.open_project(str(out_path))
                try:
                    temp_app.oSiwave.RestoreWindow()
                    temp_app.oproject.ScrFitAll()
                    img_file = Path(output_dir) / f"{suffix[1:]}.png"
                    temp_app.oproject.ScrSaveToPngFile(str(img_file))
                finally:
                    temp_app.quit_application()
                    
        except Exception as e:
            if self.logger: self.logger.log(f"Export Layer Images Failed: {e}", level=LogLevel.ERROR)