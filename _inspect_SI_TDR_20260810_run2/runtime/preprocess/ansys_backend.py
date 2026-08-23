from __future__ import annotations

import shutil
import subprocess
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from .bom import match_installed_bom_designator, parse_bom_designators
from .contracts import ReferencePreprocessError, ReferencePreprocessRequest


@dataclass(frozen=True)
class AcquiredAnfCmp:
    anf: Path
    cmp: Path
    evidence: dict[str, Any]


@dataclass(frozen=True)
class BuiltReference:
    reference_siw: Path
    reference_aedb: Path
    evidence: dict[str, Any]


def _exactly_one(paths: Sequence[Path], label: str) -> Path:
    candidates = sorted({path.resolve() for path in paths if path.is_file()}, key=str)
    if len(candidates) != 1:
        raise ReferencePreprocessError(
            f"expected exactly one {label}; found {len(candidates)}: "
            + ", ".join(str(path) for path in candidates)
        )
    return candidates[0]


def _safe_extract_zip(source: Path, destination: Path) -> list[str]:
    destination = destination.resolve()
    extracted: list[str] = []
    with zipfile.ZipFile(source, "r") as archive:
        for info in archive.infolist():
            target = (destination / info.filename).resolve()
            if target != destination and destination not in target.parents:
                raise ReferencePreprocessError(
                    f"Zuken archive member escapes the job workspace: {info.filename}"
                )
        archive.extractall(destination)
        extracted = [info.filename for info in archive.infolist() if not info.is_dir()]
    return extracted


class AnsysReferenceBackend:
    """Live Ansys/Zuken implementation behind the license-free contract layer.

    Imports are intentionally lazy.  Merely importing SI-TDR or validating a
    Config never checks out an Ansys license and never depends on PyEDB.
    """

    def __init__(
        self,
        *,
        command_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._command_runner = command_runner
        self._sleeper = sleeper

    @staticmethod
    def _siwave_operation(
        label: str, operation: Callable[..., Any], *args: Any
    ) -> Any:
        try:
            return operation(*args)
        except Exception as exc:
            raise ReferencePreprocessError(f"SIWave {label} failed: {exc}") from exc

    @staticmethod
    def _siwave_result_evidence(result: Any) -> Any:
        if result is None or isinstance(result, (bool, int, float, str)):
            return result
        if isinstance(result, (dict, list, tuple, set)):
            return {"type": type(result).__name__, "count": len(result)}
        summary = str(result)
        return {"type": type(result).__name__, "summary": summary[:200]}

    @staticmethod
    def _siwave_class():
        try:
            from pyedb.siwave import Siwave
        except ImportError as exc:
            raise ReferencePreprocessError(
                "PyEDB/SIWave runtime is required for live reference preprocessing"
            ) from exc
        return Siwave

    @staticmethod
    def _edb_class():
        try:
            from pyedb import Edb
        except ImportError as exc:
            raise ReferencePreprocessError(
                "PyEDB runtime is required for dcir_reference_v1 preprocessing"
            ) from exc
        return Edb

    def acquire_zuken(
        self,
        request: ReferencePreprocessRequest,
        attempt_dir: Path,
    ) -> AcquiredAnfCmp:
        if request.design is None or request.zuken_bin is None:
            raise ReferencePreprocessError("zuken_design request is incomplete")
        source_dir = attempt_dir / "zuken_source"
        source_dir.mkdir(parents=True, exist_ok=False)
        extracted: list[str] = []
        if request.design.suffix.casefold() == ".zip":
            extracted = _safe_extract_zip(request.design, source_dir)
        elif request.design.suffix.casefold() == ".pcb":
            shutil.copy2(request.design, source_dir / request.design.name)
        else:
            raise ReferencePreprocessError(
                "zuken_design currently requires an explicit .zip or .pcb input"
            )

        pcb = _exactly_one(list(source_dir.rglob("*.pcb")), "Zuken PCB file")
        zuken = request.settings.get("zuken") or {}
        if not isinstance(zuken, dict):
            raise ReferencePreprocessError("preprocessing.zuken must be an object")
        attempts = int(zuken.get("maxAttempts", 3))
        retry_delay = float(zuken.get("retryDelaySeconds", 120))
        if attempts < 1 or retry_delay < 0:
            raise ReferencePreprocessError(
                "preprocessing.zuken retry settings must be non-negative"
            )

        command_records: list[dict[str, Any]] = []
        evolve = request.zuken_bin / "DFevolv.cr5.exe"
        convert = request.zuken_bin / "DFdsgn2anf.exe"
        for executable in (evolve, convert):
            if not executable.is_file():
                raise ReferencePreprocessError(f"Zuken executable not found: {executable}")

        evolve_argv = [str(evolve), str(pcb.parent)]
        evolve_result = self._run_command(evolve_argv)
        command_records.append(self._command_record(evolve_argv, evolve_result, 1))
        if evolve_result.returncode != 0:
            raise ReferencePreprocessError(
                f"Zuken PCB-to-DSGN conversion failed: returncode={evolve_result.returncode}"
            )
        dsgn = pcb.with_suffix(".dsgn")
        if not dsgn.is_file():
            raise ReferencePreprocessError(f"Zuken DSGN output not found: {dsgn}")

        anf = dsgn.with_suffix(".anf")
        for attempt in range(1, attempts + 1):
            argv = [str(convert), "-r", str(dsgn), "-o", str(anf)]
            result = self._run_command(argv)
            command_records.append(self._command_record(argv, result, attempt))
            if result.returncode == 0 and anf.is_file():
                break
            if attempt < attempts:
                self._sleeper(retry_delay)
        else:
            raise ReferencePreprocessError(
                "Zuken DSGN-to-ANF conversion did not produce the configured ANF output"
            )
        cmp_path = _exactly_one(list(source_dir.rglob("*.cmp")), "Zuken CMP file")
        return AcquiredAnfCmp(
            anf=anf.resolve(),
            cmp=cmp_path,
            evidence={
                "sourceCopy": str(source_dir),
                "archiveMembers": extracted,
                "pcb": str(pcb),
                "dsgn": str(dsgn),
                "commands": command_records,
            },
        )

    def _run_command(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        return self._command_runner(argv, capture_output=True, text=True)

    @staticmethod
    def _command_record(
        argv: list[str],
        result: subprocess.CompletedProcess[str],
        attempt: int,
    ) -> dict[str, Any]:
        return {
            "argv": argv,
            "attempt": attempt,
            "returnCode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }

    def build_from_anf_cmp(
        self,
        request: ReferencePreprocessRequest,
        anf: Path,
        cmp_path: Path,
        attempt_dir: Path,
    ) -> BuiltReference:
        if request.aedt_version is None or request.reference_policy is None:
            raise ReferencePreprocessError("generated reference request is incomplete")
        Siwave = self._siwave_class()
        base_siw = attempt_dir / f"{request.output_name}__import.siw"
        base_aedb = attempt_dir / f"{request.output_name}__import.aedb"
        siwave = Siwave(specified_version=request.aedt_version)
        try:
            siwave.oSiwave.ImportAnfFile(str(anf))
            if not siwave.oproject.ScrImportComponentFile(str(cmp_path)):
                raise ReferencePreprocessError(f"SIWave CMP import failed: {cmp_path}")
            if not siwave.oproject.ScrSaveProjectAs(str(base_siw)):
                raise ReferencePreprocessError(f"SIWave base save failed: {base_siw}")
            siwave.oproject.ScrExportEDB(str(base_aedb))
        finally:
            siwave.quit_application()
        self._require_aedb(base_aedb)

        working_aedb = attempt_dir / f"{request.output_name}__working.aedb"
        stackup_evidence: dict[str, Any] | None = None
        if request.stackup is not None:
            from .stackup import apply_stk

            stackup_evidence = apply_stk(
                base_aedb,
                working_aedb,
                request.stackup,
                request.aedt_version,
            )
        else:
            shutil.copytree(base_aedb, working_aedb)

        transform_evidence: dict[str, Any] = {"policy": request.reference_policy}
        if request.reference_policy == "dcir_reference_v1":
            transform_evidence.update(self._apply_dcir_reference_policy(request, working_aedb))

        reference_siw = attempt_dir / f"{request.output_name}_ref.siw"
        reference_aedb = attempt_dir / f"{request.output_name}_ref.aedb"
        siwave_operations: dict[str, Any] | None = None
        siwave = Siwave(specified_version=request.aedt_version)
        try:
            siwave.import_edb(str(working_aedb))
            if request.reference_policy == "dcir_reference_v1":
                dc_short = request.settings["dcShort"]
                siwave_operations = {
                    "requestedPartTypeChanges": list(dc_short["shortedComp"]),
                    "requestedComponentDeletes": list(transform_evidence["deleteComponents"]),
                    "requestedNetMerges": dict(transform_evidence["shortCorrections"]),
                    "pmapPath": str(request.pmap) if request.pmap is not None else None,
                    "swsPath": str(request.sws) if request.sws is not None else None,
                    "pmapImportResult": None,
                    "swsImportResult": None,
                    "partTypeChangeResults": {},
                    "componentDeleteResults": {},
                    "netMergeResults": {},
                    "zeroResultOperations": [],
                }
                for part_name in dc_short["shortedComp"]:
                    result = self._siwave_operation(
                        f"part type change for {part_name}",
                        siwave.oproject.ScrChangePartType,
                        part_name,
                        "Capacitor",
                    )
                    siwave_operations["partTypeChangeResults"][part_name] = (
                        self._siwave_result_evidence(result)
                    )
                    if result is False or result == 0:
                        siwave_operations["zeroResultOperations"].append(
                            f"part_type:{part_name}"
                        )
                for component in transform_evidence["deleteComponents"]:
                    result = self._siwave_operation(
                        f"component delete for {component}",
                        siwave.oproject.ScrDeleteCktElem,
                        component,
                    )
                    siwave_operations["componentDeleteResults"][component] = (
                        self._siwave_result_evidence(result)
                    )
                    if result is False or result == 0:
                        siwave_operations["zeroResultOperations"].append(
                            f"component_delete:{component}"
                        )
                for primary, secondaries in transform_evidence["shortCorrections"].items():
                    merged_nets = [primary, *secondaries]
                    result = self._siwave_operation(
                        f"net merge for {primary}",
                        siwave.oproject.ScrMergeConnectedNets,
                        merged_nets,
                    )
                    siwave_operations["netMergeResults"][primary] = (
                        self._siwave_result_evidence(result)
                    )
                    if result is False or result == 0:
                        siwave_operations["zeroResultOperations"].append(
                            f"net_merge:{primary}"
                        )
                if request.pmap is not None:
                    result = self._siwave_operation(
                        "PMAP import",
                        siwave.oproject.ScrImportPmap,
                        str(request.pmap),
                    )
                    siwave_operations["pmapImportResult"] = (
                        self._siwave_result_evidence(result)
                    )
                    if result is False or result == 0:
                        siwave_operations["zeroResultOperations"].append("pmap_import")
                if request.sws is not None:
                    result = self._siwave_operation(
                        "SWS import",
                        siwave.oproject.ScrImportSIwaveSimulationOptions,
                        str(request.sws),
                    )
                    siwave_operations["swsImportResult"] = (
                        self._siwave_result_evidence(result)
                    )
                    if result is False or result == 0:
                        siwave_operations["zeroResultOperations"].append("sws_import")
            if not siwave.oproject.ScrSaveProjectAs(str(reference_siw)):
                raise ReferencePreprocessError(
                    f"SIWave reference save failed: {reference_siw}"
                )
            siwave.oproject.ScrExportEDB(str(reference_aedb))
        finally:
            siwave.quit_application()
        if siwave_operations is not None:
            transform_evidence["siwaveOperations"] = siwave_operations
        self._require_pair(reference_siw, reference_aedb)
        return BuiltReference(
            reference_siw=reference_siw.resolve(),
            reference_aedb=reference_aedb.resolve(),
            evidence={
                "sourceAnf": str(anf.resolve()),
                "sourceCmp": str(cmp_path.resolve()),
                "baseSiw": str(base_siw.resolve()),
                "baseAedb": str(base_aedb.resolve()),
                "workingAedb": str(working_aedb.resolve()),
                "stackup": stackup_evidence,
                "transform": transform_evidence,
            },
        )

    def _apply_dcir_reference_policy(
        self,
        request: ReferencePreprocessRequest,
        working_aedb: Path,
    ) -> dict[str, Any]:
        if request.bom is None or request.aedt_version is None:
            raise ReferencePreprocessError("dcir_reference_v1 inputs are incomplete")
        bom_config = request.settings.get("BOM") or {}
        if not isinstance(bom_config, dict):
            raise ReferencePreprocessError("preprocessing.BOM must be an object")
        configured_columns = bom_config.get("designatorColumns") or []
        if not isinstance(configured_columns, list):
            raise ReferencePreprocessError(
                "preprocessing.BOM.designatorColumns must be an array"
            )
        installed = parse_bom_designators(
            request.bom,
            configured_columns=configured_columns,
        )
        expanded_policy = bom_config.get("expandedDesignatorPolicy", "exact_only")
        dc_short = request.settings["dcShort"]
        excluded_nets = list(dc_short["excludeNet"])
        short_defs = set(dc_short["shortedComp"])
        exclude_prefixes = tuple(dc_short["excludePrefixes"])
        delete_types = set(dc_short["deleteCompTypes"])
        preserve_types = set(dc_short["preserveCompTypes"])
        short_key = dc_short["shortKey"]

        Edb = self._edb_class()
        edb = Edb(edbpath=str(working_aedb), edbversion=request.aedt_version)
        disabled: list[str] = []
        deleted_excluded_nets: list[str] = []
        unresolved_excluded_nets: list[str] = []
        preserved: list[str] = []
        expanded_matches: dict[str, str] = {}
        delete_components: set[str] = set()
        short_corrections: dict[str, list[str]] = {}
        try:
            nets = edb.nets.nets
            for name in excluded_nets:
                net = nets.get(name)
                if net is None:
                    unresolved_excluded_nets.append(name)
                    continue
                result = net.delete()
                if result is False:
                    raise ReferencePreprocessError(
                        f"failed to delete configured excluded net: {name}"
                    )
                deleted_excluded_nets.append(name)

            components = edb._components.components
            for name, component in components.items():
                installed_match = match_installed_bom_designator(
                    name,
                    installed,
                    expanded_policy,
                )
                if installed_match is not None:
                    if installed_match != name:
                        expanded_matches[name] = installed_match
                    continue
                if not name.startswith(exclude_prefixes) and component.component_def in short_defs:
                    continue
                component_type = str(component.type)
                if component_type in preserve_types:
                    preserved.append(name)
                elif component_type in delete_types:
                    delete_components.add(name)
                else:
                    component.enabled = False
                    disabled.append(name)

            for name, component in components.items():
                if name.startswith(exclude_prefixes) or component.component_def not in short_defs:
                    continue
                nets = list(edb.nets.nets_by_components[name])
                if len(nets) != 2:
                    continue
                net1, net2 = (str(nets[0]), str(nets[1]))
                primary, secondary = (
                    (net2, net1)
                    if short_key in net1 or (short_key not in net2 and len(net1) > len(net2))
                    else (net1, net2)
                )
                short_corrections.setdefault(primary, []).append(secondary)
                delete_components.add(name)
            edb.save()
        finally:
            try:
                edb.close_edb()
            except AttributeError:
                edb.close()
        return {
            "requestedExcludedNets": excluded_nets,
            "deletedExcludedNets": deleted_excluded_nets,
            "unresolvedExcludedNets": unresolved_excluded_nets,
            "installedBomDesignatorCount": len(installed),
            "bomExpandedDesignatorPolicy": expanded_policy,
            "expandedBomDesignatorMatches": dict(sorted(expanded_matches.items())),
            "disabledComponents": sorted(disabled),
            "preservedComponents": sorted(preserved),
            "deleteComponents": sorted(delete_components),
            "shortCorrections": {
                key: sorted(set(values)) for key, values in sorted(short_corrections.items())
            },
            "excludedDcirSteps": [
                "spec_case_trace",
                "target_net_sanitize",
                "spec_based_zero_ohm_install",
                "case_siw_generation",
                "dcir_solve",
                "dcir_post",
            ],
        }

    def export_aedb_from_siw(
        self,
        request: ReferencePreprocessRequest,
        attempt_dir: Path,
    ) -> BuiltReference:
        if request.reference_siw is None or request.aedt_version is None:
            raise ReferencePreprocessError("reference_siw export request is incomplete")
        Siwave = self._siwave_class()
        reference_siw = attempt_dir / request.reference_siw.name
        reference_aedb = attempt_dir / f"{request.output_name}_ref.aedb"
        shutil.copy2(request.reference_siw, reference_siw)
        siwave = Siwave(specified_version=request.aedt_version)
        try:
            if not siwave.open_project(str(reference_siw)):
                raise ReferencePreprocessError(f"SIWave open failed: {reference_siw}")
            result = siwave.oproject.ScrExportEDB(str(reference_aedb))
        finally:
            siwave.quit_application()
        self._require_pair(reference_siw, reference_aedb)
        return BuiltReference(
            reference_siw=reference_siw.resolve(),
            reference_aedb=reference_aedb.resolve(),
            evidence={
                "sourceSiw": str(request.reference_siw.resolve()),
                "protectedCopy": str(reference_siw.resolve()),
                "scrExportEdbResult": str(result),
            },
        )

    @staticmethod
    def _require_aedb(path: Path) -> None:
        if not path.is_dir() or not (path / "edb.def").is_file():
            raise ReferencePreprocessError(f"AEDB output is incomplete: {path}")

    @classmethod
    def _require_pair(cls, siw: Path, aedb: Path) -> None:
        if not siw.is_file():
            raise ReferencePreprocessError(f"SIW output not found: {siw}")
        cls._require_aedb(aedb)
