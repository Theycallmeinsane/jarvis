"""Config loading: config.yaml + optional .env, with attribute-style access."""
from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent


class Section(dict):
    """dict with attribute access; nested dicts become Sections."""

    def __getattr__(self, key: str) -> Any:
        try:
            value = self[key]
        except KeyError as e:
            raise AttributeError(key) from e
        return Section(value) if isinstance(value, dict) and not isinstance(value, Section) else value

    def get_section(self, key: str) -> "Section":
        return Section(self.get(key) or {})


def resolve_path(p: str | os.PathLike) -> Path:
    """Expand ~ and make relative paths relative to the project root."""
    path = Path(os.path.expanduser(str(p)))
    return path if path.is_absolute() else ROOT / path


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | os.PathLike | None = None) -> Section:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    cfg_path = Path(path) if path else ROOT / "config.yaml"
    with open(cfg_path, encoding="utf-8-sig") as f:  # -sig: tolerate files saved by Notepad/PowerShell
        data = yaml.safe_load(f) or {}

    # config.local.yaml (git-ignored) lets you override settings without editing the default file
    local = cfg_path.with_name("config.local.yaml")
    if local.exists():
        with open(local, encoding="utf-8-sig") as f:
            data = _deep_merge(data, yaml.safe_load(f) or {})
    return Section(data)
