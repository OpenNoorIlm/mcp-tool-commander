"""
Interactive permission gating for fs-shell-mcp.

Every action that touches the filesystem outside an allowed root, or that
runs a shell command, pops a real GUI dialog (zenity) on the user's Ubuntu
desktop. Nothing happens until the user clicks a button. This is what makes
"the AI has full access, but the user has to grant it" true in practice.
"""
import shutil
import subprocess
from pathlib import Path

import config as cfg_mod

# In-memory grants that last only for this server process's lifetime.
_session_file_roots: set[str] = set()
_session_commands_allowed = False

ZENITY_MISSING_MSG = (
    "zenity is not installed, so permission dialogs cannot be shown. "
    "Install it with: sudo apt install zenity\n"
    "Until then, all actions requiring confirmation are DENIED for safety."
)


def _zenity_available() -> bool:
    return shutil.which("zenity") is not None


def _ask(title: str, text: str) -> str:
    """
    Show a radiolist dialog with 4 choices. Returns one of:
    'once', 'session', 'always', 'deny'
    """
    if not _zenity_available():
        return "deny"

    options = [
        ("TRUE", "once", "Allow just this once"),
        ("FALSE", "session", "Allow for the rest of this session"),
        ("FALSE", "always", "Always allow (remember permanently)"),
        ("FALSE", "deny", "Deny"),
    ]
    cmd = [
        "zenity", "--list", "--radiolist",
        "--title", title,
        "--text", text,
        "--width", "520", "--height", "260",
        "--column", "", "--column", "key", "--column", "Choice",
        "--hide-column", "2",
        "--print-column", "2",
    ]
    for selected, key, label in options:
        cmd += [selected, key, label]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, OSError):
        return "deny"

    choice = result.stdout.strip()
    if result.returncode != 0 or choice not in {"once", "session", "always", "deny"}:
        return "deny"
    return choice


def notify(message: str) -> None:
    """Best-effort desktop notification, non-blocking, no permission needed."""
    if shutil.which("notify-send"):
        try:
            subprocess.run(["notify-send", "fs-shell-mcp", message], timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass


def request_file_permission(action: str, path: str) -> tuple[bool, str]:
    """
    Ask permission for a file-system write/edit/create/delete action on `path`.
    Returns (allowed, message).
    """
    cfg = cfg_mod.load_config()
    resolved = str(Path(path).expanduser().resolve())

    if cfg_mod.is_under_any(resolved, cfg["allowed_roots"]):
        return True, "allowed (whitelisted root)"
    if resolved in _session_file_roots or cfg_mod.is_under_any(resolved, list(_session_file_roots)):
        return True, "allowed (session grant)"

    if not _zenity_available():
        return False, ZENITY_MISSING_MSG

    choice = _ask(
        "MCP Permission Request",
        f"An AI assistant wants to:\n\n  {action}\n\nPath: {resolved}\n\nAllow?",
    )

    if choice == "once":
        return True, "allowed once"
    if choice == "session":
        _session_file_roots.add(resolved)
        return True, "allowed for this session"
    if choice == "always":
        cfg_mod.add_allowed_root(resolved if Path(resolved).is_dir() else str(Path(resolved).parent))
        return True, "allowed permanently (root whitelisted)"
    return False, "denied by user"


def request_command_permission(command: str, cwd: str) -> tuple[bool, str]:
    """
    Ask permission to run a shell command. Returns (allowed, message).
    """
    global _session_commands_allowed
    cfg = cfg_mod.load_config()
    resolved_cwd = str(Path(cwd).expanduser().resolve())

    if cfg.get("always_allow_commands"):
        return True, "allowed (commands always-allow is enabled in config)"
    if _session_commands_allowed:
        return True, "allowed (session grant for commands)"
    if cfg_mod.is_under_any(resolved_cwd, cfg.get("trusted_command_roots", [])):
        return True, "allowed (trusted command root)"

    if not _zenity_available():
        return False, ZENITY_MISSING_MSG

    choice = _ask(
        "MCP Permission Request \u2014 Run Command",
        f"An AI assistant wants to run a shell command:\n\n  {command}\n\nWorking directory: {resolved_cwd}\n\nAllow?",
    )

    if choice == "once":
        return True, "allowed once"
    if choice == "session":
        _session_commands_allowed = True
        return True, "allowed for this session (all future commands)"
    if choice == "always":
        cfg_mod.add_trusted_command_root(resolved_cwd)
        return True, f"allowed permanently for commands run in {resolved_cwd}"
    return False, "denied by user"
