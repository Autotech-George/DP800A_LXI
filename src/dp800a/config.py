"""Persistent JSON-backed configuration."""
from __future__ import annotations

import json
import os
from pathlib import Path
from threading import Lock

from .models import AppConfig

_LOCK = Lock()


def config_path() -> Path:
    base = os.environ.get("DP800A_CONFIG_DIR")
    if base:
        return Path(base) / "config.json"
    return Path.home() / ".dp800a" / "config.json"


def load_config() -> AppConfig:
    path = config_path()
    if not path.exists():
        return AppConfig()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return AppConfig.model_validate(data)
    except Exception:
        return AppConfig()


def save_config(cfg: AppConfig) -> None:
    path = config_path()
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(cfg.model_dump_json(indent=2), encoding="utf-8")
