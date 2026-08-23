"""Apply a customer STK (AnsysEM StackupLayers format) onto an ANF-imported AEDB via pyedb.

Why not SIWave ScrImportLayerStackupFile: it requires the STK layer structure to match
the ANF-imported project (rejects extra layers such as solder resist), and the EDB
exported after a successful import fails SIWave SYZ plane detection
("no significant planes detected") — verified 2026-07-08, see
docs/si-tdr-stk-route-verification-2026-07-08.md. This module keeps the STK file as the
input contract but applies it with the pyedb mechanics validated on 7/5 (stkfix):
thickness/material per layer, Djordjevic-Sarkar dielectrics, and it can add outer
dielectric layers (solder resist) that the native import cannot.
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SITE_PACKAGES = ROOT / "DCIR" / "SIwave_DCIR-1p4p1" / ".venv" / "Lib" / "site-packages"

LAYER_RE = re.compile(r"Layer\(LayerID=(\d+), LayerName='([^']*)', LayerType=(\d+),")
FIELD_RE = {
    "thickness": re.compile(r"Thickness=([0-9.eE+-]+)"),
    "material": re.compile(r"Material='([^']*)'"),
    "fill": re.compile(r"DefinedDielectricFill='([^']*)'"),
}
CONDUCTOR_TYPE = "2"


def _edb_class():
    try:
        from pyedb import Edb
    except ImportError:
        if SITE_PACKAGES.exists():
            sys.path.insert(0, str(SITE_PACKAGES))
        try:
            from pyedb import Edb
        except ImportError as exc:
            raise RuntimeError("PyEDB is required to apply an STK to an AEDB") from exc
    return Edb


def parse_stk(path: Path) -> tuple[list[dict], dict[str, dict]]:
    """Return ordered layers and explicitly defined STK materials."""
    text = path.read_text(encoding="utf-8")
    layers: list[dict] = []
    for line in text.splitlines():
        m = LAYER_RE.search(line)
        if not m:
            continue
        layers.append(
            {
                "name": m.group(2),
                "kind": "conductor" if m.group(3) == CONDUCTOR_TYPE else "dielectric",
                "thickness_mm": float(FIELD_RE["thickness"].search(line).group(1)),
                "material": FIELD_RE["material"].search(line).group(1),
                "fill": FIELD_RE["fill"].search(line).group(1),
            }
        )

    materials: dict[str, dict] = {}
    for match in re.finditer(
        r"\$begin 'Conductor'(.*?)\$end 'Conductor'", text, flags=re.DOTALL
    ):
        block = match.group(1)
        name = re.search(r"Name='([^']*)'", block).group(1)
        conductivity = re.search(r"Conductivity=([0-9.eE+-]+)", block)
        if conductivity is None:
            raise ValueError(f"STK conductor {name!r} has no Conductivity")
        permeability = re.search(r"Permeability=([0-9.eE+-]+)", block)
        materials[name] = {
            "kind": "conductor",
            "conductivity": float(conductivity.group(1)),
            "permeability": (
                float(permeability.group(1)) if permeability is not None else 1.0
            ),
        }
    for m in re.finditer(
        r"\$begin 'Insulator'(.*?)\$end 'Insulator'", text, flags=re.DOTALL
    ):
        block = m.group(1)
        name = re.search(r"Name='([^']*)'", block).group(1)
        entry = {
            "kind": "dielectric",
            "permittivity": float(re.search(r"Permittivity=([0-9.eE+-]+)", block).group(1)),
            "loss_tangent": float(re.search(r"LossTangent=([0-9.eE+-]+)", block).group(1)),
        }
        ds = re.search(
            r"\$begin 'Djordjevic-Sarkar'(.*?)\$end 'Djordjevic-Sarkar'", block, flags=re.DOTALL
        )
        if ds:
            eps_dc = re.search(r"EpsDC=([0-9.eE+-]+)", ds.group(1))
            entry["ds"] = {
                "measurement_frequency_hz": float(re.search(r"MeasurementFreq=([0-9.eE+-]+)", ds.group(1)).group(1)),
                "dc_conductivity": float(re.search(r"SigmaDC=([0-9.eE+-]+)", ds.group(1)).group(1)),
                "dc_permittivity": float(eps_dc.group(1)) if eps_dc else None,
            }
        materials[name] = entry
    return layers, materials


def default_dielectric_fill(
    index: int,
    inner_layers: list[dict],
    outer_top: list[dict],
    outer_bottom: list[dict],
) -> str | None:
    """Mirror SIWave's topology rule when DefinedDielectricFill is empty."""

    if index == 0 and not outer_top:
        return "air"
    if index == len(inner_layers) - 1 and not outer_bottom:
        return "air"
    neighbors = [
        inner_layers[neighbor]
        for neighbor in (index - 1, index + 1)
        if 0 <= neighbor < len(inner_layers)
        and inner_layers[neighbor]["kind"] == "dielectric"
    ]
    if outer_top and index == 0:
        neighbors.append(outer_top[-1])
    if outer_bottom and index == len(inner_layers) - 1:
        neighbors.append(outer_bottom[0])
    if not neighbors:
        return None
    return min(neighbors, key=lambda layer: layer["thickness_mm"])["material"]


def apply_stk(source_aedb: Path, target_aedb: Path, stk_path: Path, edb_version: str) -> dict:
    stk_layers, stk_materials = parse_stk(stk_path)
    conductors = [l for l in stk_layers if l["kind"] == "conductor"]
    first_cond = stk_layers.index(conductors[0])
    last_cond = stk_layers.index(conductors[-1])
    inner = stk_layers[first_cond : last_cond + 1]
    outer_top = stk_layers[:first_cond]
    outer_bottom = stk_layers[last_cond + 1 :]

    if target_aedb.exists():
        shutil.rmtree(target_aedb)
    shutil.copytree(source_aedb, target_aedb)

    record: dict = {"materials": [], "layers": [], "addedLayers": []}
    Edb = _edb_class()
    edb = Edb(edbpath=str(target_aedb), edbversion=edb_version)
    try:
        # 1. Materials with Djordjevic-Sarkar model (validated stkfix mechanics).
        for name, props in stk_materials.items():
            if name in edb.materials.materials:
                continue
            if props["kind"] == "conductor":
                edb.materials.add_conductor_material(
                    name,
                    props["conductivity"],
                    permeability=props["permeability"],
                )
                record["materials"].append({"name": name, **props})
                continue
            ds = props.get("ds") or {}
            material = edb.materials.add_djordjevicsarkar_dielectric(
                name,
                props["permittivity"],
                props["loss_tangent"],
                (ds.get("measurement_frequency_hz") or 1e9) / 1e9,
                dc_conductivity=ds.get("dc_conductivity"),
            )
            if ds.get("dc_permittivity") is not None:
                dc_model = material.dc_model
                dc_model.SetUseDCRelativePermitivity(True)
                dc_model.SetDCRelativePermitivity(ds["dc_permittivity"])
            record["materials"].append({"name": name, **props})

        # 2. Map STK inner span onto the existing stackup by order and kind.
        edb_layers = list(edb.stackup.stackup_layers.values())
        edb_kinds = ["conductor" if str(l.type) == "signal" else "dielectric" for l in edb_layers]
        stk_kinds = [l["kind"] for l in inner]
        if edb_kinds != stk_kinds:
            raise RuntimeError(
                f"stackup shape mismatch: edb={edb_kinds} stk(inner)={stk_kinds}"
            )
        for index, (edb_layer, stk_layer) in enumerate(zip(edb_layers, inner)):
            edb_layer.thickness = stk_layer["thickness_mm"] / 1000.0
            if stk_layer["kind"] == "dielectric":
                edb_layer.material = stk_layer["material"]
            else:
                if stk_layer["material"]:
                    edb_layer.material = stk_layer["material"]
                fill = stk_layer["fill"] or default_dielectric_fill(
                    index, inner, outer_top, outer_bottom
                )
                if fill:
                    edb_layer.dielectric_fill = fill
            record["layers"].append(
                {
                    "edbLayer": edb_layer.name,
                    "stkLayer": stk_layer["name"],
                    "thickness_m": stk_layer["thickness_mm"] / 1000.0,
                    "material": stk_layer["material"] if stk_layer["kind"] == "dielectric" else stk_layer["fill"] or None,
                }
            )

        # 3. Outer dielectrics (solder resist) that the native STK import cannot carry.
        for stk_layer in reversed(outer_top):
            edb.stackup.add_layer_top(
                stk_layer["name"],
                layer_type="dielectric",
                thickness=f"{stk_layer['thickness_mm']}mm",
                material=stk_layer["material"],
            )
            record["addedLayers"].append({"name": stk_layer["name"], "position": "top"})
        for stk_layer in outer_bottom:
            edb.stackup.add_layer_bottom(
                stk_layer["name"],
                layer_type="dielectric",
                thickness=f"{stk_layer['thickness_mm']}mm",
                material=stk_layer["material"],
            )
            record["addedLayers"].append({"name": stk_layer["name"], "position": "bottom"})

        edb.save()
        record["resultLayers"] = {
            n: {"thickness": l.thickness, "material": l.material}
            for n, l in edb.stackup.stackup_layers.items()
        }
    finally:
        edb.close_edb()

    record.update({"sourceAedb": str(source_aedb), "targetAedb": str(target_aedb), "stk": str(stk_path)})
    return record
