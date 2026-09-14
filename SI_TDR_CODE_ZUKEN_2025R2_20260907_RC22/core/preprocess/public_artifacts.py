"""Public-only transformations; original solver artifacts are never overwritten."""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

JPEG_SETTINGS = {"quality": 95, "subsampling": 0, "optimize": False, "progressive": False}
REFERENCE_ARCHIVE_SCHEMA = "si-tdr-reference-archive/1"


def file_identity(path: Path) -> dict[str, Any]:
    stat = path.stat()
    if not path.is_file() or stat.st_size <= 0:
        raise ValueError(f"missing/empty artifact: {path}")
    return {"path": str(path.resolve()), "size": stat.st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "mtimeNs": stat.st_mtime_ns}


def jpeg_bytes(source: Path) -> tuple[bytes, dict[str, Any]]:
    from PIL import Image, __version__ as pillow_version

    before = file_identity(source)
    with Image.open(source) as image:
        if image.format != "PNG" or image.mode != "RGB":
            raise ValueError(f"public JPEG source must be a validated RGB PNG: {source}")
        image.load()
        size = image.size
        output = io.BytesIO()
        image.save(output, format="JPEG", **JPEG_SETTINGS)
    data = output.getvalue()
    with Image.open(io.BytesIO(data)) as decoded:
        decoded.load()
        if decoded.format != "JPEG" or decoded.size != size or decoded.mode != "RGB":
            raise ValueError("public JPEG failed decoding/dimension validation")
    if file_identity(source) != before:
        raise ValueError("public JPEG source changed during conversion")
    return data, {"schema": "si-tdr-public-jpeg/1", "source": before,
                  "pillowVersion": pillow_version, "settings": dict(JPEG_SETTINGS),
                  "width": size[0], "height": size[1], "mode": "RGB",
                  "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}


def create_reference_archive(*, reference_siw: Path, reference_aedb: Path,
                             directory: Path, executable: Path,
                             runner=subprocess.run) -> Path:
    """Run documented SaveArchive on a private copy, without a solve command."""
    source = file_identity(reference_siw)
    if not reference_aedb.is_dir() or not (reference_aedb / "edb.def").is_file():
        raise ValueError("reference AEDB is incomplete")
    directory.mkdir(parents=True, exist_ok=False)
    private_siw = directory / reference_siw.name
    shutil.copy2(reference_siw, private_siw)
    shutil.copytree(reference_aedb, directory / reference_aedb.name)
    archive = private_siw.with_suffix(".siwz")
    exec_path = directory / "archive.exec"
    exec_path.write_text(f'SaveArchive "{archive.resolve()}"\n', encoding="utf-8")
    command = [str(executable.resolve()), str(private_siw.resolve()), str(exec_path.resolve()),
               "-formatOutput", "-useSubdir"]
    result = runner(command, cwd=directory, capture_output=True, text=True, check=False)
    record = {"schema": REFERENCE_ARCHIVE_SCHEMA, "status": "error", "source": source,
              "command": command, "returnCode": result.returncode,
              "stdout": result.stdout, "stderr": result.stderr}
    try:
        if result.returncode != 0:
            raise ValueError(f"reference SaveArchive failed: {result.returncode}")
        if file_identity(reference_siw) != source:
            raise ValueError("reference source changed during archive creation")
        record["archive"] = file_identity(archive)
        record["status"] = "created"
        return archive
    finally:
        (directory / "reference_archive.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")


def validate_reference_archive(reference_siw: Path, directory: Path) -> Path:
    record = json.loads((directory / "reference_archive.json").read_text(encoding="utf-8"))
    archive = directory / reference_siw.with_suffix(".siwz").name
    if (not isinstance(record, dict) or record.get("schema") != REFERENCE_ARCHIVE_SCHEMA
            or record.get("status") != "created" or record.get("returnCode") != 0
            or record.get("source") != file_identity(reference_siw)
            or record.get("archive") != file_identity(archive)):
        raise ValueError("reference archive record/source/artifact mismatch")
    return archive


def validate_stackup_xml(path: Path) -> None:
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ValueError("malformed stackup XML") from exc
    if root.tag.rsplit("}", 1)[-1] != "Control":
        raise ValueError("stackup.xml must be an Ansys stackup control XML")
    layers = [e for e in root.iter() if e.tag.rsplit("}", 1)[-1] == "Layer"]
    if not layers or any(not e.get("Name") for e in layers):
        raise ValueError("stackup.xml has no named layers")
    if len({e.get("Name") for e in layers}) != len(layers):
        raise ValueError("stackup.xml has duplicate layer names")


def export_public_stackup(reference_aedb: Path, directory: Path, version: str, *, edb_factory=None) -> Path:
    """Export the applied stackup from a private AEDB, never recreate it from guessed STK fields."""
    if edb_factory is None:
        from pyedb import Edb
        edb_factory = Edb
    source = file_identity(reference_aedb / "edb.def")
    private = directory / reference_aedb.name
    if private.resolve() == reference_aedb.resolve():
        raise ValueError("stackup export must use a private AEDB")
    if not (private / "edb.def").is_file():
        raise ValueError("stackup export private AEDB is missing")
    destination = directory / "stackup.xml"
    if destination.exists():
        raise ValueError("stale stackup XML exists")
    edb = edb_factory(str(private), edbversion=version, isreadonly=True)
    try:
        operation = getattr(edb.stackup, "export", None)
        if not callable(operation) or operation(str(destination), file_format="xml") is not True:
            raise ValueError("PyEDB stackup.export XML failed or unavailable")
    finally:
        edb.close_edb()
    validate_stackup_xml(destination)
    if source != file_identity(reference_aedb / "edb.def"):
        raise ValueError("reference AEDB changed during stackup export")
    record_path = directory / "reference_archive.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    record["stackupXml"] = {"sourceEdb": source, "artifact": file_identity(destination),
                            "api": "PyEDB.Stackup.export", "aedtVersion": version}
    record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return destination


def validate_public_stackup(reference_aedb: Path, directory: Path) -> Path:
    record = json.loads((directory / "reference_archive.json").read_text(encoding="utf-8"))
    evidence = record.get("stackupXml")
    path = directory / "stackup.xml"
    if (not isinstance(evidence, dict) or evidence.get("api") != "PyEDB.Stackup.export"
            or evidence.get("sourceEdb") != file_identity(reference_aedb / "edb.def")
            or evidence.get("artifact") != file_identity(path)):
        raise ValueError("stackup XML/source AEDB evidence differs")
    validate_stackup_xml(path)
    return path
