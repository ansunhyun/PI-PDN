from __future__ import annotations

import math
from copy import deepcopy
from typing import Any, Mapping


SYZ_SWEEP_MODES = frozenset({"discrete", "interpolating"})
SYZ_SETTING_FIELDS = frozenset(
    {
        "sweepMode",
        "interpolation",
        "computeExactDcPoint",
        "enforceCausality",
        "enforcePassivity",
    }
)
SYZ_REQUIRED_SETTING_FIELDS = frozenset(
    {
        "sweepMode",
        "computeExactDcPoint",
        "enforceCausality",
        "enforcePassivity",
    }
)


class SyzOptionError(ValueError):
    """Raised when administrator-owned SYZ settings are ambiguous or unsafe."""


def _required_bool(value: Any, *, where: str) -> bool:
    if not isinstance(value, bool):
        raise SyzOptionError(f"{where} must be true or false")
    return value


def normalize_syz_settings(value: Any, *, where: str) -> dict[str, Any]:
    """Validate and normalize the administrator-owned SYZ option contract."""

    if not isinstance(value, Mapping):
        raise SyzOptionError(f"{where} must be an object")
    unknown = sorted(set(value) - SYZ_SETTING_FIELDS)
    if unknown:
        raise SyzOptionError(f"{where} has unsupported fields: {unknown}")
    missing = sorted(SYZ_REQUIRED_SETTING_FIELDS - set(value))
    if missing:
        raise SyzOptionError(f"{where} is missing required fields: {missing}")

    sweep_mode = value.get("sweepMode")
    if sweep_mode not in SYZ_SWEEP_MODES:
        raise SyzOptionError(
            f"{where}.sweepMode must be one of {sorted(SYZ_SWEEP_MODES)}"
        )
    compute_exact_dc = _required_bool(
        value.get("computeExactDcPoint"),
        where=f"{where}.computeExactDcPoint",
    )
    enforce_causality = _required_bool(
        value.get("enforceCausality"),
        where=f"{where}.enforceCausality",
    )
    enforce_passivity = _required_bool(
        value.get("enforcePassivity"),
        where=f"{where}.enforcePassivity",
    )
    if compute_exact_dc and enforce_causality:
        raise SyzOptionError(
            f"{where} cannot enable computeExactDcPoint and enforceCausality together"
        )

    normalized: dict[str, Any] = {
        "sweepMode": sweep_mode,
        "computeExactDcPoint": compute_exact_dc,
        "enforceCausality": enforce_causality,
        "enforcePassivity": enforce_passivity,
    }
    interpolation = value.get("interpolation")
    if sweep_mode == "discrete":
        if "interpolation" in value:
            raise SyzOptionError(
                f"{where}.interpolation is not allowed when sweepMode is discrete"
            )
        return normalized

    if not isinstance(interpolation, Mapping):
        raise SyzOptionError(
            f"{where}.interpolation is required when sweepMode is interpolating"
        )
    if set(interpolation) != {"convergence", "maxInterpPts"}:
        raise SyzOptionError(
            f"{where}.interpolation must contain only convergence and maxInterpPts"
        )
    convergence = interpolation.get("convergence")
    if (
        isinstance(convergence, bool)
        or not isinstance(convergence, (int, float))
        or not math.isfinite(float(convergence))
        or float(convergence) <= 0
    ):
        raise SyzOptionError(
            f"{where}.interpolation.convergence must be a finite positive number"
        )
    max_points = interpolation.get("maxInterpPts")
    if isinstance(max_points, bool) or not isinstance(max_points, int) or max_points <= 0:
        raise SyzOptionError(
            f"{where}.interpolation.maxInterpPts must be a positive integer"
        )
    normalized["interpolation"] = {
        "convergence": float(convergence),
        "maxInterpPts": max_points,
    }
    return deepcopy(normalized)
