from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable


PATH_START_TO_ENDPOINT = "path_start_to_endpoint"
PATH_ENDPOINT_TO_START = "path_endpoint_to_start"
MEASUREMENT_DIRECTIONS = {
    PATH_START_TO_ENDPOINT,
    PATH_ENDPOINT_TO_START,
}


class DirectionResolutionError(ValueError):
    pass


def normalize_measurement_direction(value: Any, *, where: str) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized not in MEASUREMENT_DIRECTIONS:
        raise DirectionResolutionError(
            f"{where} must be one of {sorted(MEASUREMENT_DIRECTIONS)}"
        )
    return normalized


@dataclass(frozen=True)
class DirectionCandidate:
    value: str
    source: str
    priority: int
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DirectionResolution:
    status: str
    value: str | None
    source: str | None
    reason: str | None
    near_path_side: str | None
    far_path_side: str | None
    candidates: tuple[DirectionCandidate, ...]
    overridden_candidates: tuple[DirectionCandidate, ...] = ()
    issues: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "value": self.value,
            "source": self.source,
            "reason": self.reason,
            "nearPathSide": self.near_path_side,
            "farPathSide": self.far_path_side,
            "candidates": [item.to_dict() for item in self.candidates],
            "overriddenCandidates": [
                item.to_dict() for item in self.overridden_candidates
            ],
            "issues": list(self.issues),
        }


def direction_candidate(
    value: Any,
    *,
    source: str,
    priority: int,
    reason: str | None = None,
) -> DirectionCandidate | None:
    if value is None or not str(value).strip():
        return None
    if not str(source).strip():
        raise DirectionResolutionError("direction candidate source is required")
    return DirectionCandidate(
        value=normalize_measurement_direction(value, where=f"{source}.measurementDirection"),
        source=str(source).strip(),
        priority=int(priority),
        reason=str(reason).strip() if reason and str(reason).strip() else None,
    )


def resolve_measurement_direction(
    candidates: Iterable[DirectionCandidate | None],
    *,
    required: bool = True,
) -> DirectionResolution:
    present = tuple(item for item in candidates if item is not None)
    if not present:
        issues = (
            {
                "code": "measurement_direction_missing",
                "message": "measurement direction has no explicit, profile, override, or compatibility candidate",
            },
        )
        return DirectionResolution(
            status="unresolved" if required else "not_configured",
            value=None,
            source=None,
            reason=None,
            near_path_side=None,
            far_path_side=None,
            candidates=(),
            issues=issues,
        )

    highest_priority = max(item.priority for item in present)
    selected_candidates = tuple(
        item for item in present if item.priority == highest_priority
    )
    selected_values = {item.value for item in selected_candidates}
    if len(selected_values) != 1:
        return DirectionResolution(
            status="unresolved",
            value=None,
            source=None,
            reason=None,
            near_path_side=None,
            far_path_side=None,
            candidates=present,
            issues=(
                {
                    "code": "measurement_direction_ambiguous",
                    "message": "multiple same-priority measurement direction candidates disagree",
                    "sources": [item.source for item in selected_candidates],
                    "values": sorted(selected_values),
                },
            ),
        )

    selected = selected_candidates[0]
    overridden = tuple(
        item
        for item in present
        if item is not selected and item.value != selected.value
    )
    near_path_side = (
        "start" if selected.value == PATH_START_TO_ENDPOINT else "endpoint"
    )
    far_path_side = "endpoint" if near_path_side == "start" else "start"
    return DirectionResolution(
        status="resolved",
        value=selected.value,
        source=selected.source,
        reason=selected.reason,
        near_path_side=near_path_side,
        far_path_side=far_path_side,
        candidates=present,
        overridden_candidates=overridden,
    )


def require_resolved_direction(resolution: DirectionResolution, *, where: str) -> str:
    if resolution.status != "resolved" or resolution.value is None:
        issue_codes = [str(item.get("code")) for item in resolution.issues]
        raise DirectionResolutionError(
            f"{where} measurement direction is unresolved: {issue_codes}"
        )
    return resolution.value
