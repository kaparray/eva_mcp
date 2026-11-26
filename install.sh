#!/bin/sh
# Installs the EVA MCP server for Claude Code: asks for your EVA URL (and optional CA chain),
# stores your personal token and registers the server.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
command -v python3 >/dev/null || { echo "python3 not found"; exit 1; }
CFG_DIR="$HOME/.config/eva-mcp"
CFG="$CFG_DIR/config.json"
mkdir -p "$CFG_DIR"

# defaults: existing config.json, then site.json shipped with this build (if any)
read_key() { python3 -c 'import json,sys
for p in sys.argv[2:]:
    try: v = json.load(open(p)).get(sys.argv[1])
    except Exception: v = None
    if v: print(v); break' "$1" "$CFG" "$DIR/site.json"; }
DEF_URL="${EVA_URL:-$(read_key url)}"
DEF_CA="${EVA_CA_BUNDLE:-$(read_key ca_bundle)}"

printf "EVA URL%s: " "${DEF_URL:+ [$DEF_URL]}"; read -r URL
URL="${URL:-$DEF_URL}"
[ -n "$URL" ] || { echo "EVA URL is required (e.g. https://eva.example.com)"; exit 1; }
echo "CA chain (PEM) to verify TLS. Leave empty to use the system trust store."
printf "Path to CA bundle%s: " "${DEF_CA:+ [$DEF_CA]}"; read -r CA
CA="${CA:-$DEF_CA}"
if [ -n "$CA" ]; then
  case "$CA" in /*|~*) ;; *) [ -f "$DIR/$CA" ] && CA="$DIR/$CA" ;; esac
  [ -f "$CA" ] || { echo "CA bundle not found: $CA"; exit 1; }
fi

python3 - "$CFG" "$URL" "$CA" <<'PY'
import json, os, sys
path, url, ca = sys.argv[1:4]
try:
    cfg = json.load(open(path))
except Exception:
    cfg = {}
cfg["url"] = url.rstrip("/")
if ca:
    cfg["ca_bundle"] = os.path.abspath(os.path.expanduser(ca))
else:
    cfg.pop("ca_bundle", None)
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
print(f"Settings saved to {path}")
PY

if [ "$(uname)" = "Darwin" ]; then
  if security find-generic-password -a "$USER" -s eva-api-token >/dev/null 2>&1; then
    echo "Token already in Keychain (eva-api-token). To replace: security delete-generic-password -a \"$USER\" -s eva-api-token"
  else
    echo "Paste your personal EVA API token (input is hidden):"
    security add-generic-password -a "$USER" -s eva-api-token -w
  fi
else
  F="$CFG_DIR/token"
  if [ ! -s "$F" ]; then
    printf "Paste your personal EVA API token: "; stty -echo; read -r T; stty echo; echo
    umask 077; printf "%s" "$T" > "$F"
  fi
fi

python3 "$DIR/server.py" --check

# method catalog and UI protocol are specific to an EVA instance: generate them on first install
[ -s "$DIR/catalog.json" ] || { echo "Generating catalog.json from your EVA..."; python3 "$DIR/server.py" --refresh-catalog; }
[ -s "$DIR/protocol.json" ] || { echo "Generating protocol.json from your EVA..."; python3 "$DIR/server.py" --refresh-protocol; }

if command -v claude >/dev/null; then
  claude mcp remove eva -s user >/dev/null 2>&1 || true
  claude mcp add eva -s user -- python3 "$DIR/server.py"
  echo "Done. Start a new Claude Code session: tools eva_* will be available."
else
  echo "Claude Code CLI not found. Add to your MCP client config:"
  echo "  {\"mcpServers\":{\"eva\":{\"command\":\"python3\",\"args\":[\"$DIR/server.py\"]}}}"
fi
