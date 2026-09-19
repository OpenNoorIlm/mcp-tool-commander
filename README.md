# fs-shell-mcp

An MCP server that gives an AI assistant (Claude, LM Studio, etc.) the
ability to run shell commands and read/search/write/edit files and folders
on your Ubuntu 24.04 machine — with **real permission popups** so nothing
happens without you clicking "allow."

## Tools it exposes

| Tool | What it does | Needs your permission? |
|---|---|---|
| `run_command` | Runs a shell command in a given working directory | Yes, every time (unless you've trusted that folder or enabled always-allow) |
| `read_file` | Reads a text file | No (read-only) |
| `list_dir` | Lists a folder's contents | No (read-only) |
| `create_file` | Creates a new file (fails if it exists) | Yes, unless folder is whitelisted |
| `write_file` | Creates/overwrites a file | Yes, unless folder is whitelisted |
| `edit_file` | Find-and-replace within an existing file | Yes, unless folder is whitelisted |
| `append_file` | Appends text to the end of a file (creates it if missing) | Yes, unless folder is whitelisted |
| `delete_file` | Deletes a single file (files only, not directories) | Yes, unless folder is whitelisted |
| `move_file` | Moves/renames a file, optionally overwriting the destination | Yes, unless folder is whitelisted |
| `create_folder` | Creates a folder (and parents) | Yes, unless folder is whitelisted |
| `search` | Fast recursive text search in a folder | No (read-only) |
| `search_deep` | Thorough search incl. filenames + hidden files, broader root | No (read-only) |
| `web_search` | Searches the web (DuckDuckGo HTML, no API key needed) and returns title/URL/snippet results | No (network only, no local filesystem/command access) |
| `web_fetch` | Fetches a URL and returns readable text (or raw HTML) | No (network only, no local filesystem/command access) |

## How the permission system works

Whenever the assistant tries to run a command or touch the filesystem
outside a folder you've already whitelisted, a **zenity dialog pops up on
your desktop** describing exactly what it wants to do. You choose one of:

- **Allow just this once**
- **Allow for the rest of this session** (until you restart the server)
- **Always allow** (remembered permanently in a config file)
- **Deny**

If `zenity` isn't installed, every gated action is denied by default (fails
safe) rather than silently allowed.

Permanent grants are stored in `~/.config/fs-shell-mcp/config.json`. You can
edit this file directly at any time, e.g. to remove a whitelisted folder or
to set `"always_allow_commands": true` if you want to stop being prompted
for shell commands entirely (not recommended unless you understand the
risk).

## 1. Install dependencies (Ubuntu 24.04)

```bash
sudo apt update
sudo apt install -y zenity python3-pip python3-venv
# optional but recommended, makes search_deep faster on huge trees:
sudo apt install -y ripgrep
```

Then, from this project folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2. Test it standalone

```bash
source .venv/bin/activate
python3 server.py
```

It will sit there speaking MCP over stdio — that's expected, it's meant to
be launched by a client, not used directly in a terminal. Ctrl+C to stop.
You can sanity-check the permission dialog works with:

```bash
python3 -c "import permissions; print(permissions.request_command_permission('echo test', '.'))"
```

A popup should appear.

## 3. Connect it to LM Studio

In LM Studio: **Settings → Program → Install → Edit `mcp.json`** (or the
equivalent "Model Context Protocol servers" config screen), and add:

```json
{
  "mcpServers": {
    "fs-shell-mcp": {
      "command": "/home/YOUR_USER/fs-shell-mcp/.venv/bin/python3",
      "args": ["/home/YOUR_USER/fs-shell-mcp/server.py"]
    }
  }
}
```

Replace `/home/YOUR_USER/fs-shell-mcp` with the actual path where you put
this project. Restart LM Studio, then enable the `fs-shell-mcp` tools for
your chat/model.

## 4. Connect it to Claude Desktop

Edit `~/.config/Claude/claude_desktop_config.json` (create it if it
doesn't exist):

```json
{
  "mcpServers": {
    "fs-shell-mcp": {
      "command": "/home/YOUR_USER/fs-shell-mcp/.venv/bin/python3",
      "args": ["/home/YOUR_USER/fs-shell-mcp/server.py"]
    }
  }
}
```

Restart Claude Desktop. You should see the tools available (look for the
tool/plug icon in the chat).

## Notes on safety

- `run_command` runs via `sh -c`, so it can do anything your user account
  can do — that's the point, but it means you should only click "Always
  allow" for folders/commands you genuinely trust.
- There's no built-in blocklist of dangerous commands (e.g. `rm -rf`) —
  the permission dialog *is* the safety net. Read what it shows you before
  clicking allow.
- `search` and `search_deep` are read-only and never prompt, but
  `search_deep` defaults to scanning your whole home directory, which can
  be slow on large disks.
- To revoke a permanent grant, edit or delete entries in
  `~/.config/fs-shell-mcp/config.json`.
- `web_search` and `web_fetch` reach out to the public internet on your
  behalf (DuckDuckGo and whatever URL you ask it to fetch). They never
  prompt for permission since they don't touch local files or run
  commands, but they do mean the assistant can pull in arbitrary web
  content — treat that content as untrusted, the same way you would in a
  browser.
