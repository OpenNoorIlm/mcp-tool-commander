# SPDX-License-Identifier: MIT
# Copyright (c) 2025 Dev
# See LICENSE file for full license text.

import os as _os
"""
fs-shell-mcp: an MCP server giving an AI assistant (Claude, LM Studio, etc.)
the ability to run shell commands and read/search/write/edit files and
folders on this machine -- gated by real permission dialogs the user must
approve.

Run manually:
    python3 server.py

Configure in your MCP client (Claude Desktop / LM Studio) to launch this
via stdio -- see README.md.
"""
import fnmatch
import subprocess
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import requests
from bs4 import BeautifulSoup
from mcp.server.fastmcp import FastMCP

import permissions

mcp = FastMCP("fs-shell-mcp")

EXCLUDE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".cache", "dist", "build"}

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 fs-shell-mcp/1.0"
    )
}


def _fmt_denied(msg: str) -> str:
    return f"DENIED: {msg}"


# ---------------------------------------------------------------------------
# Shell commands
# ---------------------------------------------------------------------------

@mcp.tool()
def run_command(command: str, cwd: str = ".", timeout: int = 60) -> str:
    """
    Run a shell command on the user's machine. Requires user permission via
    a desktop popup unless the user has pre-trusted this working directory
    or enabled always-allow. Returns stdout, stderr, and the exit code.

    Args:
        command: the shell command to execute (runs via `sh -c`)
        cwd: working directory to run the command in (default: current dir)
        timeout: max seconds to allow the command to run (default 60)
    """
    resolved_cwd = str(Path(cwd).expanduser().resolve())
    allowed, msg = permissions.request_command_permission(command, resolved_cwd)
    if not allowed:
        return _fmt_denied(msg)

    try:
        result = subprocess.run(
            command, shell=True, cwd=resolved_cwd,
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout}s"
    except OSError as e:
        return f"ERROR: {e}"

    out = f"[permission: {msg}]\nexit code: {result.returncode}\n"
    if result.stdout:
        out += f"--- stdout ---\n{result.stdout}\n"
    if result.stderr:
        out += f"--- stderr ---\n{result.stderr}\n"
    return out


# ---------------------------------------------------------------------------
# File reading
# ---------------------------------------------------------------------------

@mcp.tool()
def read_file(path: str, max_bytes: int = 200_000) -> str:
    """
    Read a text file's contents. Read-only; no permission prompt required.

    Args:
        path: path to the file
        max_bytes: truncate file content to this many bytes if larger
    """
    p = Path(path).expanduser()
    if not p.exists():
        return f"ERROR: file not found: {p}"
    if not p.is_file():
        return f"ERROR: not a file: {p}"
    try:
        data = p.read_bytes()
    except OSError as e:
        return f"ERROR: {e}"
    truncated = len(data) > max_bytes
    data = data[:max_bytes]
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception as e:
        return f"ERROR decoding file: {e}"
    if truncated:
        text += f"\n\n[... truncated, file exceeds {max_bytes} bytes ...]"
    return text


@mcp.tool()
def list_dir(path: str = ".") -> str:
    """
    List files and folders inside a directory (non-recursive). Read-only;
    no permission prompt required.

    Args:
        path: directory to list
    """
    p = Path(path).expanduser()
    if not p.exists():
        return f"ERROR: path not found: {p}"
    if not p.is_dir():
        return f"ERROR: not a directory: {p}"
    entries = []
    try:
        for child in sorted(p.iterdir()):
            kind = "DIR " if child.is_dir() else "FILE"
            size = "" if child.is_dir() else f" ({child.stat().st_size}B)"
            entries.append(f"{kind}  {child.name}{size}")
    except OSError as e:
        return f"ERROR: {e}"
    return "\n".join(entries) if entries else "(empty directory)"


# ---------------------------------------------------------------------------
# File writing / creation / editing (permission-gated)
# ---------------------------------------------------------------------------

@mcp.tool()
def create_file(path: str, content: str = "") -> str:
    """
    Create a new file with the given content. Fails if the file already
    exists (use write_file to overwrite, or edit_file to modify). Requires
    user permission via a desktop popup unless the target folder is
    pre-whitelisted.

    Args:
        path: path of the new file to create
        content: text content to write into the file
    """
    p = Path(path).expanduser()
    if p.exists():
        return f"ERROR: file already exists: {p} (use write_file to overwrite or edit_file to modify)"

    allowed, msg = permissions.request_file_permission(f"Create new file: {p.name}", str(p.parent))
    if not allowed:
        return _fmt_denied(msg)

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except OSError as e:
        return f"ERROR: {e}"
    return f"[permission: {msg}]\nCreated {p} ({len(content)} chars)"


@mcp.tool()
def write_file(path: str, content: str, overwrite: bool = True) -> str:
    """
    Write content to a file, creating it if needed. If overwrite is False
    and the file exists, this fails instead of clobbering it. Requires user
    permission via a desktop popup unless pre-whitelisted.

    Args:
        path: path of the file to write
        content: text content to write
        overwrite: if False, refuse to overwrite an existing file
    """
    p = Path(path).expanduser()
    if p.exists() and not overwrite:
        return f"ERROR: file already exists and overwrite=False: {p}"

    action = f"Overwrite file: {p.name}" if p.exists() else f"Create file: {p.name}"
    allowed, msg = permissions.request_file_permission(action, str(p))
    if not allowed:
        return _fmt_denied(msg)

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    except OSError as e:
        return f"ERROR: {e}"
    return f"[permission: {msg}]\nWrote {len(content)} chars to {p}"


@mcp.tool()
def edit_file(path: str, old_str: str, new_str: str, replace_all: bool = False) -> str:
    """
    Find-and-replace text within an existing file. By default old_str must
    appear exactly once in the file (to avoid ambiguous edits); set
    replace_all=True to replace every occurrence. Requires user permission
    via a desktop popup unless pre-whitelisted.

    Args:
        path: path to the file to edit
        old_str: exact text to find
        new_str: text to replace it with
        replace_all: replace every occurrence instead of requiring exactly one
    """
    p = Path(path).expanduser()
    if not p.exists() or not p.is_file():
        return f"ERROR: file not found: {p}"

    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        return f"ERROR: {e}"

    count = text.count(old_str)
    if count == 0:
        return f"ERROR: old_str not found in {p}"
    if count > 1 and not replace_all:
        return f"ERROR: old_str appears {count} times in {p}; pass replace_all=True or make old_str more specific"

    allowed, msg = permissions.request_file_permission(f"Edit file: {p.name}", str(p))
    if not allowed:
        return _fmt_denied(msg)

    new_text = text.replace(old_str, new_str) if replace_all else text.replace(old_str, new_str, 1)
    try:
        p.write_text(new_text, encoding="utf-8")
    except OSError as e:
        return f"ERROR: {e}"
    replaced = count if replace_all else 1
    return f"[permission: {msg}]\nReplaced {replaced} occurrence(s) in {p}"


@mcp.tool()
def create_folder(path: str) -> str:
    """
    Create a folder (and any missing parent folders). Requires user
    permission via a desktop popup unless pre-whitelisted.

    Args:
        path: path of the folder to create
    """
    p = Path(path).expanduser()
    if p.exists():
        return f"Folder already exists: {p}" if p.is_dir() else f"ERROR: path exists and is a file: {p}"

    allowed, msg = permissions.request_file_permission(f"Create folder: {p}", str(p.parent))
    if not allowed:
        return _fmt_denied(msg)

    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        return f"ERROR: {e}"
    return f"[permission: {msg}]\nCreated folder {p}"


@mcp.tool()
def append_file(path: str, content: str) -> str:
    """
    Append content to the end of an existing file (creates it if missing).
    Requires user permission via a desktop popup unless pre-whitelisted.

    Args:
        path: path of the file to append to
        content: text content to append
    """
    p = Path(path).expanduser()
    if p.exists() and not p.is_file():
        return f"ERROR: not a file: {p}"

    action = f"Append to file: {p.name}" if p.exists() else f"Create file: {p.name}"
    allowed, msg = permissions.request_file_permission(action, str(p))
    if not allowed:
        return _fmt_denied(msg)

    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        return f"ERROR: {e}"
    return f"[permission: {msg}]\nAppended {len(content)} chars to {p}"


@mcp.tool()
def delete_file(path: str) -> str:
    """
    Delete a file (not a directory - use delete_folder-style handling via
    run_command for directories, this is file-only for safety). Requires
    user permission via a desktop popup unless pre-whitelisted.

    Args:
        path: path of the file to delete
    """
    p = Path(path).expanduser()
    if not p.exists():
        return f"ERROR: file not found: {p}"
    if not p.is_file():
        return f"ERROR: not a file (refusing to delete directories with this tool): {p}"

    allowed, msg = permissions.request_file_permission(f"Delete file: {p.name}", str(p))
    if not allowed:
        return _fmt_denied(msg)

    try:
        p.unlink()
    except OSError as e:
        return f"ERROR: {e}"
    return f"[permission: {msg}]\nDeleted {p}"


@mcp.tool()
def move_file(source: str, destination: str, overwrite: bool = False) -> str:
    """
    Move or rename a file from source to destination. Fails if destination
    already exists unless overwrite=True. Requires user permission via a
    desktop popup (checked against the destination path) unless
    pre-whitelisted.

    Args:
        source: path of the file to move
        destination: path to move/rename it to
        overwrite: if True, replace an existing file at destination
    """
    src = Path(source).expanduser()
    dst = Path(destination).expanduser()
    if not src.exists():
        return f"ERROR: source file not found: {src}"
    if not src.is_file():
        return f"ERROR: source is not a file: {src}"
    if dst.exists() and not overwrite:
        return f"ERROR: destination already exists and overwrite=False: {dst}"

    allowed, msg = permissions.request_file_permission(f"Move file to: {dst.name}", str(dst))
    if not allowed:
        return _fmt_denied(msg)

    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.replace(dst)
    except OSError as e:
        return f"ERROR: {e}"
    return f"[permission: {msg}]\nMoved {src} -> {dst}"


# ---------------------------------------------------------------------------
# Search (read-only, no permission needed)
# ---------------------------------------------------------------------------

def _iter_files(root: Path, include_hidden: bool):
    for child in root.rglob("*"):
        if child.is_dir():
            continue
        parts = child.relative_to(root).parts
        if not include_hidden and any(part.startswith(".") for part in parts):
            continue
        if any(part in EXCLUDE_DIRS for part in parts):
            continue
        yield child


@mcp.tool()
def search(query: str, path: str = ".", case_sensitive: bool = False, max_results: int = 100) -> str:
    """
    Quick recursive text search for `query` inside files under `path`.
    Skips common noise folders (.git, node_modules, __pycache__, venv, etc.)
    and hidden files/folders. Read-only; no permission prompt required.

    Args:
        query: text to search for
        path: directory to search within (default: current directory)
        case_sensitive: whether the match should be case sensitive
        max_results: cap on number of matching lines returned
    """
    root = Path(path).expanduser()
    if not root.exists() or not root.is_dir():
        return f"ERROR: not a directory: {root}"

    needle = query if case_sensitive else query.lower()
    results = []
    for f in _iter_files(root, include_hidden=False):
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            haystack = line if case_sensitive else line.lower()
            if needle in haystack:
                results.append(f"{f}:{i}: {line.strip()}")
                if len(results) >= max_results:
                    return "\n".join(results) + f"\n\n[stopped at {max_results} results, refine your query or use search_deep]"
    return "\n".join(results) if results else "No matches found."


@mcp.tool()
def search_deep(
    query: str,
    root: str = "~",
    match_filenames: bool = True,
    include_hidden: bool = True,
    case_sensitive: bool = False,
    max_results: int = 200,
) -> str:
    """
    Thorough recursive search for `query` across a broader root (defaults to
    the user's home directory). Unlike `search`, this also matches against
    filenames by default and includes hidden files/folders. Use this when a
    quick `search` doesn't find what you're looking for. Read-only; no
    permission prompt required, but can be slow on large trees.

    Args:
        query: text to search for (in file contents and, optionally, names)
        root: root directory to search from (default: home directory)
        match_filenames: also report files whose name contains the query
        include_hidden: include dotfiles and dot-directories
        case_sensitive: whether matching is case sensitive
        max_results: cap on number of results returned
    """
    root_path = Path(root).expanduser()
    if not root_path.exists() or not root_path.is_dir():
        return f"ERROR: not a directory: {root_path}"

    needle = query if case_sensitive else query.lower()
    results = []

    for f in _iter_files(root_path, include_hidden=include_hidden):
        if match_filenames:
            name_cmp = f.name if case_sensitive else f.name.lower()
            if fnmatch.fnmatch(name_cmp, f"*{needle}*"):
                results.append(f"[name match] {f}")
                if len(results) >= max_results:
                    break
        try:
            text = f.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            haystack = line if case_sensitive else line.lower()
            if needle in haystack:
                results.append(f"{f}:{i}: {line.strip()}")
                if len(results) >= max_results:
                    break
        if len(results) >= max_results:
            break

    if not results:
        return "No matches found."
    if len(results) >= max_results:
        results.append(f"[stopped at {max_results} results, narrow your query]")
    return "\n".join(results)


# ---------------------------------------------------------------------------
# Web search / web fetch (network, read-only — no filesystem/command
# permission prompt, since these don't touch the local machine's files or
# processes. They do reach out to the network, which is the tradeoff for
# giving the assistant live web access.)
# ---------------------------------------------------------------------------

@mcp.tool()
def web_search(query: str, max_results: int = 5) -> str:
    """
    Search the web for `query` and return a list of results (title, URL,
    snippet). Uses DuckDuckGo's HTML endpoint, so no API key is required.
    Read-only; no permission prompt (network access only, not local
    filesystem or command execution).

    Args:
        query: the search query
        max_results: maximum number of results to return (default 5)
    """
    try:
        resp = requests.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers=_HTTP_HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        return f"ERROR: web search failed: {e}"

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for result in soup.select(".result"):
        link = result.select_one("a.result__a")
        if not link:
            continue
        title = link.get_text(strip=True)
        href = link.get("href", "")
        snippet_el = result.select_one(".result__snippet")
        snippet = snippet_el.get_text(strip=True) if snippet_el else ""
        results.append(f"{title}\n{href}\n{snippet}")
        if len(results) >= max_results:
            break

    if not results:
        return "No results found."
    return "\n\n".join(results)


@mcp.tool()
def web_fetch(url: str, max_bytes: int = 100_000, extract_text: bool = True) -> str:
    """
    Fetch the contents of a web page at `url`. By default strips HTML down
    to readable text (extract_text=True); set extract_text=False to get the
    raw HTML instead. Read-only; no permission prompt (network access only,
    not local filesystem or command execution). Only http(s) URLs are
    allowed.

    Args:
        url: the URL to fetch (must start with http:// or https://)
        max_bytes: truncate returned content to this many bytes if larger
        extract_text: if True, strip tags/scripts/styles and return visible text
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "ERROR: only http:// and https:// URLs are allowed"

    try:
        resp = requests.get(url, headers=_HTTP_HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        return f"ERROR: fetch failed: {e}"

    content_type = resp.headers.get("Content-Type", "")
    if extract_text and "html" in content_type:
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = "\n".join(
            line.strip() for line in soup.get_text("\n").splitlines() if line.strip()
        )
    else:
        text = resp.text

    truncated = len(text.encode("utf-8", errors="ignore")) > max_bytes
    text = text.encode("utf-8", errors="ignore")[:max_bytes].decode("utf-8", errors="ignore")
    if truncated:
        text += f"\n\n[... truncated, content exceeds {max_bytes} bytes ...]"
    return text




# ---------------------------------------------------------------------------
# KiCad schematic direct-edit tools
# These manipulate the .kicad_sch file directly (no kicad-mcp-pro needed)
# ---------------------------------------------------------------------------

import re as _re

_SCH_DEFAULT = _os.path.expanduser("~/Downloads/Ijr-X1-Core/Ijr-X1-Core.kicad_sch")


def _sch_path(sch_file: str) -> str:
    return sch_file or _SCH_DEFAULT


@mcp.tool()
def sch_delete_symbol(reference: str, sch_file: str = "") -> str:
    """
    Delete a placed symbol from the KiCad schematic by its reference
    designator (e.g. 'D1', 'R1', '#PWR01'). Edits the .kicad_sch file
    directly without needing kicad-mcp-pro.

    Args:
        reference: the reference designator to delete (e.g. 'D1')
        sch_file: path to .kicad_sch (default: Ijr-X1-Core.kicad_sch)
    """
    p = Path(_sch_path(sch_file)).expanduser()
    if not p.exists():
        return f"ERROR: schematic not found: {p}"

    allowed, msg = permissions.request_file_permission(
        f"Delete symbol {reference} from schematic", str(p)
    )
    if not allowed:
        return _fmt_denied(msg)

    text = p.read_text(encoding="utf-8")
    lines = text.split('\n')
    result_lines = []
    i = 0
    removed = 0

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        # Detect start of a placed symbol block (not a lib_symbols definition)
        if stripped == '(symbol' or (stripped.startswith('(symbol') and not stripped.startswith('(symbol "')):
            # collect the whole block
            depth = stripped.count('(') - stripped.count(')')
            buffer = [line]
            i += 1
            while i < len(lines) and depth > 0:
                buffer.append(lines[i])
                depth += lines[i].count('(') - lines[i].count(')')
                i += 1
            block = '\n'.join(buffer)
            if f'"Reference" "{reference}"' in block:
                removed += 1  # drop it
            else:
                result_lines.extend(buffer)
        else:
            result_lines.append(line)
            i += 1

    if removed == 0:
        return f"ERROR: symbol '{reference}' not found in schematic"

    p.write_text('\n'.join(result_lines), encoding="utf-8")
    return f"[permission: {msg}]\nDeleted {removed} block(s) with reference '{reference}' from {p.name}"


@mcp.tool()
def sch_delete_wire(uuid: str, sch_file: str = "") -> str:
    """
    Delete a wire from the KiCad schematic by its UUID.

    Args:
        uuid: the UUID of the wire to delete (get UUIDs from the .kicad_sch file)
        sch_file: path to .kicad_sch (default: Ijr-X1-Core.kicad_sch)
    """
    p = Path(_sch_path(sch_file)).expanduser()
    if not p.exists():
        return f"ERROR: schematic not found: {p}"

    allowed, msg = permissions.request_file_permission(
        f"Delete wire {uuid[:8]}... from schematic", str(p)
    )
    if not allowed:
        return _fmt_denied(msg)

    text = p.read_text(encoding="utf-8")
    pattern = _re.compile(
        r'\n\t\(wire\n(?:\t\t[^\n]*\n)*?\t\t\(uuid\s+"' + _re.escape(uuid) + r'"\)\n\t\)',
        _re.MULTILINE
    )
    new_text, count = pattern.subn("", text)

    if count == 0:
        return f"ERROR: wire with UUID '{uuid}' not found in schematic"

    p.write_text(new_text, encoding="utf-8")
    return f"[permission: {msg}]\nDeleted wire '{uuid}' from {p.name}"


@mcp.tool()
def sch_delete_all_symbols(sch_file: str = "") -> str:
    """
    Delete ALL placed symbols and wires from the schematic, leaving a clean
    empty sheet. Does NOT touch the lib_symbols library definitions.
    Use this to wipe test content before starting real design work.

    Args:
        sch_file: path to .kicad_sch (default: Ijr-X1-Core.kicad_sch)
    """
    p = Path(_sch_path(sch_file)).expanduser()
    if not p.exists():
        return f"ERROR: schematic not found: {p}"

    allowed, msg = permissions.request_file_permission(
        "Wipe all symbols and wires from schematic", str(p)
    )
    if not allowed:
        return _fmt_denied(msg)

    text = p.read_text(encoding="utf-8")

    # Remove all placed symbol instances — they start with (symbol\n\t\t(lib_id
    sym_pattern = _re.compile(
        r'\n\t\(symbol\n\t\t\(lib_id[\s\S]*?\n\t\)',
        _re.MULTILINE
    )
    text, sym_count = sym_pattern.subn("", text)

    # Remove all wires
    wire_pattern = _re.compile(
        r'\n\t\(wire\n[\s\S]*?\n\t\)',
        _re.MULTILINE
    )
    text, wire_count = wire_pattern.subn("", text)

    p.write_text(text, encoding="utf-8")
    return (
        f"[permission: {msg}]\n"
        f"Removed {sym_count} symbol(s) and {wire_count} wire(s). "
        f"Schematic is now clean and ready for real design."
    )


# ---------------------------------------------------------------------------
# KiCad MCP Pro bridge
# Spawns kicad-mcp-pro as a persistent stdio subprocess and forwards
# JSON-RPC calls to it, so all KiCad tools are available from this server.
# ---------------------------------------------------------------------------

import json as _json
import os as _os
import subprocess as _subprocess

_KICAD_BIN = _os.path.expanduser("~/.local/bin/kicad-mcp-pro")
_KICAD_DEFAULT_PROJECT = _os.environ.get(
    "KICAD_MCP_PROJECT_DIR",
    _os.path.expanduser("~/Downloads/Ijr-X1-Core")
)


def _kicad_session(tool_name: str, arguments: dict, project_dir: str) -> str:
    """Open a fresh kicad-mcp-pro stdio session, call one tool, return result."""
    import json, subprocess, os

    env = {
        **os.environ,
        "KICAD_MCP_PROJECT_DIR": project_dir,
        "KICAD_MCP_OPERATING_MODE": "write",
        "KICAD_CLI": "/usr/bin/kicad-cli",
        "PATH": "/usr/bin:/usr/local/bin:/home/bismillah/.local/bin:" + os.environ.get("PATH", ""),
    }

    try:
        proc = subprocess.Popen(
            [_KICAD_BIN, "--transport", "stdio",
             "--project-dir", project_dir, "--mode", "write",
             "--profile", "pcb_layout"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env
        )
    except FileNotFoundError:
        return f"ERROR: kicad-mcp-pro not found at {_KICAD_BIN}"

    def send(msg):
        try:
            proc.stdin.write(json.dumps(msg) + "\n")
            proc.stdin.flush()
        except BrokenPipeError:
            pass

    def recv():
        line = proc.stdout.readline()
        return json.loads(line.strip()) if line.strip() else None

    try:
        # 1. initialize
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                         "clientInfo": {"name": "commander-bridge", "version": "1.0"}}})
        recv()  # consume init response

        # 2. initialized notification
        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})

        # 3. actual call
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
              "params": {"name": tool_name, "arguments": arguments}})
        r = recv()
    finally:
        try:
            proc.stdin.close()
        except BrokenPipeError:
            pass
        proc.wait(timeout=10)

    if r is None:
        return "ERROR: no response from kicad-mcp-pro"
    if "error" in r:
        return f"ERROR: {r['error']}"
    contents = r.get("result", {}).get("content", [])
    return "\n".join(c.get("text", "") for c in contents if c.get("type") == "text") or str(r.get("result"))


def _kicad_list_session() -> str:
    """Open a kicad-mcp-pro session and list all available tools."""
    import json, subprocess, os

    env = {
        **os.environ,
        "KICAD_MCP_PROJECT_DIR": _KICAD_DEFAULT_PROJECT,
        "KICAD_CLI": "/usr/bin/kicad-cli",
        "PATH": "/usr/bin:/usr/local/bin:/home/bismillah/.local/bin:" + os.environ.get("PATH", ""),
    }

    try:
        proc = subprocess.Popen(
            [_KICAD_BIN, "--transport", "stdio",
             "--project-dir", _KICAD_DEFAULT_PROJECT, "--mode", "readonly"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env
        )
    except FileNotFoundError:
        return f"ERROR: kicad-mcp-pro not found at {_KICAD_BIN}"

    def send(msg):
        proc.stdin.write(json.dumps(msg) + "\n")
        proc.stdin.flush()

    def recv():
        line = proc.stdout.readline()
        return json.loads(line.strip()) if line.strip() else None

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                         "clientInfo": {"name": "commander-bridge", "version": "1.0"}}})
        recv()
        send({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        r = recv()
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)

    if r is None:
        return "ERROR: no response from kicad-mcp-pro"
    tools = r.get("result", {}).get("tools", [])
    return "\n".join(f"- {t['name']}: {t.get('description','')[:90]}" for t in tools)


@mcp.tool()
def kicad_list_tools() -> str:
    """
    List all KiCad MCP Pro tools available via kicad_call().
    Use this first to discover what KiCad operations are supported.
    """
    return _kicad_list_session()


@mcp.tool()
def kicad_call(tool_name: str, arguments: dict = {}, project_dir: str = "") -> str:
    """
    Call any KiCad MCP Pro tool by name. Run kicad_list_tools() first to
    see all available tools and what arguments they accept.

    Args:
        tool_name: KiCad tool name (e.g. 'kicad_get_version', 'sch_get_symbols')
        arguments: dict of arguments for the tool (use {} if none needed)
        project_dir: path to KiCad project folder (default: ~/Downloads/Ijr-X1-Core)
    """
    proj = project_dir or _KICAD_DEFAULT_PROJECT
    return _kicad_session(tool_name, arguments, proj)


# ---------------------------------------------------------------------------
# Video analysis tool
# Uses FFmpeg (subprocess) for fast frame extraction + resize + JPEG encode,
# and OpenCV-Contrib + imagehash for perceptual deduplication.
# Read-only — no permission prompt needed.
# ---------------------------------------------------------------------------

import base64 as _base64
import hashlib as _hashlib
import tempfile as _tempfile
import shutil as _shutil

def _check_deps() -> tuple[bool, bool]:
    """Returns (has_cv2, has_ffmpeg)."""
    try:
        import cv2 as _cv2  # noqa: F401
        has_cv2 = True
    except ImportError:
        has_cv2 = False
    has_ffmpeg = _shutil.which("ffmpeg") is not None
    return has_cv2, has_ffmpeg


def _phash(frame_bgr) -> str:
    """Perceptual hash of a BGR frame using OpenCV (no imagehash dep needed)."""
    import cv2 as _cv2
    import numpy as _np
    small = _cv2.resize(frame_bgr, (32, 32), interpolation=_cv2.INTER_AREA)
    gray  = _cv2.cvtColor(small, _cv2.COLOR_BGR2GRAY).astype(_np.float32)
    mean  = gray.mean()
    bits  = (gray > mean).flatten()
    val   = int("".join("1" if b else "0" for b in bits), 2)
    return f"{val:016x}"


def _hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def _probe_video(p: Path) -> dict:
    """
    Run ffprobe on a video file and return a dict of metadata.
    Keys: width, height, duration_sec, fps, codec, pix_fmt, bit_rate,
          audio_codec, audio_sample_rate, audio_channels, file_size_mb,
          nb_frames (may be None), rotation (may be 0).
    """
    # Video stream
    v_cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries",
        "stream=width,height,r_frame_rate,codec_name,pix_fmt,bit_rate,nb_frames"
        ":stream_tags=rotate",
        "-of", "json",
        str(p),
    ]
    # Audio stream
    a_cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "a:0",
        "-show_entries", "stream=codec_name,sample_rate,channels,bit_rate",
        "-of", "json",
        str(p),
    ]
    # Container / format
    f_cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration,size,bit_rate,format_long_name",
        "-of", "json",
        str(p),
    ]

    def run(cmd):
        import json
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        return json.loads(r.stdout) if r.stdout.strip() else {}

    vj = run(v_cmd)
    aj = run(a_cmd)
    fj = run(f_cmd)

    vs = (vj.get("streams") or [{}])[0]
    as_ = (aj.get("streams") or [{}])[0]
    fmt = fj.get("format", {})

    # Parse fractional fps like "30000/1001"
    raw_fps = vs.get("r_frame_rate", "0/1")
    try:
        num, den = raw_fps.split("/")
        fps_val = round(int(num) / int(den), 3)
    except Exception:
        fps_val = 0.0

    duration = float(fmt.get("duration") or 0)
    size_bytes = int(fmt.get("size") or 0)

    rotation = 0
    tags = vs.get("tags", {})
    if isinstance(tags, dict):
        rotation = int(tags.get("rotate", 0))

    nb_frames = vs.get("nb_frames")
    if nb_frames:
        try:
            nb_frames = int(nb_frames)
        except Exception:
            nb_frames = None

    return {
        "width":              int(vs.get("width", 0)),
        "height":             int(vs.get("height", 0)),
        "duration_sec":       duration,
        "fps":                fps_val,
        "codec":              vs.get("codec_name", "unknown"),
        "pix_fmt":            vs.get("pix_fmt", "unknown"),
        "bit_rate_kbps":      round(int(vs.get("bit_rate") or fmt.get("bit_rate") or 0) / 1000, 1),
        "nb_frames":          nb_frames,
        "rotation":           rotation,
        "audio_codec":        as_.get("codec_name", "none"),
        "audio_sample_rate":  as_.get("sample_rate", "N/A"),
        "audio_channels":     as_.get("channels", 0),
        "audio_bitrate_kbps": round(int(as_.get("bit_rate") or 0) / 1000, 1),
        "container":          fmt.get("format_long_name", "unknown"),
        "file_size_mb":       round(size_bytes / 1_048_576, 2),
    }


def _build_frame_pipeline(
    p: Path,
    mode: str,
    fps: float,
    scene_threshold: float,
    thumbnail_batch: int,
    max_width: int,
    max_frames: int,
    start_sec: float,
    end_sec: float,
) -> list[str]:
    """Build the ffmpeg command list for frame extraction."""
    filters = []

    if mode == "scene":
        filters.append(f"thumbnail={thumbnail_batch}")
        filters.append("setpts=N/TB")
    elif mode == "scene_strict":
        # true scene-cut detection — works well for high-action video
        filters.append(f"select='gt(scene,{scene_threshold})'")
        filters.append("setpts=N/TB")
    elif mode == "keyframe":
        filters.append("select='eq(pict_type,I)'")
        filters.append("setpts=N/TB")
    else:  # uniform — accepts any decimal fps e.g. 0.5, 0.1, 0.0005
        # ffmpeg fps filter accepts fractions; express as rational for precision
        # e.g. 0.5 → "1/2", 0.0005 → "1/2000"
        if fps <= 0:
            fps = 1.0
        from fractions import Fraction
        frac = Fraction(fps).limit_denominator(100000)
        filters.append(f"fps={frac.numerator}/{frac.denominator}")

    filters.append(
        f"scale='if(gt(iw,ih),{max_width},-2)':'if(gt(iw,ih),-2,{max_width})'"
    )
    filters.append("format=rgb24")
    vf = ",".join(filters)

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
    if start_sec > 0:
        cmd += ["-ss", str(start_sec)]
    if end_sec > 0:
        cmd += ["-to", str(end_sec)]
    cmd += [
        "-i", str(p),
        "-vf", vf,
        "-vsync", "vfr",
        "-frames:v", str(max_frames * 4),   # over-extract; dedup trims
        "-f", "rawvideo",
        "-pix_fmt", "rgb24",
        "pipe:1",
    ]
    return cmd


def _extract_frames(
    raw: bytes,
    src_w: int,
    src_h: int,
    max_width: int,
    max_frames: int,
    jpeg_quality: int,
    dedup_threshold: int,
    blur_threshold: float,
    brightness_min: float,
    brightness_max: float,
) -> tuple[list[bytes], int, int]:
    """
    Split raw RGB24 stream → JPEG bytes, applying dedup + quality filters.
    Returns (kept_jpegs, out_w, out_h).
    """
    import cv2 as _cv2
    import numpy as _np

    # Compute scaled output dimensions
    if src_w >= src_h:
        out_w = max_width
        out_h = int(src_h * max_width / src_w) & ~1
    else:
        out_h = max_width
        out_w = int(src_w * max_width / src_h) & ~1

    frame_size = out_w * out_h * 3
    if frame_size == 0 or len(raw) < frame_size:
        return [], out_w, out_h

    n_raw = len(raw) // frame_size
    kept: list[bytes] = []
    seen_hashes: list[str] = []

    for fi in range(n_raw):
        chunk = raw[fi * frame_size:(fi + 1) * frame_size]
        frame_rgb = _np.frombuffer(chunk, dtype=_np.uint8).reshape((out_h, out_w, 3))
        frame_bgr = _cv2.cvtColor(frame_rgb, _cv2.COLOR_RGB2BGR)

        # Optional: skip blurry frames
        if blur_threshold > 0:
            gray = _cv2.cvtColor(frame_bgr, _cv2.COLOR_BGR2GRAY)
            laplacian_var = _cv2.Laplacian(gray, _cv2.CV_64F).var()
            if laplacian_var < blur_threshold:
                continue

        # Optional: skip too-dark or too-bright frames
        if brightness_min > 0 or brightness_max < 255:
            mean_brightness = frame_bgr.mean()
            if mean_brightness < brightness_min or mean_brightness > brightness_max:
                continue

        # Perceptual dedup
        ph = _phash(frame_bgr)
        if any(_hamming(ph, h) < dedup_threshold for h in seen_hashes):
            continue
        seen_hashes.append(ph)

        ok, buf = _cv2.imencode(
            ".jpg", frame_bgr,
            [_cv2.IMWRITE_JPEG_QUALITY, jpeg_quality,
             _cv2.IMWRITE_JPEG_OPTIMIZE, 1]
        )
        if not ok:
            continue
        kept.append(bytes(buf))
        if len(kept) >= max_frames:
            break

    return kept, out_w, out_h


@mcp.tool()
def details_video(path: str) -> str:
    """
    Return full technical metadata for a video file without extracting any
    frames. Includes resolution, duration, FPS, codec, pixel format,
    bitrate, audio info, rotation, container format, and file size.
    Read-only; no permission prompt required.

    Args:
        path: path to the video file
    """
    p = Path(path).expanduser()
    if not p.exists() or not p.is_file():
        return f"ERROR: file not found: {p}"

    _, has_ffmpeg = _check_deps()
    if not has_ffmpeg:
        return "ERROR: ffmpeg/ffprobe binary not found in PATH"

    try:
        m = _probe_video(p)
    except Exception as e:
        return f"ERROR: ffprobe failed: {e}"

    dur = m["duration_sec"]
    hh, rem = divmod(int(dur), 3600)
    mm, ss  = divmod(rem, 60)

    lines = [
        f"file:            {p.name}",
        f"size:            {m['file_size_mb']} MB",
        f"container:       {m['container']}",
        f"",
        f"--- Video ---",
        f"resolution:      {m['width']}x{m['height']}"
            + (f"  (rotated {m['rotation']}°)" if m['rotation'] else ""),
        f"duration:        {hh:02d}:{mm:02d}:{ss:02d}  ({dur:.3f}s)",
        f"fps:             {m['fps']}",
        f"total frames:    {m['nb_frames'] if m['nb_frames'] else 'N/A (not in header)'}",
        f"codec:           {m['codec']}",
        f"pixel format:    {m['pix_fmt']}",
        f"bitrate:         {m['bit_rate_kbps']} kbps",
        f"",
        f"--- Audio ---",
        f"codec:           {m['audio_codec']}",
        f"sample rate:     {m['audio_sample_rate']} Hz",
        f"channels:        {m['audio_channels']}",
        f"bitrate:         {m['audio_bitrate_kbps']} kbps",
    ]
    return "\n".join(lines)


@mcp.tool()
def analyze_video(
    path: str,
    # --- extraction mode ---
    mode: str = "scene",
    fps: float = 1.0,
    scene_threshold: float = 0.30,
    thumbnail_batch: int = 30,
    # --- frame limits ---
    max_frames: int = 8,
    start_sec: float = 0.0,
    end_sec: float = 0.0,
    # --- output size & quality ---
    max_width: int = 512,
    jpeg_quality: int = 60,
    # --- dedup & filtering ---
    dedup_threshold: int = 10,
    blur_threshold: float = 0.0,
    brightness_min: float = 0.0,
    brightness_max: float = 255.0,
    # --- response format ---
    return_mode: str = "both",
    include_metadata: bool = False,
) -> str:
    """
    Extract key frames from a video and return them as base64 JPEG images so
    the MCP client (Claude) can perform vision analysis directly.
    Uses FFmpeg for decoding/resizing and OpenCV for perceptual deduplication.
    Read-only; no permission prompt required.

    Args:
        path:              path to the video file

        mode:              extraction strategy —
                             "scene"       best representative frame per N-frame batch
                                           (thumbnail filter — great for screencasts &
                                            low-motion content, default)
                             "scene_strict" scene-cut detection via pixel difference score;
                                           use scene_threshold to tune sensitivity
                             "uniform"     fixed time interval sampling; supports any
                                           decimal fps e.g. 1.0, 0.5, 0.1, 0.0005
                             "keyframe"    codec I-frames only (fastest seek points)

        fps:               frames per second for uniform mode — accepts any positive
                           decimal, including very slow rates:
                             1.0    = 1 frame every second
                             0.5    = 1 frame every 2 seconds
                             0.1    = 1 frame every 10 seconds
                             0.0005 = 1 frame every ~33 minutes
                           (ignored in scene / scene_strict / keyframe modes)

        scene_threshold:   sensitivity for scene_strict mode — float 0.0–1.0;
                           lower = more sensitive (detects subtle changes),
                           higher = only hard cuts. Default 0.30.

        thumbnail_batch:   frame batch size for scene mode (default 30 = ~1s at 30fps);
                           smaller = finer sampling, larger = coarser.

        max_frames:        hard cap on returned frames (default 8, max 40)

        start_sec:         clip start time in seconds (0 = beginning of file)
        end_sec:           clip end time in seconds (0 = end of file)

        max_width:         resize longest edge to this many pixels (default 512);
                           use 256 for minimal tokens, 768 for higher detail

        jpeg_quality:      JPEG compression quality 1–95 (default 60);
                           lower = smaller base64 payload, higher = more detail

        dedup_threshold:   perceptual hash Hamming distance below which a frame is
                           considered a duplicate and skipped (default 10);
                           0 = keep all frames, 64 = extremely aggressive dedup

        blur_threshold:    Laplacian variance floor — frames below this are considered
                           blurry and skipped (default 0.0 = disabled);
                           try 50–200 to filter motion blur or out-of-focus frames

        brightness_min:    skip frames whose mean pixel brightness is below this
                           (default 0.0 = disabled); range 0–255

        brightness_max:    skip frames whose mean pixel brightness is above this
                           (default 255.0 = disabled); range 0–255;
                           e.g. set 240 to skip blown-out / all-white frames

        return_mode:       "frames" — base64 images only (no header text)
                           "both"   — metadata header + base64 images (default)

        include_metadata:  if True, prepend full video metadata (same output as
                           details_video) before the frames (default False)
    """
    import cv2 as _cv2

    p = Path(path).expanduser()
    if not p.exists() or not p.is_file():
        return f"ERROR: file not found: {p}"

    suffix = p.suffix.lower()
    known_exts = {
        ".mp4", ".mkv", ".avi", ".mov", ".webm",
        ".flv", ".m4v", ".ts", ".wmv", ".3gp", ".ogv",
    }
    if suffix not in known_exts:
        return (
            f"ERROR: unrecognised video extension '{suffix}'. "
            f"Supported: {', '.join(sorted(known_exts))}"
        )

    has_cv2, has_ffmpeg = _check_deps()
    if not has_cv2:
        return "ERROR: opencv-contrib-python is not installed"
    if not has_ffmpeg:
        return "ERROR: ffmpeg binary not found in PATH"

    max_frames = min(max(1, max_frames), 40)
    jpeg_quality = max(1, min(95, jpeg_quality))

    # --- optional metadata header ---
    meta_block = ""
    if include_metadata:
        try:
            m = _probe_video(p)
            dur = m["duration_sec"]
            hh, rem = divmod(int(dur), 3600)
            mm2, ss = divmod(rem, 60)
            meta_block = (
                f"[metadata] {p.name} | "
                f"{m['width']}x{m['height']} | "
                f"{hh:02d}:{mm2:02d}:{ss:02d} | "
                f"{m['fps']} fps | "
                f"{m['codec']} | "
                f"{m['file_size_mb']} MB\n"
            )
        except Exception as e:
            meta_block = f"[metadata error: {e}]\n"

    # --- probe source dimensions for stride math ---
    probe_cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=p=0",
        str(p),
    ]
    try:
        probe = subprocess.run(probe_cmd, capture_output=True, text=True, timeout=15)
        w_str, h_str = probe.stdout.strip().split(",")
        src_w, src_h = int(w_str), int(h_str)
    except Exception as e:
        return f"ERROR: ffprobe failed to read dimensions: {e}"

    # --- build and run ffmpeg pipeline ---
    cmd = _build_frame_pipeline(
        p, mode, fps, scene_threshold, thumbnail_batch,
        max_width, max_frames, start_sec, end_sec,
    )
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=180)
    except subprocess.TimeoutExpired:
        return "ERROR: ffmpeg timed out after 180s"
    except OSError as e:
        return f"ERROR: ffmpeg failed to launch: {e}"

    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")[:600]
        return f"ERROR: ffmpeg exited {result.returncode}: {stderr}"

    raw = result.stdout
    if not raw:
        return (
            "ERROR: ffmpeg produced no output. "
            "Try mode='uniform' with a low fps, or check the file path."
        )

    # --- extract, filter, dedup frames ---
    kept, out_w, out_h = _extract_frames(
        raw, src_w, src_h, max_width, max_frames,
        jpeg_quality, dedup_threshold,
        blur_threshold, brightness_min, brightness_max,
    )

    if not kept:
        return (
            "ERROR: no frames survived filtering. "
            "Try lowering dedup_threshold, blur_threshold, or adjusting brightness limits."
        )

    # --- build response ---
    lines = []
    if meta_block:
        lines.append(meta_block)

    if return_mode == "both":
        total_kb = sum(len(b) for b in kept) // 1024
        lines += [
            f"video: {p.name}",
            f"mode: {mode} | frames: {len(kept)} | "
            f"resolution: {out_w}x{out_h} | "
            f"jpeg_quality: {jpeg_quality} | "
            f"total: {total_kb}KB",
            "",
        ]

    for idx, jpeg_bytes in enumerate(kept, 1):
        b64 = _base64.b64encode(jpeg_bytes).decode("ascii")
        if return_mode == "both":
            lines.append(f"frame_{idx:03d} ({len(jpeg_bytes)//1024}KB):")
        lines.append(f"data:image/jpeg;base64,{b64}")
        lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run()
