"""
Config storage for fs-shell-mcp.

Persisted at ~/.config/fs-shell-mcp/config.json

{
  "allowed_roots": ["/home/user/projects"],   // paths always allowed for file ops, no prompt
  "always_allow_commands": false,              // if true, run_command never prompts
  "trusted_command_roots": []                  // cwds where commands run without prompting
}
"""
import json
import os
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "fs-shell-mcp"
CONFIG_FILE = CONFIG_DIR / "config.json"

DEFAULT_CONFIG = {
    "allowed_roots": [],
    "always_allow_commands": False,
    "trusted_command_roots": [],
}


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        save_config(DEFAULT_CONFIG)
        return dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_FILE, "r") as f:
            data = json.load(f)
        merged = dict(DEFAULT_CONFIG)
        merged.update(data)
        return merged
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


def add_allowed_root(path: str) -> None:
    cfg = load_config()
    norm = str(Path(path).expanduser().resolve())
    if norm not in cfg["allowed_roots"]:
        cfg["allowed_roots"].append(norm)
        save_config(cfg)


def add_trusted_command_root(path: str) -> None:
    cfg = load_config()
    norm = str(Path(path).expanduser().resolve())
    if norm not in cfg["trusted_command_roots"]:
        cfg["trusted_command_roots"].append(norm)
        save_config(cfg)


def is_under_any(path: str, roots: list) -> bool:
    try:
        p = Path(path).expanduser().resolve()
    except OSError:
        return False
    for r in roots:
        try:
            rp = Path(r).expanduser().resolve()
            if p == rp or rp in p.parents:
                return True
        except OSError:
            continue
    return False
