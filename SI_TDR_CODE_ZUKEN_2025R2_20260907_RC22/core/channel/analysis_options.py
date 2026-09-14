from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Any, Mapping

try:
    from ..syz_options import (
        SYZ_REQUIRED_SETTING_FIELDS,
        SYZ_SETTING_FIELDS,
        normalize_syz_settings,
    )
except ImportError:  # Support compact customer core imports.
    from syz_options import (  # type: ignore[no-redef]
        SYZ_REQUIRED_SETTING_FIELDS,
        SYZ_SETTING_FIELDS,
        normalize_syz_settings,
    )


CATALOG_SCHEMA = "si-tdr-analysis-option-catalog/1"
SELECTION_SCHEMA = "si-tdr-analysis-option-selection/1"
ANALYSIS_OPTION_FOLDER_PARTS = {
    "sws": ("Setup", "SWS"),
    "sfsdf": ("Setup", "SFSDF"),
    "tdr": ("Setup", "TDR"),
}
REMOVED_TDR_FIELDS = frozenset(
    {"stepPs", "stopPs", "useTsConvolution", "useToConvolution"}
)
TDR_REQUIRED_FIELDS = (
    "riseTimePs",
    "pulseRepetition",
    "pulseWidth",
    "timeDelay",
)


class AnalysisOptionError(ValueError):
    """Raised when a staged analysis option Profile cannot be used safely."""


@dataclass(frozen=True)
class ResolvedOptionFile:
    file_name: str
    resolved_path: Path
    sha256: str

    def to_dict(self) -> dict[str, str]:
        return {
            "fileName": self.file_name,
            "resolvedPath": str(self.resolved_path),
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class ResolvedSyzProfile:
    profile_id: str
    sws: ResolvedOptionFile
    sfsdf: ResolvedOptionFile
    settings: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "profileId": self.profile_id,
            "sws": self.sws.to_dict(),
            "sfsdf": self.sfsdf.to_dict(),
            "settings": deepcopy(self.settings),
        }


@dataclass(frozen=True)
class ResolvedTdrProfile:
    profile_id: str
    option_file: ResolvedOptionFile
    settings: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "profileId": self.profile_id,
            "file": self.option_file.to_dict(),
            "settings": deepcopy(self.settings),
        }


@dataclass(frozen=True)
class ResolvedAnalysisOptionCatalog:
    job_root: Path
    default_syz_profile_id: str
    default_tdr_profile_id: str
    syz_profiles: dict[str, ResolvedSyzProfile]
    tdr_profiles: dict[str, ResolvedTdrProfile]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": CATALOG_SCHEMA,
            "jobRoot": str(self.job_root),
            "defaults": {
                "syzProfileId": self.default_syz_profile_id,
                "tdrProfileId": self.default_tdr_profile_id,
            },
            "profiles": {
                "syz": {
                    profile_id: profile.to_dict()
                    for profile_id, profile in self.syz_profiles.items()
                },
                "tdr": {
                    profile_id: profile.to_dict()
                    for profile_id, profile in self.tdr_profiles.items()
                },
            },
        }


def _object(value: Any, *, where: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AnalysisOptionError(f"{where} must be an object")
    return value


def _identifier(value: Any, *, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AnalysisOptionError(f"{where} must be a non-empty string")
    return value.strip()


def _basename(value: Any, *, where: str, extension: str) -> str:
    name = _identifier(value, where=where)
    path = PurePath(name)
    if (
        path.name != name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        or any(ord(character) < 32 for character in name)
    ):
        raise AnalysisOptionError(f"{where} must be a Job-local file name, not a path")
    if Path(name).suffix.casefold() != extension:
        raise AnalysisOptionError(f"{where} must use the {extension} extension")
    return name


def _matching_entries(folder: Path, requested_name: str) -> list[Path]:
    if not folder.is_dir():
        return []
    folded_name = requested_name.casefold()
    return sorted(
        (item for item in folder.iterdir() if item.name.casefold() == folded_name),
        key=lambda item: item.name,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _resolve_option_file(
    *,
    job_root: Path,
    folder_parts: tuple[str, ...],
    file_name: Any,
    extension: str,
    where: str,
) -> ResolvedOptionFile:
    requested_name = _basename(file_name, where=where, extension=extension)
    folder = job_root.joinpath(*folder_parts)
    folder_label = "/".join(folder_parts)
    matches = _matching_entries(folder, requested_name)
    if len(matches) > 1:
        raise AnalysisOptionError(
            f"{where} is ambiguous in Job folder {folder_label}/: "
            + ", ".join(str(path) for path in matches)
        )
    if not matches:
        raise AnalysisOptionError(
            f"{where} file not found in required Job folder {folder_label}/: "
            f"{requested_name}"
        )
    try:
        resolved = matches[0].resolve(strict=True)
        resolved_job_root = job_root.resolve(strict=True)
        resolved_folder = folder.resolve(strict=True)
    except OSError as exc:
        raise AnalysisOptionError(f"{where} cannot be resolved: {matches[0]}") from exc
    if resolved_job_root not in resolved.parents:
        raise AnalysisOptionError(f"{where} resolves outside the Job root: {matches[0]}")
    if resolved_folder not in resolved.parents:
        raise AnalysisOptionError(
            f"{where} resolves outside the required Job folder {folder_label}/: {matches[0]}"
        )
    if resolved.parent != resolved_folder:
        raise AnalysisOptionError(
            f"{where} must be directly under required Job folder {folder_label}/: "
            f"{matches[0]}"
        )
    if not resolved.is_file():
        raise AnalysisOptionError(f"{where} is not a file: {matches[0]}")
    return ResolvedOptionFile(
        file_name=requested_name,
        resolved_path=resolved,
        sha256=_sha256(resolved),
    )


def _walk_removed_tdr_fields(value: Any, *, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            if key in REMOVED_TDR_FIELDS:
                raise AnalysisOptionError(
                    f"removed TDR option field is not supported: {child_path}"
                )
            _walk_removed_tdr_fields(child, path=child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk_removed_tdr_fields(child, path=f"{path}[{index}]")


def _load_tdr_settings(path: Path, *, profile_id: str) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AnalysisOptionError(
            f"TDR Profile {profile_id!r} has invalid JSON {path}: {exc}"
        ) from exc
    settings = _object(payload, where=f"TDR Profile {profile_id!r} JSON root")
    _walk_removed_tdr_fields(settings)
    unsupported = sorted(set(settings) - set(TDR_REQUIRED_FIELDS))
    if unsupported:
        raise AnalysisOptionError(
            f"TDR Profile {profile_id!r} has unsupported fields: {unsupported}; "
            f"allowed={list(TDR_REQUIRED_FIELDS)}"
        )
    missing = [field for field in TDR_REQUIRED_FIELDS if field not in settings]
    if missing:
        raise AnalysisOptionError(
            f"TDR Profile {profile_id!r} is missing required fields: {missing}"
        )
    rise_time = settings["riseTimePs"]
    if (
        isinstance(rise_time, bool)
        or not isinstance(rise_time, (int, float))
        or float(rise_time) <= 0
    ):
        raise AnalysisOptionError(
            f"TDR Profile {profile_id!r} riseTimePs must be a positive number"
        )
    for field in ("pulseRepetition", "pulseWidth", "timeDelay"):
        if not isinstance(settings[field], str) or not settings[field].strip():
            raise AnalysisOptionError(
                f"TDR Profile {profile_id!r} {field} must be a non-empty value with units"
            )
    return deepcopy(dict(settings))


def resolve_analysis_option_catalog(
    config: Mapping[str, Any],
    *,
    job_root: Path,
) -> ResolvedAnalysisOptionCatalog:
    """Resolve every configured Profile from the three staged Setup folders only."""

    resolved_job_root = job_root.resolve()
    if not resolved_job_root.is_dir():
        raise AnalysisOptionError(f"Job root folder not found: {resolved_job_root}")
    options = _object(config.get("analysisOptions"), where="analysisOptions")
    defaults = _object(options.get("defaults"), where="analysisOptions.defaults")
    default_syz_profile_id = _identifier(
        defaults.get("syzProfileId"),
        where="analysisOptions.defaults.syzProfileId",
    )
    default_tdr_profile_id = _identifier(
        defaults.get("tdrProfileId"),
        where="analysisOptions.defaults.tdrProfileId",
    )
    raw_syz_profiles = _object(options.get("syz"), where="analysisOptions.syz")
    raw_tdr_profiles = _object(options.get("tdr"), where="analysisOptions.tdr")

    syz_profiles: dict[str, ResolvedSyzProfile] = {}
    for raw_profile_id, raw_profile in raw_syz_profiles.items():
        profile_id = _identifier(raw_profile_id, where="analysisOptions.syz profile ID")
        profile = _object(raw_profile, where=f"analysisOptions.syz.{profile_id}")
        expected_fields = {"sws", "sfsdf"} | set(SYZ_REQUIRED_SETTING_FIELDS)
        if profile.get("sweepMode") == "interpolating":
            expected_fields.add("interpolation")
        if set(profile) != expected_fields:
            raise AnalysisOptionError(
                f"analysisOptions.syz.{profile_id} fields must be exactly "
                f"{sorted(expected_fields)}"
            )
        try:
            syz_settings = normalize_syz_settings(
                {key: profile[key] for key in profile if key in SYZ_SETTING_FIELDS},
                where=f"analysisOptions.syz.{profile_id}",
            )
        except ValueError as exc:
            raise AnalysisOptionError(str(exc)) from exc
        syz_profiles[profile_id] = ResolvedSyzProfile(
            profile_id=profile_id,
            sws=_resolve_option_file(
                job_root=resolved_job_root,
                folder_parts=ANALYSIS_OPTION_FOLDER_PARTS["sws"],
                file_name=profile.get("sws"),
                extension=".sws",
                where=f"analysisOptions.syz.{profile_id}.sws",
            ),
            sfsdf=_resolve_option_file(
                job_root=resolved_job_root,
                folder_parts=ANALYSIS_OPTION_FOLDER_PARTS["sfsdf"],
                file_name=profile.get("sfsdf"),
                extension=".sfsdf",
                where=f"analysisOptions.syz.{profile_id}.sfsdf",
            ),
            settings=syz_settings,
        )

    tdr_profiles: dict[str, ResolvedTdrProfile] = {}
    for raw_profile_id, raw_profile in raw_tdr_profiles.items():
        profile_id = _identifier(raw_profile_id, where="analysisOptions.tdr profile ID")
        profile = _object(raw_profile, where=f"analysisOptions.tdr.{profile_id}")
        option_file = _resolve_option_file(
            job_root=resolved_job_root,
            folder_parts=ANALYSIS_OPTION_FOLDER_PARTS["tdr"],
            file_name=profile.get("file"),
            extension=".json",
            where=f"analysisOptions.tdr.{profile_id}.file",
        )
        tdr_profiles[profile_id] = ResolvedTdrProfile(
            profile_id=profile_id,
            option_file=option_file,
            settings=_load_tdr_settings(option_file.resolved_path, profile_id=profile_id),
        )

    if default_syz_profile_id not in syz_profiles:
        raise AnalysisOptionError(
            f"default SYZ Profile is not defined: {default_syz_profile_id!r}"
        )
    if default_tdr_profile_id not in tdr_profiles:
        raise AnalysisOptionError(
            f"default TDR Profile is not defined: {default_tdr_profile_id!r}"
        )
    return ResolvedAnalysisOptionCatalog(
        job_root=resolved_job_root,
        default_syz_profile_id=default_syz_profile_id,
        default_tdr_profile_id=default_tdr_profile_id,
        syz_profiles=syz_profiles,
        tdr_profiles=tdr_profiles,
    )


def select_analysis_options(
    catalog: ResolvedAnalysisOptionCatalog,
    *,
    batch_id: str,
    syz_profile_id: str | None,
    tdr_profile_ids_by_item: Mapping[str, str | None],
) -> dict[str, Any]:
    selected_syz_id = (syz_profile_id or "").strip() or catalog.default_syz_profile_id
    syz_source = "Spec.SYZ_Option" if (syz_profile_id or "").strip() else "administratorDefault"
    syz_profile = catalog.syz_profiles.get(selected_syz_id)
    if syz_profile is None:
        raise AnalysisOptionError(
            f"sNp batch {batch_id!r} selects undefined SYZ_Option {selected_syz_id!r}; "
            f"available={sorted(catalog.syz_profiles)}"
        )

    tdr_selections: dict[str, Any] = {}
    for item_id, requested_profile_id in tdr_profile_ids_by_item.items():
        selected_tdr_id = (
            (requested_profile_id or "").strip() or catalog.default_tdr_profile_id
        )
        tdr_source = (
            "Spec.TDR_Option"
            if (requested_profile_id or "").strip()
            else "administratorDefault"
        )
        tdr_profile = catalog.tdr_profiles.get(selected_tdr_id)
        if tdr_profile is None:
            raise AnalysisOptionError(
                f"sNp batch {batch_id!r}, TDR item {item_id!r} selects undefined "
                f"TDR_Option {selected_tdr_id!r}; available={sorted(catalog.tdr_profiles)}"
            )
        tdr_selections[item_id] = {
            "itemId": item_id,
            "selectionSource": tdr_source,
            **tdr_profile.to_dict(),
        }

    return {
        "schema": SELECTION_SCHEMA,
        "batchId": batch_id,
        "syz": {
            "selectionSource": syz_source,
            **syz_profile.to_dict(),
        },
        "tdr": tdr_selections,
    }
