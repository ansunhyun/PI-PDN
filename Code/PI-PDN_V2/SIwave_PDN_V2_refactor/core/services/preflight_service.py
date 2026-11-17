from pathlib import Path

from core.SettingsManager import SettingsManager
from core.database import InValChk
from core.logger import LogLevel


def _apply_spec_fallback(settings_manager: SettingsManager, input_dir: Path) -> None:
    original_spec_name = settings_manager.data.get("CAE", {}).get("SOC", {}).get("Spec", "")
    if not original_spec_name:
        return
    primary_spec_path = input_dir / original_spec_name
    if primary_spec_path.exists():
        return
    parts = original_spec_name.rsplit("_", 1)
    base_name = parts[0] if len(parts) == 2 else original_spec_name.rsplit(".", 1)[0]
    fallback_spec_name = f"{base_name}_reference.csv"
    settings_manager.data["CAE"]["SOC"]["Spec"] = fallback_spec_name


def _apply_bom_fallback(settings_manager: SettingsManager, input_dir: Path, logger) -> None:
    original_bom_name = settings_manager.data.get("CAE", {}).get("PCB", {}).get("BOM", "")
    if not original_bom_name:
        return

    primary_bom_path = input_dir / original_bom_name
    if primary_bom_path.exists():
        return

    # Fallback 1: filename-only lookup (ignore stale nested folders in JSON path)
    flat_bom_path = input_dir / Path(original_bom_name).name
    if flat_bom_path.exists():
        logger.log(
            f"Primary BOM '{original_bom_name}' not found. Fallback to '{flat_bom_path.name}'",
            level=LogLevel.WARNING,
        )
        settings_manager.data["CAE"]["PCB"]["BOM"] = flat_bom_path.name
        return

    if primary_bom_path.suffix.lower() == ".csv":
        # Fallback 2: same folder, alternate extension
        for ext in [".xlsx", ".xls"]:
            fallback_bom_path = primary_bom_path.with_suffix(ext)
            if fallback_bom_path.exists():
                settings_manager.data["CAE"]["PCB"]["BOM"] = original_bom_name.rsplit(".", 1)[0] + ext
                return

        # Fallback 3: filename-only + alternate extension
        flat_base = flat_bom_path.with_suffix("")
        for ext in [".csv", ".xlsx", ".xls"]:
            alt = flat_base.with_suffix(ext)
            if alt.exists():
                logger.log(
                    f"Primary BOM '{original_bom_name}' not found. Fallback to '{alt.name}'",
                    level=LogLevel.WARNING,
                )
                settings_manager.data["CAE"]["PCB"]["BOM"] = alt.name
                return


def load_and_validate_settings(input_json: Path, conf_manager, input_dir: Path, logger):
    settings_manager = SettingsManager(input_json, configuration=conf_manager, logger=logger)
    settings_manager.data.setdefault("CAE", {})
    settings_manager.data["CAE"].setdefault("PCB", {})
    settings_manager.data["CAE"].setdefault("SOC", {})

    _apply_spec_fallback(settings_manager, input_dir)
    _apply_bom_fallback(settings_manager, input_dir, logger)

    input_valchk = InValChk(settings_manager.data, input_dir, logger)
    default, optional = input_valchk.is_valid()

    settings_manager.data["CAE"]["PCB"].update(
        {"cadFile": default["cadFile"], "Stackup": default["Stackup"], "BOM": default["BOM"]}
    )
    settings_manager.data["CAE"]["SOC"].update({"Spec": default["Spec"], "Inner_cap": optional["Inner_cap"]})
    return settings_manager, input_valchk
