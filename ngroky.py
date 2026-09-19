#!/usr/bin/env python3
"""
ngroky.py — expose the fs-shell-mcp server to the internet through ngrok.
"""
import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
SERVER_FILE = PROJECT_DIR / "server.py"
STATE_DIR = Path.home() / ".config" / "fs-shell-mcp"
STATE_FILE = STATE_DIR / "ngroky_state.json"
LOG_DIR = STATE_DIR / "logs"
NGROK_CONFIG_CANDIDATES = [
    Path.home() / ".config" / "ngrok" / "ngrok.yml",
    Path.home() / "Library" / "Application Support" / "ngrok" / "ngrok.yml",
]


def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _pid_alive(pid) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _find_free_port(start: int) -> int:
    port = start
    while _port_open("127.0.0.1", port, timeout=0.2):
        port += 1
    return port


def _http_get_json(url: str, timeout: float = 3.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def check_ngrok_installed() -> str:
    from shutil import which
    path = which("ngrok")
    if not path:
        sys.exit(
            "ngrok is not installed.\n"
            "Install it with:\n"
            "  curl -sSL https://ngrok.com/download/linux-amd64/ngrok-v3-stable-linux-amd64.tgz "
            "-o /tmp/ngrok.tgz && sudo tar -xzf /tmp/ngrok.tgz -C /usr/local/bin\n"
            "(or via snap: sudo snap install ngrok)"
        )
    return path


def check_ngrok_authenticated() -> None:
    for cfg in NGROK_CONFIG_CANDIDATES:
        if cfg.exists():
            try:
                text = cfg.read_text()
                if "authtoken" in text:
                    return
            except OSError:
                continue

    print(
        "\nngrok isn't authenticated yet. Grab a free authtoken from:\n"
        "  https://dashboard.ngrok.com/get-started/your-authtoken\n"
    )
    token = input("Paste your ngrok authtoken here: ").strip()
    if not token:
        sys.exit("No token provided, can't continue.")
    result = subprocess.run(
        ["ngrok", "config", "add-authtoken", token],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        sys.exit(f"Failed to set ngrok authtoken:\n{result.stdout}\n{result.stderr}")
    print("ngrok authtoken configured.\n")


def get_ngrok_config_path():
    for cfg in NGROK_CONFIG_CANDIDATES:
        if cfg.exists():
            return cfg
    return None


def ensure_mcp_server(host: str, port: int, state: dict):
    if _port_open(host, port):
        print(f"MCP server already listening on {host}:{port}, reusing it.")
        return state.get("mcp_pid")

    if not SERVER_FILE.exists():
        sys.exit(f"Can't find server.py next to ngroky.py at {SERVER_FILE}")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / "mcp_http.log"

    wrapper = f"""
import sys
sys.path.insert(0, {str(PROJECT_DIR)!r})
import server as srv

srv.mcp.settings.host = {host!r}
srv.mcp.settings.port = {port}
try:
    srv.mcp.settings.transport_security.enable_dns_rebinding_protection = False
except AttributeError:
    pass

srv.mcp.run(transport="streamable-http")
"""
    log_file = open(log_path, "a")
    proc = subprocess.Popen(
        [sys.executable, "-c", wrapper],
        cwd=str(PROJECT_DIR),
        stdout=log_file, stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    print(f"Starting MCP server (HTTP) on {host}:{port} (pid {proc.pid}), logs at {log_path} ...")
    for _ in range(120):
        if _port_open(host, port):
            print("MCP server is up.")
            return proc.pid
        time.sleep(0.5)

    sys.exit(f"MCP server didn't come up within 60s, check {log_path}")


def stop_process(pid, label: str) -> None:
    if pid and _pid_alive(pid):
        try:
            os.kill(pid, 15)
            print(f"Stopped {label} (pid {pid}).")
        except OSError as e:
            print(f"Couldn't stop {label} (pid {pid}): {e}")
    else:
        print(f"{label} wasn't running.")


def find_existing_tunnel(inspector_port: int, target_port: int):
    # Try both ngrok v2 and v3 API paths
    for api_path in ["/api/tunnels", "/api/tunnels/"]:
        try:
            data = _http_get_json(f"http://127.0.0.1:{inspector_port}{api_path}")
            tunnels = data.get("tunnels", [])
            for t in tunnels:
                addr = t.get("config", {}).get("addr", "")
                # ngrok v3 sometimes uses full URL like "http://localhost:8765"
                if str(target_port) in addr:
                    url = t.get("public_url", "")
                    if url.startswith("https://"):
                        return url
                    # fallback to any url
                    if url:
                        return url
        except (urllib.error.URLError, OSError, json.JSONDecodeError):
            pass
    return None


def _scrape_url_from_log(log_path: Path) -> str | None:
    """Fallback: grep the ngrok log for the forwarding URL."""
    try:
        text = log_path.read_text(errors="replace")
        for line in reversed(text.splitlines()):
            if "url=" in line and "ngrok-free.app" in line:
                for part in line.split():
                    if part.startswith("url=https://"):
                        return part[4:]
                    if part.startswith("https://") and "ngrok" in part:
                        return part
    except OSError:
        pass
    return None


def ensure_ngrok_tunnel(target_port: int, state: dict) -> str:
    saved_pid = state.get("ngrok_pid")
    saved_inspector_port = state.get("ngrok_inspector_port")

    if saved_pid and _pid_alive(saved_pid) and saved_inspector_port:
        url = find_existing_tunnel(saved_inspector_port, target_port)
        if url:
            print(f"Reusing existing ngrok tunnel (pid {saved_pid}).")
            return url

    inspector_port = _find_free_port(4040)
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / "ngrok.log"

    # Truncate old log so _scrape_url_from_log only sees fresh output
    log_path.write_text("")
    log_file = open(log_path, "a")

    cmd = ["ngrok", "http", str(target_port), "--log", "stdout", "--log-format", "json"]

    if inspector_port != 4040:
        default_cfg = get_ngrok_config_path()
        web_addr_cfg = STATE_DIR / "ngrok_webaddr.yml"
        web_addr_cfg.write_text(f"version: 3\nagent:\n  web_addr: 127.0.0.1:{inspector_port}\n")
        if default_cfg:
            cmd += ["--config", str(default_cfg), "--config", str(web_addr_cfg)]
        else:
            cmd += ["--config", str(web_addr_cfg)]

    proc = subprocess.Popen(
        cmd,
        stdout=log_file, stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    TIMEOUT_SECONDS = 90
    POLL_INTERVAL = 2
    iterations = TIMEOUT_SECONDS // POLL_INTERVAL

    print(f"Starting ngrok tunnel to port {target_port} (pid {proc.pid}), inspector on {inspector_port} ...")
    print(f"Waiting up to {TIMEOUT_SECONDS}s for tunnel to establish ...")

    url = None
    for i in range(iterations):
        # Check if process died early
        if proc.poll() is not None:
            print(f"ngrok process exited early (code {proc.returncode}), check {log_path}")
            break

        # Try API first
        url = find_existing_tunnel(inspector_port, target_port)
        if url:
            break

        # Fallback: scrape log for URL (works even if inspector isn't up yet)
        url = _scrape_url_from_log(log_path)
        if url:
            break

        elapsed = (i + 1) * POLL_INTERVAL
        print(f"  [{elapsed}s] waiting...", end="\r", flush=True)
        time.sleep(POLL_INTERVAL)

    print()  # newline after the \r progress line

    if not url:
        print(f"\nLast 20 lines of {log_path}:")
        try:
            lines = log_path.read_text(errors="replace").splitlines()
            print("\n".join(lines[-20:]))
        except OSError:
            pass
        sys.exit(f"\nngrok didn't establish a tunnel within {TIMEOUT_SECONDS}s.")

    state["ngrok_pid"] = proc.pid
    state["ngrok_inspector_port"] = inspector_port
    return url


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8765, help="local port for the MCP HTTP server")
    parser.add_argument("--host", default="127.0.0.1", help="local bind host for the MCP HTTP server")
    parser.add_argument("--stop", action="store_true", help="stop the MCP server and ngrok tunnel this script started")
    parser.add_argument("--status", action="store_true", help="print current status and exit")
    args = parser.parse_args()

    state = _load_state()

    if args.stop:
        stop_process(state.get("mcp_pid"), "MCP server")
        stop_process(state.get("ngrok_pid"), "ngrok tunnel")
        _save_state({})
        return

    if args.status:
        mcp_up = _port_open(args.host, args.port)
        print(f"MCP server ({args.host}:{args.port}): {'UP' if mcp_up else 'down'}")
        insp = state.get("ngrok_inspector_port")
        url = find_existing_tunnel(insp, args.port) if insp else None
        print(f"ngrok tunnel: {url or 'not running'}")
        return

    check_ngrok_installed()
    check_ngrok_authenticated()

    mcp_pid = ensure_mcp_server(args.host, args.port, state)
    if mcp_pid:
        state["mcp_pid"] = mcp_pid
    state["mcp_port"] = args.port
    _save_state(state)

    public_url = ensure_ngrok_tunnel(args.port, state)
    _save_state(state)

    endpoint = public_url.rstrip("/") + "/mcp"
    print("\n" + "=" * 60)
    print(f"MCP server is live at:  {endpoint}")
    print("=" * 60)
    print(
        "\n⚠️  Anyone with this URL can call your MCP tools (still gated by\n"
        "zenity popups on this machine, but treat the URL as a secret).\n"
        f"Run `python3 {Path(__file__).name} --stop` when you're done."
    )


if __name__ == "__main__":
    main()
