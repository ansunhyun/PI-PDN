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
from core.database import PDNSessionException, ErrorCode
from core.logger import LogLevel  

class _NoOpSyzSetup:
    """Fallback setup handle when SYZ setup APIs are unavailable."""

    def __init__(self, logger=None, setup_name=""):
        self.logger = logger
        self.setup_name = setup_name

    def add_sweep(
        self,
        name,
        start_freq,
        stop_freq,
        count,
        freq_sweep_type="kDecadeCount",
        sweep_type="Interpolating",
    ):
        if self.logger:
            self.logger.log(
                f"[WARNING] SYZ sweep skipped (API unavailable): {self.setup_name}/{name} "
                f"{start_freq}->{stop_freq}, count={count}, type={freq_sweep_type}, mode={sweep_type}",
                level=LogLevel.WARNING,
            )
        return False

class SIwave:
    """Provides the SIwave application interface for general automation.
    Uses Composition to manage EDB and SIwave COM objects safely.
    """

    def __init__(self, version="2025.1", logger=None):
        self._version = version
        self.logger = logger
        self.edb = None
        self._edb_path = None
        self._siw_app = None
        self._sparam_com_unavailable = False
        self._sparam_cap_logged = False
        # Keep assignment logs concise by default (summary is emitted by caller).
        self._log_each_sparam_assignment = os.environ.get("PDN_LOG_SPARAM_EACH", "").strip().lower() in {"1", "true", "y", "yes", "on"}
        self._edb_open_retries = 3
        self._edb_retry_delay_sec = 10

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
        return getattr(self.siw_app, "oproject", None)

    @property
    def oSiwave(self):
        return getattr(self.siw_app, "oSiwave", None)

    @property
    def oeditor(self):
        # Best-effort editor handle for native port APIs.
        prj = self.oproject
        if prj is not None:
            for editor_name in ("Layout", "3D Layout", "SIwave", "SchematicEditor"):
                try:
                    get_editor = getattr(prj, "GetActiveEditor", None)
                    if get_editor:
                        editor = get_editor(editor_name)
                        if editor is not None:
                            return editor
                except Exception:
                    continue
        return getattr(self.siw_app, "oeditor", None)

    def _com_owners(self):
        """Return available COM owners safely for version-agnostic fallback."""
        owners = []
        try:
            prj = self.oproject
            if prj is not None:
                owners.append(prj)
        except Exception:
            pass
        try:
            osiw = self.oSiwave
            if osiw is not None:
                owners.append(osiw)
        except Exception:
            pass
        return owners

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
            target_path = str(Path(cad_file).resolve())
            if self.edb and self._edb_path and self._edb_path.lower() == target_path.lower():
                if self.logger:
                    self.logger.log(f"Reusing opened EDB session: {target_path}", level=LogLevel.DETAIL2)
                return
            # Prevent duplicate-open/file-lock issues by closing prior EDB session first.
            if self.edb:
                self.close_edb()
            last_exc = None
            for attempt in range(1, self._edb_open_retries + 1):
                try:
                    self.edb = Edb(str(cad_file), edbversion=self._version)
                    if self.edb.db.IsNull():
                        raise ValueError("EDB database is null.")
                    self._edb_path = target_path
                    if self.logger:
                        self.logger.log(f"EDB initialized with CAD file: {cad_file}", level=LogLevel.DETAIL2)
                    return
                except Exception as e:
                    last_exc = e
                    msg = str(e)
                    is_license_error = ("acquire license" in msg.lower()) or ("license" in msg.lower() and "open" in msg.lower())
                    if not is_license_error or attempt >= self._edb_open_retries:
                        raise
                    if self.logger:
                        self.logger.log(
                            f"[WARNING] EDB license unavailable (attempt {attempt}/{self._edb_open_retries}). "
                            f"Retry after {self._edb_retry_delay_sec}s.",
                            level=LogLevel.WARNING,
                        )
                    try:
                        self.close_edb()
                    except Exception:
                        pass
                    time.sleep(self._edb_retry_delay_sec)
            if last_exc:
                raise last_exc
        except Exception as e:
            if self.logger:
                self.logger.log(f"EDB Open Error Details: {e}", level=LogLevel.ERROR)
            raise PDNSessionException(ErrorCode.EDB_DATABASE_OPEN_FAIL, cad_file)

    def close_edb(self):
        try:
            if self.edb:
                self.edb.close_edb()
                self.edb = None
                self._edb_path = None
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

    def create_rlc_component(self, pins, comp_name, part_name, r_value=0, l_value=None, c_value=None):
        try:
            kwargs = {
                "is_rlc": True,
                "component_name": comp_name,
                "component_part_name": part_name,
                "r_value": r_value,
            }
            if l_value is not None:
                kwargs["l_value"] = l_value
            if c_value is not None:
                kwargs["c_value"] = c_value
            try:
                self.edb._components.create(pins, **kwargs)
            except TypeError:
                # Backward compatibility for API variants that don't accept l/c kwargs.
                self.edb._components.create(
                    pins,
                    is_rlc=True,
                    component_name=comp_name,
                    component_part_name=part_name,
                    r_value=r_value,
                )
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
                        dist = (pin.position[0] - ref_coord[0]) ** 2 + (pin.position[1] - ref_coord[1]) ** 2
                        if dist < min_dist:
                            min_dist, best_coord, best_layer = dist, pin.position, comp.placement_layer
        except Exception as e:
            if self.logger: self.logger.log(f"Find Nearest GND Error: {e}", level=LogLevel.WARNING)
        return best_coord, best_layer
        
    def prepare_vrm_connection(self, target_net, source_name, source_pin, gnd_net, net_chain, inductor_prefix='L', bulk_inductor_list=None):
        """
        Find VRM or Port placement nodes for target net. (AC PDN 용도로도 사용됨)
        1) Prefer IC-side pad of a series power inductor connected to source output.
        2) If no valid inductor is found, use the source output pin directly.
        """
        try:
            source_comp = self.edb._components.components.get(source_name)
            if not source_comp:
                if self.logger: self.logger.log(f"[WARNING] Source component '{source_name}' not found.", level=LogLevel.WARNING)
                return None, None, None, None, None

            # 1) Resolve source output pin.
            source_pin_inst = source_comp.pins.get(source_pin)
            if not source_pin_inst:
                # Fallback: if pin name mismatches, auto-find pin by net_chain membership.
                for p_name, p_inst in source_comp.pins.items():
                    if p_inst.net_name in net_chain:
                        source_pin_inst = p_inst
                        source_pin = p_name
                        break
            
            if not source_pin_inst:
                if self.logger: self.logger.log(f"[WARNING] Valid output pin for '{source_name}' not found in net_chain.", level=LogLevel.WARNING)
                return None, None, None, None, None

            source_out_net = source_pin_inst.net_name
            
            # 2) Find a series inductor between source output net and target net-chain.
            target_inductor = None
            ic_side_pin = None
            
            for comp_name, comp_inst in self.edb._components.components.items():
                # Accept inductor/bead prefixes.
                if comp_name.startswith(inductor_prefix) or comp_name.startswith(('B', 'FB')):
                    
                    # If bulk_inductor_list is provided, skip components outside the list.
                    if bulk_inductor_list is not None and comp_name not in bulk_inductor_list:
                        continue

                    comp_nets = {p.net_name: p for p in comp_inst.pins.values() if p.net_name}
                    
                    # Check whether this component is connected to source output net.
                    if source_out_net in comp_nets:
                        # Check if the opposite net belongs to the traced net-chain (IC direction).
                        for net_name, pin_inst in comp_nets.items():
                            if net_name != source_out_net and net_name in net_chain:
                                target_inductor = comp_inst
                                ic_side_pin = pin_inst
                                break
                if target_inductor:
                    break

            # 3) Finalize placement coordinates and return.
            if target_inductor and ic_side_pin:
                if self.logger: self.logger.log(f"[INFO] Found Power Inductor '{target_inductor.name}'. Placing Port/VRM on IC-side pad (Net: {ic_side_pin.net_name}).", level=LogLevel.DETAIL2)
                pos_coord = ic_side_pin.position
                pos_layer = target_inductor.placement_layer
                neg_coord, neg_layer = self.find_nearest_gnd(pos_coord, gnd_net)
                
                # Defensive check: nearest GND may be unavailable.
                if not neg_coord:
                    if self.logger: self.logger.log(f"[ERROR] GND Net '{gnd_net}' not found near Inductor '{target_inductor.name}'.", level=LogLevel.ERROR)
                    return None, None, None, None, None

                # Main flow can deactivate this inductor by source name.
                src_name = f"Inductor_{target_inductor.name}" 
                return pos_coord, pos_layer, neg_coord, neg_layer, src_name
                
            else:
                if self.logger: self.logger.log(f"[INFO] No Power Inductor found. Placing Port/VRM directly on '{source_name}' Pin '{source_pin}'.", level=LogLevel.DETAIL2)
                pos_coord = source_pin_inst.position
                pos_layer = source_comp.placement_layer
                neg_coord, neg_layer = self.find_nearest_gnd(pos_coord, gnd_net)
                
                # Defensive check: nearest GND may be unavailable.
                if not neg_coord:
                    if self.logger: self.logger.log(f"[ERROR] GND Net '{gnd_net}' not found near source '{source_name}'.", level=LogLevel.ERROR)
                    return None, None, None, None, None

                src_name = f"SOURCE_{source_name}"
                return pos_coord, pos_layer, neg_coord, neg_layer, src_name

        except Exception as e:
            if self.logger: self.logger.log(f"[ERROR] Failed to prepare VRM/Port connection: {e}", level=LogLevel.ERROR)
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

    def import_layer_stackup(self, stk_file):
        try:
            if not stk_file:
                return False
            self.oproject.ScrImportLayerStackupFile(str(stk_file))
            return True
        except Exception as e:
            if self.logger:
                self.logger.log(f"Layer stackup import failed: {e}", level=LogLevel.WARNING)
            return False

    def create_project(self, anf_file, cmp_file, stk_file, siw_path, edb_path):
        try:
            imported = False
            for owner in self._com_owners():
                method = getattr(owner, "ImportAnfFile", None)
                if method:
                    method(str(anf_file))
                    imported = True
                    break
            if not imported:
                raise RuntimeError("ImportAnfFile API is unavailable in current SIwave backend.")

            if self.oproject is None:
                raise RuntimeError("oproject API is unavailable in current SIwave backend.")
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

    def setup_simulation(self, pmap_file, sws_file, sfsdf_file=None):
        try:
            if self.oproject is None:
                raise RuntimeError("oproject API is unavailable in current SIwave backend.")
            if pmap_file:
                self.oproject.ScrImportPmap(str(pmap_file))
            if sfsdf_file:
                imported = False
                for owner in self._com_owners():
                    for method_name in (
                        "ScrImportSIwaveFrequencySweepDefinitionFile",
                        "ScrImportSIwaveFrequencySweepDefinition",
                        "ScrImportFrequencySweepDefinition",
                        "ScrImportSFSDF",
                    ):
                        method = getattr(owner, method_name, None)
                        if not method:
                            continue
                        try:
                            method(str(sfsdf_file))
                            imported = True
                            break
                        except Exception:
                            continue
                    if imported:
                        break
                if self.logger:
                    if imported:
                        self.logger.log(f"Imported Z-parameter frequency setup: {sfsdf_file}", level=LogLevel.DETAIL2)
                    else:
                        self.logger.log(f"[WARNING] Could not import SFSDF with available API methods: {sfsdf_file}", level=LogLevel.WARNING)
            self.oproject.ScrImportSIwaveSimulationOptions(str(sws_file))
        except Exception as e:
            if self.logger: self.logger.log(f"Setup Simulation Failed: {e}", level=LogLevel.ERROR)
            raise

    def create_syz_setup(self, name):
        """
        Create an SYZ setup with best-effort API compatibility.
        Returns a setup-like object that supports add_sweep().
        """
        # 1) Native API path (preferred)
        native = getattr(self.siw_app, "create_syz_setup", None)
        if native:
            try:
                return native(name=name)
            except TypeError:
                return native(name)
            except Exception as e:
                if self.logger:
                    self.logger.log(f"[WARNING] Native create_syz_setup failed: {e}", level=LogLevel.WARNING)

        # 2) Generic create_setup path if available in current backend
        create_setup = getattr(self.siw_app, "create_setup", None)
        if create_setup:
            try:
                setup_type = None
                if hasattr(self.siw_app, "SETUPS") and hasattr(self.siw_app.SETUPS, "SIW_SYZ"):
                    setup_type = self.siw_app.SETUPS.SIW_SYZ
                if setup_type is not None:
                    return create_setup(name=name, setup_type=setup_type)
                return create_setup(name=name)
            except Exception as e:
                if self.logger:
                    self.logger.log(f"[WARNING] Generic create_setup fallback failed: {e}", level=LogLevel.WARNING)

        # 3) COM-script setup creation best effort (sweep can still be skipped safely)
        for owner in self._com_owners():
            for method_name in (
                "ScrCreateSYZSetup",
                "ScrAddSYZSetup",
                "ScrCreateSYZSimulation",
            ):
                method = getattr(owner, method_name, None)
                if not method:
                    continue
                for args in ((str(name),), (str(name), "Interpolating"), (str(name), 1)):
                    try:
                        method(*args)
                        if self.logger:
                            self.logger.log(
                                f"Created SYZ setup via COM method {method_name}: {name}",
                                level=LogLevel.DETAIL2,
                            )
                        return _NoOpSyzSetup(logger=self.logger, setup_name=name)
                    except Exception:
                        continue

        if self.logger:
            self.logger.log(
                f"[WARNING] No SYZ setup creation API available. Returning no-op setup handle: {name}",
                level=LogLevel.WARNING,
            )
        return _NoOpSyzSetup(logger=self.logger, setup_name=name)

    def assign_sparameter_model(self, component_name, s2p_path, port_count=2, pos_pin=None, neg_pin=None):
        """
        Best-effort S-parameter model assignment.
        Returns True when at least one backend API call reports success.
        """
        if not component_name or not s2p_path:
            return False

        model_name = Path(s2p_path).stem

        # 1) Try EDB-level model assignment methods (pyedb primary path)
        try:
            if self.edb:
                comp = self.edb._components.components.get(component_name)
                if comp:
                    if self.logger and not self._sparam_cap_logged:
                        supported = [m for m in (
                            "assign_s_param_model",
                            "assign_s_parameter_model",
                            "assign_touchstone_model",
                            "set_touchstone_model",
                            "use_s_parameter_model",
                        ) if hasattr(comp, m)]
                        self.logger.log(
                            f"[SParam] EDB component API capabilities: {supported if supported else 'none'}",
                            level=LogLevel.DETAIL1,
                        )
                        self._sparam_cap_logged = True

                    # pyedb 0.50.x canonical API
                    try:
                        if hasattr(comp, "assign_s_param_model"):
                            comp.assign_s_param_model(str(s2p_path), model_name=model_name, reference_net=None)
                            if self.logger and self._log_each_sparam_assignment:
                                self.logger.log(
                                    f"[SParam] Assigned via EDB assign_s_param_model: {component_name} -> {s2p_path}",
                                    level=LogLevel.DETAIL2,
                                )
                            return True
                    except Exception:
                        pass

                    # Try component-definition assignment path as fallback.
                    try:
                        part_name = getattr(comp, "part_name", None)
                        definitions = getattr(self.edb._components, "definitions", {})
                        comp_def = definitions.get(part_name) if part_name and isinstance(definitions, dict) else None
                        if comp_def and hasattr(comp_def, "assign_s_param_model"):
                            comp_def.assign_s_param_model(str(s2p_path), model_name=model_name, reference_net=None)
                            if self.logger and self._log_each_sparam_assignment:
                                self.logger.log(
                                    f"[SParam] Assigned via EDB component_def.assign_s_param_model: {component_name}({part_name}) -> {s2p_path}",
                                    level=LogLevel.DETAIL2,
                                )
                            return True
                    except Exception:
                        pass

                    # Legacy/variant API names.
                    for method_name in (
                        "assign_s_param_model",
                        "assign_s_parameter_model",
                        "assign_touchstone_model",
                        "set_touchstone_model",
                        "use_s_parameter_model",
                    ):
                        method = getattr(comp, method_name, None)
                        if not method:
                            continue
                        try:
                            # Try most explicit signature first, then simple variants.
                            try:
                                method(str(s2p_path), int(port_count), str(pos_pin or ""), str(neg_pin or ""))
                            except Exception:
                                try:
                                    method(str(s2p_path), model_name)
                                except Exception:
                                    method(str(s2p_path))
                            if self.logger and self._log_each_sparam_assignment:
                                self.logger.log(
                                    f"[SParam] Assigned via EDB {method_name}: {component_name} -> {s2p_path}",
                                    level=LogLevel.DETAIL2,
                                )
                            return True
                        except Exception:
                            continue
        except Exception:
            pass

        # 2) Try SIwave COM script methods (API varies by version)
        owners = []
        if self._sparam_com_unavailable:
            owners = []
        else:
            try:
                owners = self._com_owners()
                if not owners:
                    self._sparam_com_unavailable = True
                    if self.logger:
                        self.logger.log(
                            "[SParam][WARNING] SIwave COM owners unavailable. Disable COM fallback in this run.",
                            level=LogLevel.WARNING,
                        )
            except Exception as e:
                self._sparam_com_unavailable = True
                if self.logger:
                    self.logger.log(
                        f"[SParam][WARNING] SIwave COM object unavailable. Disable COM fallback in this run: {e}",
                        level=LogLevel.WARNING,
                    )
                owners = []

        for owner in owners:
            for method_name in (
                "ScrAssignSParameterModel",
                "ScrAssignSParamModel",
                "ScrAssignNPortModel",
                "ScrAssignSpiceModel",
            ):
                method = getattr(owner, method_name, None)
                if not method:
                    continue
                for args in (
                    (str(component_name), str(s2p_path), int(port_count), str(pos_pin or ""), str(neg_pin or "")),
                    (str(component_name), str(s2p_path), int(port_count)),
                    (str(component_name), str(s2p_path)),
                ):
                    try:
                        method(*args)
                        if self.logger and self._log_each_sparam_assignment:
                            self.logger.log(
                                f"[SParam] Assigned via COM {method_name}: {component_name} -> {s2p_path}",
                                level=LogLevel.DETAIL2,
                            )
                        return True
                    except Exception:
                        continue

        if self.logger:
            self.logger.log(
                f"[SParam][WARNING] Assignment API not available or failed: {component_name} -> {s2p_path}",
                level=LogLevel.WARNING,
            )
        return False

    def place_circuit_port(
        self,
        port_name=None,
        pos_node=None,
        pos_layer=None,
        neg_node=None,
        neg_layer=None,
        impedance=0.1,
        name=None,
        pos=None,
        layer=None,
        neg_pos=None,
        scale_factor=1000.0,
    ):
        """Create a real SIwave circuit port using editor API with COM fallback."""
        # Backward compatibility for existing call sites.
        port_name = port_name or name
        pos_node = pos_node if pos_node is not None else pos
        pos_layer = pos_layer or layer
        neg_node = neg_node if neg_node is not None else neg_pos

        if not port_name:
            raise ValueError("port_name is required")
        if pos_node is None or neg_node is None:
            raise ValueError("pos/neg node is required")

        # 1) Native editor API path requested by user.
        try:
            editor = self.oeditor
            if editor and hasattr(editor, "CreateCircuitPort"):
                editor.CreateCircuitPort(
                    [
                        "NAME:CircuitPortData",
                        "PortName:=", str(port_name),
                        "PosNode:=", pos_node,
                        "PosLayer:=", pos_layer,
                        "NegNode:=", neg_node,
                        "NegLayer:=", neg_layer,
                        "Impedance:=", str(float(impedance)),
                    ]
                )
                if self.logger:
                    self.logger.log(f"Placed Circuit Port (CreateCircuitPort): {port_name} ({impedance} ohm)", level=LogLevel.DETAIL2)
                return True
        except Exception as e:
            if self.logger:
                self.logger.log(f"[WARNING] CreateCircuitPort path failed ({port_name}): {e}", level=LogLevel.WARNING)

        # 2) Legacy COM script fallback with coordinate-based placement.
        try:
            if isinstance(pos_node, (tuple, list)) and isinstance(neg_node, (tuple, list)):
                self.oproject.ScrPlaceCircuitElement(
                    str(port_name), 'Port', 5, 2,
                    float(pos_node[0]) * scale_factor, float(pos_node[1]) * scale_factor, pos_layer,
                    2, float(neg_node[0]) * scale_factor, float(neg_node[1]) * scale_factor, neg_layer,
                    0, 0, float(impedance), 0, 0, 0
                )
                if self.logger:
                    self.logger.log(f"Placed Circuit Port (ScrPlaceCircuitElement): {port_name} ({impedance} ohm)", level=LogLevel.DETAIL2)
                return True
            raise RuntimeError("ScrPlaceCircuitElement requires coordinate tuple/list nodes.")
        except Exception as e:
            if self.logger:
                self.logger.log(f"Place Circuit Port Failed ({port_name}): {e}", level=LogLevel.ERROR)
            return False

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

                # Keep logger attached while using a temporary SIwave object.
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
