"""Export and import of the app's settings (the options on the Configuration tab).

Pure functions: `export_doc` builds the file, `prepare` turns an imported file into the
options to save and the list of what changes. Saving goes through the Supervisor (`App.save_options`).
"""
from __future__ import annotations

from dataclasses import fields
from datetime import datetime

from .config import OVERRIDE_OPTION, ConfigError, Options, from_dict

APP = "battery_pilot"
OVERRIDES = frozenset(OVERRIDE_OPTION.values())       # entity names: optional, left out when empty
DEFAULTS = {f.name: getattr(Options(), f.name) for f in fields(Options) if f.name != "entities"}
KNOWN = frozenset(DEFAULTS) | OVERRIDES


def export_doc(options: dict, version: str, now: datetime) -> dict:
    return {"app": APP, "version": version, "exported": now.isoformat(timespec="seconds"),
            "options": {k: v for k, v in options.items() if k in KNOWN}}


def _typed(key: str, value):
    """The value in the type the setting has (the Supervisor checks types strictly)."""
    want = "" if key in OVERRIDES else DEFAULTS[key]
    number = isinstance(value, (int, float)) and not isinstance(value, bool)
    if isinstance(want, bool):
        if isinstance(value, bool):
            return value
        raise ConfigError(f"{key} must be true or false, not {value!r}.")
    if isinstance(want, int):
        if number and float(value).is_integer():
            return int(value)
        raise ConfigError(f"{key} must be a whole number, not {value!r}.")
    if isinstance(want, float):
        if number:
            return value
        raise ConfigError(f"{key} must be a number, not {value!r}.")
    if isinstance(value, str):
        return value.strip() if key in OVERRIDES else value
    raise ConfigError(f"{key} must be text, not {value!r}.")


def prepare(raw, current: dict) -> tuple[dict, list[dict], list[str]]:
    """(options to save, changes [{key, old, new}], ignored keys) for an imported file.

    `raw` is an export file or a bare options object. A setting the file does not have keeps its
    current value, so a file from an older version still works. Raises ConfigError when the file
    or a value in it is not usable; nothing is saved then.
    """
    if isinstance(raw, dict) and isinstance(raw.get("options"), dict):
        raw = raw["options"]
    if not isinstance(raw, dict):
        raise ConfigError("this is not a Battery Pilot settings file.")
    ignored = sorted(str(k) for k in raw if k not in KNOWN)
    new = {k: _typed(k, v) for k, v in raw.items() if k in KNOWN and v is not None}
    if not new:
        raise ConfigError("this file holds no Battery Pilot settings.")
    merged = {**current, **new}
    merged = {k: v for k, v in merged.items() if not (k in OVERRIDES and not str(v or "").strip())}
    try:
        from_dict(merged)
    except ConfigError:
        raise
    except Exception as e:  # noqa: BLE001 - a value of the wrong kind in the saved settings
        raise ConfigError(f"these settings cannot be used ({e}).") from None

    def shown(d, k):
        v = d.get(k)
        return None if k in OVERRIDES and not str(v or "").strip() else v

    changes = [{"key": k, "old": shown(current, k), "new": shown(merged, k)}
               for k in sorted(set(current) | set(merged)) if shown(current, k) != shown(merged, k)]
    return merged, changes, ignored
