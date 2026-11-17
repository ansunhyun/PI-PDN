import re


def normalize_aedt_version(value):
    """Return the canonical PyAEDT version form, for example ``2026.1``."""
    text = str(value or "").strip().upper()
    if not text:
        raise ValueError("AEDT version is empty")

    compact = re.sub(r"[^0-9]", "", text)
    if len(compact) == 5 and compact.startswith("20"):
        year = int(compact[:4])
        release = int(compact[4])
    elif len(compact) == 4 and compact.startswith("20"):
        year = int(compact)
        release = 1
    elif len(compact) == 3 and compact[:2].isdigit():
        year = 2000 + int(compact[:2])
        release = int(compact[2])
    else:
        raise ValueError(f"Unsupported AEDT version format: {value!r}")

    if year < 2024 or release not in (1, 2):
        raise ValueError(f"Unsupported AEDT version: {value!r}")
    return f"{year}.{release}"


def aedt_env_suffix(value):
    """Return the ANSYSEM_ROOT suffix (``2026.1`` -> ``261``)."""
    year, release = normalize_aedt_version(value).split(".")
    return f"{year[-2:]}{release}"


def aedt_display_version(value):
    """Return the human-readable form (``2026 R1``)."""
    year, release = normalize_aedt_version(value).split(".")
    return f"{year} R{release}"
