"""Normalize a preserved Heaven request into the SI-TDR reference contract."""

from __future__ import annotations

import hashlib
import re
import zipfile
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, Mapping

try:
    from ..admin_config import derive_design_input_type, validate_administrator_config
except ImportError:  # Support top-level preprocess import from packaged/direct main.py.
    from admin_config import (  # type: ignore[no-redef]
        derive_design_input_type,
        validate_administrator_config,
    )
from .contracts import (
    ReferencePreprocessError,
    ReferencePreprocessRequest,
    ReferencePreprocessResult,
)
from .reference_preprocessor import (
    ReferencePreprocessBackend,
    run_reference_preprocess_request,
)

if TYPE_CHECKING:
    from ..channel.analysis_options import ResolvedAnalysisOptionCatalog
    from ..job_inputs import JobInputResolution


EXTERNAL_PLAN_SCHEMA = "si-tdr-external-reference-preprocess-plan/1"
# 유도된 designInputType이 곧 전처리 갈래 이름이다.
CUSTOMER_BRANCHES = {"zuken_design": "zuken_design", "anf_cmp": "anf_cmp"}


@dataclass(frozen=True)
class AnfCmpInput:
    source_kind: str
    anf_path: Path | None = None
    cmp_path: Path | None = None
    anf_member: str | None = None
    cmp_member: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "sourceKind": self.source_kind,
            "anfPath": str(self.anf_path) if self.anf_path is not None else None,
            "cmpPath": str(self.cmp_path) if self.cmp_path is not None else None,
            "anfMember": self.anf_member,
            "cmpMember": self.cmp_member,
        }


@dataclass(frozen=True)
class ExternalReferencePreprocessPlan:
    administrator_config_path: Path
    request_path: Path
    job_root: Path
    work_dir: Path
    design_input_type: str
    branch: str
    aedt_version: str
    reference_policy: str
    output_name: str
    output_name_source: str
    cad_file: Path
    stackup: Path
    bom: Path
    syz_profile_id: str
    syz_profile_source: str
    sws: Path
    sws_sha256: str
    sfsdf: Path
    sfsdf_sha256: str
    dc_short: dict[str, Any]
    bom_settings: dict[str, Any]
    is_zuken: bool
    zuken_bin: Path
    zuken_exports: dict[str, bool]
    anf_cmp: AnfCmpInput | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": EXTERNAL_PLAN_SCHEMA,
            "requestPath": str(self.request_path),
            "jobRoot": str(self.job_root),
            "administratorConfigPath": str(self.administrator_config_path),
            "designInputType": self.design_input_type,
            "preprocessingBranch": self.branch,
            "branchSource": "administrator Config designInputType",
            "aedtVersion": self.aedt_version,
            "workDir": str(self.work_dir),
            "referencePolicy": self.reference_policy,
            "outputName": self.output_name,
            "outputNameSource": self.output_name_source,
            "inputs": {
                "cadFile": str(self.cad_file),
                "stackup": str(self.stackup),
                "bom": str(self.bom),
                "anfCmp": self.anf_cmp.to_dict() if self.anf_cmp is not None else None,
            },
            "selectedSyzProfile": {
                "profileId": self.syz_profile_id,
                "selectionSource": self.syz_profile_source,
                "sws": {
                    "resolvedPath": str(self.sws),
                    "sha256": self.sws_sha256,
                },
                "sfsdf": {
                    "resolvedPath": str(self.sfsdf),
                    "sha256": self.sfsdf_sha256,
                    "applicationStage": "FB-07",
                },
            },
            "dcShort": deepcopy(self.dc_short),
            "zuken": {
                "isZuken": self.is_zuken,
                "DF_path": str(self.zuken_bin),
                **self.zuken_exports,
                "runtimeExecutableCheck": "required when preprocessing executes",
            },
            "expectedReferencePair": {
                "siw": f"{self.output_name}_ref.siw",
                "aedb": f"{self.output_name}_ref.aedb",
            },
        }


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _contained_file(path: Path, *, job_root: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ReferencePreprocessError(f"{label} cannot be resolved: {path}") from exc
    if job_root != resolved and job_root not in resolved.parents:
        raise ReferencePreprocessError(f"{label} resolves outside the Job root: {path}")
    if not resolved.is_file():
        raise ReferencePreprocessError(f"{label} is not a file: {path}")
    return resolved


def _safe_archive_name(raw_name: str) -> str:
    if "\\" in raw_name:
        raise ReferencePreprocessError(
            f"ANF/CMP archive member escapes or is not portable: {raw_name}"
        )
    normalized = raw_name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or (path.parts and ":" in path.parts[0])
    ):
        raise ReferencePreprocessError(
            f"ANF/CMP archive member escapes or is not portable: {raw_name}"
        )
    return path.as_posix()


def _inspect_anf_cmp_archive(cad_file: Path) -> AnfCmpInput:
    try:
        with zipfile.ZipFile(cad_file, "r") as archive:
            names: list[str] = []
            folded: dict[str, str] = {}
            for info in archive.infolist():
                if info.is_dir():
                    continue
                name = _safe_archive_name(info.filename)
                duplicate = folded.get(name.casefold())
                if duplicate is not None:
                    raise ReferencePreprocessError(
                        "ANF/CMP archive has case-insensitive duplicate members: "
                        f"{duplicate!r}, {name!r}"
                    )
                folded[name.casefold()] = name
                names.append(name)
    except (OSError, zipfile.BadZipFile) as exc:
        raise ReferencePreprocessError(
            f"designInputType='anf_cmp' requires a readable ANF/CMP ZIP: {cad_file}"
        ) from exc

    anf_members = [name for name in names if Path(name).suffix.casefold() == ".anf"]
    cmp_members = [name for name in names if Path(name).suffix.casefold() == ".cmp"]
    if len(anf_members) != 1 or len(cmp_members) != 1:
        raise ReferencePreprocessError(
            "ANF/CMP ZIP must contain exactly one .anf and one .cmp; "
            f"found anf={anf_members}, cmp={cmp_members}"
        )
    anf_member, cmp_member = anf_members[0], cmp_members[0]
    if Path(anf_member).stem.casefold() != Path(cmp_member).stem.casefold():
        raise ReferencePreprocessError(
            "ANF/CMP ZIP pair must use the same base name; "
            f"found {anf_member!r} and {cmp_member!r}"
        )
    return AnfCmpInput(
        source_kind="zip",
        anf_member=anf_member,
        cmp_member=cmp_member,
    )


def _inspect_direct_anf(cad_file: Path, *, job_root: Path) -> AnfCmpInput:
    candidates = sorted(
        (
            item
            for item in cad_file.parent.iterdir()
            if item.is_file()
            and item.suffix.casefold() == ".cmp"
            and item.stem.casefold() == cad_file.stem.casefold()
        ),
        key=lambda item: item.name,
    )
    if len(candidates) != 1:
        raise ReferencePreprocessError(
            "direct ANF input requires exactly one same-base CMP beside it; "
            f"ANF={cad_file}, CMP candidates={[str(path) for path in candidates]}"
        )
    cmp_path = _contained_file(
        candidates[0],
        job_root=job_root,
        label="external ANF/CMP CMP",
    )
    return AnfCmpInput(
        source_kind="direct",
        anf_path=cad_file,
        cmp_path=cmp_path,
    )


def _inspect_anf_cmp(cad_file: Path, *, job_root: Path) -> AnfCmpInput:
    suffix = cad_file.suffix.casefold()
    if suffix == ".zip":
        return _inspect_anf_cmp_archive(cad_file)
    if suffix == ".anf":
        return _inspect_direct_anf(cad_file, job_root=job_root)
    raise ReferencePreprocessError(
        "designInputType='anf_cmp' requires CAE.PCB.cadFile to be an ANF/CMP ZIP "
        "or a direct .anf with a same-base .cmp"
    )


def _reference_output_name(
    cad_file: Path,
    *,
    anf_cmp: AnfCmpInput | None,
) -> tuple[str, str]:
    """Derive a customer-visible reference name from the actual design input."""

    if anf_cmp is None:
        raw_name = cad_file.stem
        source = "CAE.PCB.cadFile basename"
    elif anf_cmp.source_kind == "direct":
        assert anf_cmp.anf_path is not None
        raw_name = anf_cmp.anf_path.stem
        source = "direct ANF/CMP pair basename"
    else:
        assert anf_cmp.anf_member is not None
        raw_name = Path(anf_cmp.anf_member).stem
        source = "ANF/CMP archive pair basename"

    name = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw_name.strip()).strip("._")
    if name.casefold().endswith("_ref"):
        name = name[:-4].rstrip("._")
    if not name:
        raise ReferencePreprocessError(
            "CAE.PCB.cadFile does not provide a usable reference output name"
        )
    return name, source


def build_external_reference_preprocess_plan(
    administrator_config: Mapping[str, Any],
    resolution: JobInputResolution,
    analysis_options: ResolvedAnalysisOptionCatalog,
    *,
    administrator_config_path: Path,
    work_dir: Path,
    syz_profile_id: str | None = None,
) -> ExternalReferencePreprocessPlan:
    """Normalize the two customer input types without importing live tools."""

    config = validate_administrator_config(administrator_config)
    # 해석 입력 종류는 관리자가 선언하지 않고 isZuken에서 유도한다.
    design_input_type = derive_design_input_type(bool(config["isZuken"]))
    branch = CUSTOMER_BRANCHES[design_input_type]
    selected_profile_id = syz_profile_id or analysis_options.default_syz_profile_id
    try:
        selected_profile = analysis_options.syz_profiles[selected_profile_id]
    except KeyError as exc:
        raise ReferencePreprocessError(
            f"selected SYZ Profile is not resolved: {selected_profile_id!r}"
        ) from exc

    cad_file = resolution.input("cadFile").path
    anf_cmp = None
    if design_input_type == "zuken_design":
        if cad_file.suffix.casefold() != ".zip":
            raise ReferencePreprocessError(
                "isZuken=true requires CAE.PCB.cadFile to be a Zuken design ZIP"
            )
    else:
        anf_cmp = _inspect_anf_cmp(cad_file, job_root=resolution.job_root)

    output_name, output_name_source = _reference_output_name(
        cad_file,
        anf_cmp=anf_cmp,
    )

    preprocessing = config["preprocessing"]
    zuken_exports = {
        field: bool(config[field])
        for field in ("exportANF", "exportCMP", "exportODB", "exportEDB")
    }
    return ExternalReferencePreprocessPlan(
        administrator_config_path=administrator_config_path.resolve(),
        request_path=resolution.request_path,
        job_root=resolution.job_root,
        work_dir=work_dir.resolve(),
        design_input_type=design_input_type,
        branch=branch,
        aedt_version=config["aedtVersion"],
        reference_policy=preprocessing["referencePolicy"],
        output_name=output_name,
        output_name_source=output_name_source,
        cad_file=cad_file,
        stackup=resolution.input("Stackup").path,
        bom=resolution.input("BOM").path,
        syz_profile_id=selected_profile_id,
        syz_profile_source=(
            "caller-selected SYZ Profile"
            if syz_profile_id is not None
            else "analysisOptions.defaults.syzProfileId"
        ),
        sws=selected_profile.sws.resolved_path,
        sws_sha256=selected_profile.sws.sha256,
        sfsdf=selected_profile.sfsdf.resolved_path,
        sfsdf_sha256=selected_profile.sfsdf.sha256,
        dc_short=deepcopy(preprocessing["dcShort"]),
        bom_settings=deepcopy(preprocessing["BOM"]),
        is_zuken=bool(config["isZuken"]),
        zuken_bin=Path(config["DF_path"]).resolve(),
        zuken_exports=zuken_exports,
        anf_cmp=anf_cmp,
    )


def _materialize_anf_cmp(plan: ExternalReferencePreprocessPlan) -> tuple[Path, Path]:
    assert plan.anf_cmp is not None
    if plan.anf_cmp.source_kind == "direct":
        assert plan.anf_cmp.anf_path is not None and plan.anf_cmp.cmp_path is not None
        return plan.anf_cmp.anf_path, plan.anf_cmp.cmp_path

    assert plan.anf_cmp.anf_member is not None and plan.anf_cmp.cmp_member is not None
    destination = plan.work_dir / "external_anf_cmp" / _sha256_file(plan.cad_file)[:16]
    destination.mkdir(parents=True, exist_ok=True)
    output_paths: list[Path] = []
    with zipfile.ZipFile(plan.cad_file, "r") as archive:
        for member in (plan.anf_cmp.anf_member, plan.anf_cmp.cmp_member):
            target = destination / Path(member).name
            content = archive.read(member)
            if target.exists() and target.read_bytes() != content:
                raise ReferencePreprocessError(
                    f"materialized ANF/CMP input drifted: {target}"
                )
            if not target.exists():
                target.write_bytes(content)
            output_paths.append(target.resolve())
    return output_paths[0], output_paths[1]


def build_external_reference_preprocess_request(
    plan: ExternalReferencePreprocessPlan,
) -> ReferencePreprocessRequest:
    """Materialize safe archive inputs and create the common reference request."""

    if not plan.administrator_config_path.is_file():
        raise ReferencePreprocessError(
            f"administrator Config not found: {plan.administrator_config_path}"
        )
    settings = {
        "referencePolicy": plan.reference_policy,
        "outputName": plan.output_name,
        "dcShort": deepcopy(plan.dc_short),
        "BOM": deepcopy(plan.bom_settings),
        "zuken": {"maxAttempts": 3, "retryDelaySeconds": 120},
        "zukenExports": deepcopy(plan.zuken_exports),
    }
    anf = cmp_path = None
    design = None
    source_cad = None
    zuken_bin = None
    if plan.branch == "zuken_design":
        design = plan.cad_file
        zuken_bin = plan.zuken_bin
    else:
        anf, cmp_path = _materialize_anf_cmp(plan)
        source_cad = plan.cad_file if plan.anf_cmp.source_kind == "zip" else None
    return ReferencePreprocessRequest(
        config_path=plan.administrator_config_path,
        mode=plan.branch,
        mode_source="administrator_config.designInputType",
        work_dir=plan.work_dir,
        output_name=plan.output_name,
        aedt_version=plan.aedt_version,
        reference_policy=plan.reference_policy,
        settings=settings,
        design=design,
        anf=anf,
        cmp=cmp_path,
        stackup=plan.stackup,
        bom=plan.bom,
        sws=plan.sws,
        zuken_bin=zuken_bin,
        design_input_type=plan.design_input_type,
        syz_profile_id=plan.syz_profile_id,
        source_cad=source_cad,
        zuken_exports=deepcopy(plan.zuken_exports),
    )


def run_external_reference_preprocessor(
    plan: ExternalReferencePreprocessPlan,
    *,
    backend: ReferencePreprocessBackend | None = None,
) -> ReferencePreprocessResult:
    request = build_external_reference_preprocess_request(plan)
    return run_reference_preprocess_request(request, backend=backend)
