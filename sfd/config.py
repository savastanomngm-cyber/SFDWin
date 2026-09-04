"""SFD Chart Terminal — config loader (patched for Windows).

Fixes:
1. Loads flowdesk.yaml relative to THIS FILE (not the working directory),
   so `python -m sfd.server` works from any folder.
2. Explicit UTF-8 read — fixes the cp1254/Turkish-locale crash on
   emoji/special characters written on macOS.
"""
import yaml
from pathlib import Path

CONFIG_PATH = Path(__file__).resolve().parent / "flowdesk.yaml"


class ConfigError(Exception):
    pass


def load() -> dict:
    if not CONFIG_PATH.exists():
        raise ConfigError(f"Missing {CONFIG_PATH}")
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    if cfg is None:
        cfg = {}
    _validate(cfg)
    return cfg


def _validate(cfg):
    if not isinstance(cfg, dict):
        raise ConfigError("flowdesk.yaml: top level must be a mapping")
    cfg.setdefault("settings", {})
    if not isinstance(cfg.get("settings"), dict):
        raise ConfigError("flowdesk.yaml: 'settings' must be a mapping")