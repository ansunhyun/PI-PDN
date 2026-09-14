from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fp:
        payload = json.load(fp)
    if not isinstance(payload, dict):
        raise ValueError(f"JSON root must be an object: {path}")
    return payload


def _validated_role_metadata(
    config_fragment: dict[str, Any],
    port_order: list[str],
) -> dict[str, Any] | None:
    raw = config_fragment.get("portRoleMetadata")
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("config fragment portRoleMetadata must be an object")
    if raw.get("schemaVersion") != 2:
        raise ValueError(
            "config fragment portRoleMetadata.schemaVersion must be 2"
        )
    role_order = raw.get("portOrder") or []
    if not isinstance(role_order, list) or not all(
        isinstance(item, dict) for item in role_order
    ):
        raise ValueError("portRoleMetadata.portOrder must be a list of objects")
    role_names = [str(item.get("name") or "") for item in role_order]
    role_indices = [int(item.get("index") or 0) for item in role_order]
    if role_names != port_order:
        raise ValueError(
            "portRoleMetadata port order does not match ports.portOrder"
        )
    if role_indices != list(range(1, len(port_order) + 1)):
        raise ValueError("portRoleMetadata port indices are not sequential")

    index_by_name = {
        name: index for index, name in enumerate(port_order, start=1)
    }
    for channel in raw.get("channels") or []:
        if not isinstance(channel, dict):
            raise ValueError("portRoleMetadata channel entries must be objects")
        for endpoint_name in ("near", "far"):
            endpoint = channel.get(endpoint_name) or {}
            ports = endpoint.get("ports") or {}
            for polarity in ("positive", "negative"):
                port = ports.get(polarity) or {}
                name = str(port.get("name") or "")
                index = int(port.get("index") or 0)
                if not name or name not in index_by_name:
                    raise ValueError(
                        f"portRoleMetadata channel {channel.get('name')} has unknown "
                        f"{endpoint_name} {polarity} port {name!r}"
                    )
                if index != index_by_name[name]:
                    raise ValueError(
                        f"portRoleMetadata channel {channel.get('name')} has mismatched "
                        f"index for port {name!r}"
                    )
    return copy.deepcopy(raw)


def filter_port_metadata_by_config_fragment(
    source_metadata: dict[str, Any],
    config_fragment: dict[str, Any],
    *,
    source_metadata_path: Path | None = None,
) -> dict[str, Any]:
    port_order = [str(item) for item in (config_fragment.get("ports") or {}).get("portOrder") or []]
    if not port_order:
        raise ValueError("config fragment does not contain ports.portOrder")
    role_metadata = _validated_role_metadata(config_fragment, port_order)

    ports_by_name = {str(port.get("name")): port for port in source_metadata.get("ports") or []}
    selected_ports: list[dict[str, Any]] = []
    missing_ports: list[str] = []
    for index, port_name in enumerate(port_order, start=1):
        source_port = ports_by_name.get(port_name)
        if source_port is None:
            missing_ports.append(port_name)
            continue
        selected = copy.deepcopy(source_port)
        selected["index"] = index
        selected_ports.append(selected)

    output = {
        "schemaVersion": 2 if role_metadata else 1,
        "sourceMetadata": str(source_metadata_path) if source_metadata_path else source_metadata.get("sourceAedb"),
        "sourceAedb": source_metadata.get("sourceAedb"),
        "sourceTouchstone": source_metadata.get("sourceTouchstone"),
        "portCount": len(selected_ports),
        "ports": selected_ports,
        "selection": {
            "requestedPortCount": len(port_order),
            "selectedPortCount": len(selected_ports),
            "missingPorts": missing_ports,
            "portOrderPolicy": (config_fragment.get("ports") or {}).get("portOrderPolicy"),
        },
    }
    if role_metadata is not None:
        output["portRoleMetadata"] = role_metadata
    return output


def _path_ports_by_name(
    path_report: dict[str, Any],
    *,
    layer: str | None,
    impedance_ohm: float,
) -> dict[str, dict[str, Any]]:
    ports: dict[str, dict[str, Any]] = {}
    for path in path_report.get("paths") or []:
        if not str(path.get("status") or "").startswith("resolved"):
            continue

        start_net = path.get("start_net")
        start_port_name = path.get("start_port_name") or start_net
        start_component = path.get("start_component")
        start_pin = path.get("start_pin")
        if start_net and start_port_name and start_component and start_pin:
            ports[str(start_port_name)] = _padstack_port_record(
                name=str(start_port_name),
                component=str(start_component),
                pin=str(start_pin),
                net=str(start_net),
                layer=layer,
                impedance_ohm=impedance_ohm,
            )

        end_net = path.get("end_net")
        end_port_name = path.get("end_port_name") or end_net
        endpoint_component = path.get("endpoint_component")
        endpoint_pin = path.get("endpoint_pin")
        if end_net and end_port_name and endpoint_component and endpoint_pin:
            ports[str(end_port_name)] = _padstack_port_record(
                name=str(end_port_name),
                component=str(endpoint_component),
                pin=str(endpoint_pin),
                net=str(end_net),
                layer=layer,
                impedance_ohm=impedance_ohm,
            )
    return ports


def _padstack_port_record(
    *,
    name: str,
    component: str,
    pin: str,
    net: str,
    layer: str | None,
    impedance_ohm: float,
) -> dict[str, Any]:
    parameters: dict[str, Any] = {
        "ok": True,
        "terminalType": "PadstackInstanceTerminal",
        "padstack": {
            "component": component,
            "pin": pin,
            "net": net,
        },
    }
    if layer:
        parameters["layer"] = layer
    return {
        "name": name,
        "positive": {
            "name": name,
            "net": net,
            "impedance": f"{impedance_ohm:g}",
            "terminalType": "PadstackInstanceTerminal",
            "boundaryType": "PortBoundary",
            "isCircuitPort": "False",
            "rawClass": "PadstackInstanceTerminal",
            "parameters": parameters,
        },
        "reference": None,
    }


def build_port_metadata_from_channel_paths(
    path_report: dict[str, Any],
    config_fragment: dict[str, Any],
    *,
    layer: str | None = None,
    impedance_ohm: float = 50.0,
) -> dict[str, Any]:
    port_order = [str(item) for item in (config_fragment.get("ports") or {}).get("portOrder") or []]
    if not port_order:
        raise ValueError("config fragment does not contain ports.portOrder")
    role_metadata = _validated_role_metadata(config_fragment, port_order)

    available_ports = _path_ports_by_name(path_report, layer=layer, impedance_ohm=impedance_ohm)
    selected_ports: list[dict[str, Any]] = []
    missing_ports: list[str] = []
    for index, port_name in enumerate(port_order, start=1):
        port = available_ports.get(port_name)
        if port is None:
            missing_ports.append(port_name)
            continue
        selected = copy.deepcopy(port)
        selected["index"] = index
        selected_ports.append(selected)

    output = {
        "schemaVersion": 2 if role_metadata else 1,
        "sourcePathReport": path_report.get("aedb"),
        "portCount": len(selected_ports),
        "ports": selected_ports,
        "selection": {
            "requestedPortCount": len(port_order),
            "selectedPortCount": len(selected_ports),
            "missingPorts": missing_ports,
            "portOrderPolicy": (config_fragment.get("ports") or {}).get("portOrderPolicy"),
            "generation": "channel-path-padstack-to-reference",
            "positiveLayerPolicy": (
                "legacy_explicit_override" if layer else "pin_layer_range_start"
            ),
        },
    }
    if role_metadata is not None:
        output["portRoleMetadata"] = role_metadata
    return output


def write_port_metadata_from_channel_paths(
    path_report_path: Path,
    config_fragment_path: Path,
    output_path: Path,
    *,
    layer: str | None = None,
    impedance_ohm: float = 50.0,
) -> dict[str, Any]:
    path_report = _read_json(path_report_path)
    config_fragment = _read_json(config_fragment_path)
    output = build_port_metadata_from_channel_paths(
        path_report,
        config_fragment,
        layer=layer,
        impedance_ohm=impedance_ohm,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fp:
        json.dump(output, fp, indent=2, ensure_ascii=False)
        fp.write("\n")
    return output


def write_filtered_port_metadata(
    source_metadata_path: Path,
    config_fragment_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    source_metadata = _read_json(source_metadata_path)
    config_fragment = _read_json(config_fragment_path)
    output = filter_port_metadata_by_config_fragment(
        source_metadata,
        config_fragment,
        source_metadata_path=source_metadata_path,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fp:
        json.dump(output, fp, indent=2, ensure_ascii=False)
        fp.write("\n")
    return output
