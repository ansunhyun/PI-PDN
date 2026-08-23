from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .targets import ChannelTarget


ROOT = Path(__file__).resolve().parents[2]
DCIR_VENV_SITE_PACKAGES = ROOT / "DCIR" / "SIwave_DCIR-1p4p1" / ".venv" / "Lib" / "site-packages"


@dataclass(frozen=True)
class PinLookupRecord:
    channel: str
    polarity: str
    component: str
    pin: str
    found: bool
    net: str | None = None
    position: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class PinLookupRequest:
    channel: str
    polarity: str
    component: str
    pin: str


@dataclass(frozen=True)
class ChannelPinLookupReport:
    aedb: str
    aedt_version: str
    records: list[PinLookupRecord]

    @property
    def found_count(self) -> int:
        return sum(1 for record in self.records if record.found)

    @property
    def missing_count(self) -> int:
        return sum(1 for record in self.records if not record.found)

    def to_dict(self) -> dict[str, Any]:
        return {
            "aedb": self.aedb,
            "aedtVersion": self.aedt_version,
            "summary": {
                "total": len(self.records),
                "found": self.found_count,
                "missing": self.missing_count,
            },
            "records": [asdict(record) for record in self.records],
        }


def _ensure_pyedb_path() -> None:
    if DCIR_VENV_SITE_PACKAGES.exists():
        site_packages = str(DCIR_VENV_SITE_PACKAGES)
        if site_packages not in sys.path:
            sys.path.insert(0, site_packages)


def _pin_name(pin: object) -> str | None:
    for attr in ("component_pin", "name", "pin_name"):
        value = getattr(pin, attr, None)
        if value is not None:
            return str(value)
    raw = getattr(pin, "_edb_object", None)
    if raw is not None:
        try:
            return str(raw.GetName())
        except Exception:
            pass
    return None


def _pin_net(pin: object) -> str | None:
    for attr in ("net_name", "net"):
        value = getattr(pin, attr, None)
        if value is not None:
            return str(value)
    raw = getattr(pin, "_edb_object", None)
    if raw is not None:
        try:
            net = raw.GetNet()
            return str(net.GetName())
        except Exception:
            pass
    return None


def _pin_position(pin: object) -> str | None:
    raw = getattr(pin, "_edb_object", None)
    if raw is None:
        return None
    for method in ("GetPosition", "GetCenter"):
        try:
            return str(getattr(raw, method)())
        except Exception:
            pass
    return None


def _component_pins_by_name(edb: object, refdes: str) -> dict[str, object]:
    try:
        pins = edb.components.get_pin_from_component(refdes)
    except Exception as exc:
        raise LookupError(f"component lookup failed for {refdes}: {type(exc).__name__}: {exc}") from exc
    pins_by_name: dict[str, object] = {}
    for pin in pins:
        name = _pin_name(pin)
        if name:
            pins_by_name[name] = pin
    return pins_by_name


def _lookup_pin(edb: object, *, channel: str, polarity: str, component: str, pin_name: str) -> PinLookupRecord:
    if pin_name.upper().startswith("TBD"):
        return PinLookupRecord(
            channel=channel,
            polarity=polarity,
            component=component,
            pin=pin_name,
            found=False,
            error="placeholder pin; replace with customer-confirmed pin",
        )

    try:
        pins_by_name = _component_pins_by_name(edb, component)
    except LookupError as exc:
        return PinLookupRecord(
            channel=channel,
            polarity=polarity,
            component=component,
            pin=pin_name,
            found=False,
            error=str(exc),
        )

    pin = pins_by_name.get(pin_name)
    if pin is None:
        return PinLookupRecord(
            channel=channel,
            polarity=polarity,
            component=component,
            pin=pin_name,
            found=False,
            error=f"pin not found on component {component}",
        )
    return PinLookupRecord(
        channel=channel,
        polarity=polarity,
        component=component,
        pin=pin_name,
        found=True,
        net=_pin_net(pin),
        position=_pin_position(pin),
    )


def lookup_channel_target_pins(
    aedb_path: Path,
    targets: list[ChannelTarget],
    *,
    aedt_version: str = "2024.2",
) -> ChannelPinLookupReport:
    requests: list[PinLookupRequest] = []
    for target in targets:
        requests.append(
            PinLookupRequest(
                channel=target.name,
                polarity="positive",
                component=target.ic_refdes,
                pin=target.pos_pin,
            )
        )
        requests.append(
            PinLookupRequest(
                channel=target.name,
                polarity="negative",
                component=target.ic_refdes,
                pin=target.neg_pin,
            )
        )
    return lookup_pin_requests(aedb_path, requests, aedt_version=aedt_version)


def lookup_pin_requests(
    aedb_path: Path,
    requests: list[PinLookupRequest],
    *,
    aedt_version: str = "2024.2",
) -> ChannelPinLookupReport:
    """Resolve generic component/pin requests without requiring a ChannelTarget first."""
    _ensure_pyedb_path()
    from pyedb import Edb  # noqa: PLC0415

    edb = Edb(edbpath=str(aedb_path), edbversion=aedt_version)
    records: list[PinLookupRecord] = []
    try:
        for request in requests:
            records.append(
                _lookup_pin(
                    edb,
                    channel=request.channel,
                    polarity=request.polarity,
                    component=request.component,
                    pin_name=request.pin,
                )
            )
    finally:
        edb.close_edb()

    return ChannelPinLookupReport(
        aedb=str(aedb_path),
        aedt_version=aedt_version,
        records=records,
    )


def write_pin_lookup_report(
    aedb_path: Path,
    targets: list[ChannelTarget],
    output_path: Path,
    *,
    aedt_version: str = "2024.2",
) -> ChannelPinLookupReport:
    report = lookup_channel_target_pins(aedb_path, targets, aedt_version=aedt_version)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fp:
        json.dump(report.to_dict(), fp, indent=2, ensure_ascii=False)
        fp.write("\n")
    return report
