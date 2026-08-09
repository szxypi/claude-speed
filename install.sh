#!/bin/bash
# claude-speed installer (macOS)
# Compiles the menubar app, installs a user LaunchAgent, and (optionally)
# wires the Claude Code statusline. Idempotent — safe to re-run after updates.
#
# Usage:
#   ./install.sh                # menubar app only
#   ./install.sh --statusline   # also wire statusline into ~/.claude/settings.json
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
LABEL="com.claude-speed.menubar"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ "$(uname)" != "Darwin" ]]; then
  echo "The menubar app is macOS-only. On other platforms you can still use"
  echo "the statusline script — see README (Statusline section)."
  exit 1
fi
command -v swiftc >/dev/null 2>&1 || {
  echo "swiftc not found. Install Xcode Command Line Tools first:"
  echo "  xcode-select --install"
  exit 1
}
command -v python3 >/dev/null 2>&1 || { echo "python3 not found."; exit 1; }

echo "==> Compiling ClaudeSpeed"
swiftc -O -o ClaudeSpeed main.swift

echo "==> Installing LaunchAgent $LABEL"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>$LABEL</string>
    <key>ProgramArguments</key><array><string>$DIR/ClaudeSpeed</string></array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>LimitLoadToSessionType</key><string>Aqua</string>
</dict>
</plist>
EOF
# OpenCode Desktop 会从登录 shell 读取 XDG/OPENCODE_*；LaunchAgent 不会。
# 安装时把实际使用的数据路径一并固化，保证自定义 data/db 位置也能被采集。
python3 - "$PLIST" "${XDG_DATA_HOME:-}" "${OPENCODE_DB:-}" <<'PY'
import plistlib, sys

path, xdg_data, opencode_db = sys.argv[1:]
env = {}
if xdg_data:
    env["XDG_DATA_HOME"] = xdg_data
if opencode_db:
    env["OPENCODE_DB"] = opencode_db
if env:
    with open(path, "rb") as f:
        doc = plistlib.load(f)
    doc["EnvironmentVariables"] = env
    with open(path, "wb") as f:
        plistlib.dump(doc, f)
PY
# bootstrap (not load): works correctly from any context, incl. SSH/agents
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null \
  || launchctl kickstart -k "gui/$(id -u)/$LABEL"

if [[ "${1:-}" == "--statusline" ]]; then
  echo "==> Wiring Claude Code statusline (~/.claude/settings.json, backup: .bak)"
  python3 - "$DIR/statusline-speed.py" <<'PY'
import json, os, shutil, sys
p = os.path.expanduser("~/.claude/settings.json")
d = {}
if os.path.exists(p):
    shutil.copy(p, p + ".bak")
    with open(p) as f:
        d = json.load(f)
d["statusLine"] = {"type": "command", "command": sys.argv[1], "padding": 0}
os.makedirs(os.path.dirname(p), exist_ok=True)
with open(p, "w") as f:
    json.dump(d, f, ensure_ascii=False, indent=2)
print("statusLine ->", sys.argv[1])
PY
fi

echo "Done. Look for the ⚪/🟢 icon in your menu bar."
