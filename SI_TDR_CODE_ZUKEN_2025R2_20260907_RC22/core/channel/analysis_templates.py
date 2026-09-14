from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Any


class AnalysisTemplateValidationError(ValueError):
    """Raised when the analysis-template catalog cannot resolve a run setup."""


@dataclass(frozen=True)
class AnalysisTemplateSelection:
    syz_template_id: str
    tdr_template_id: str
    syz_source: str
    tdr_source: str

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return {
            "syzTemplateId": payload["syz_template_id"],
            "tdrTemplateId": payload["tdr_template_id"],
            "source": {
                "syz": payload["syz_source"],
                "tdr": payload["tdr_source"],
            },
        }


def _object(value: Any, *, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise AnalysisTemplateValidationError(f"{where} must be an object")
    return value


def _template_id(value: Any, *, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AnalysisTemplateValidationError(f"{where} must be a non-empty string")
    return value.strip()


def _template(
    templates: dict[str, Any],
    template_id: str,
    *,
    where: str,
) -> dict[str, Any]:
    if template_id not in templates:
        raise AnalysisTemplateValidationError(
            f"{where} references missing template {template_id!r}; "
            f"available={sorted(templates)}"
        )
    return _object(templates[template_id], where=f"{where}.{template_id}")


def apply_default_analysis_templates(
    config: dict[str, Any],
) -> AnalysisTemplateSelection | None:
    """Resolve Base Config defaults into effective ``syz`` and ``tdr`` blocks.

    Configs without ``analysisTemplates`` keep the legacy top-level ``syz`` and
    ``tdr`` contract unchanged. The catalog remains in the generated Run Config
    as authoring provenance, while the effective blocks are deep copies that
    runtime generation can safely extend with channels and reports.
    """

    catalog_value = config.get("analysisTemplates")
    if catalog_value is None:
        return None

    catalog = _object(catalog_value, where="analysisTemplates")
    if catalog.get("schemaVersion") != 1:
        raise AnalysisTemplateValidationError(
            "analysisTemplates.schemaVersion must be 1"
        )
    defaults = _object(catalog.get("defaults"), where="analysisTemplates.defaults")
    syz_templates = _object(
        catalog.get("syzTemplates"),
        where="analysisTemplates.syzTemplates",
    )
    tdr_templates = _object(
        catalog.get("tdrTemplates"),
        where="analysisTemplates.tdrTemplates",
    )

    syz_template_id = _template_id(
        defaults.get("syzTemplateId"),
        where="analysisTemplates.defaults.syzTemplateId",
    )
    tdr_template_id = _template_id(
        defaults.get("tdrTemplateId"),
        where="analysisTemplates.defaults.tdrTemplateId",
    )
    syz_template = _template(
        syz_templates,
        syz_template_id,
        where="analysisTemplates.syzTemplates",
    )
    tdr_template = _template(
        tdr_templates,
        tdr_template_id,
        where="analysisTemplates.tdrTemplates",
    )

    generated_tdr_keys = sorted(
        {"channels", "reportGroups", "timeRangeResolution"} & set(tdr_template)
    )
    if generated_tdr_keys:
        raise AnalysisTemplateValidationError(
            "TDR templates cannot contain generated run fields: "
            + ", ".join(generated_tdr_keys)
        )
    if "touchstoneBaseName" in syz_template:
        raise AnalysisTemplateValidationError(
            "SYZ templates cannot contain generated run field touchstoneBaseName"
        )

    selection = AnalysisTemplateSelection(
        syz_template_id=syz_template_id,
        tdr_template_id=tdr_template_id,
        syz_source="analysisTemplates.defaults",
        tdr_source="analysisTemplates.defaults",
    )
    config["syz"] = deepcopy(syz_template)
    config["tdr"] = deepcopy(tdr_template)
    config["analysisTemplateSelection"] = selection.to_dict()
    return selection
