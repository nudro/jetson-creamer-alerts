#!/usr/bin/env bash
# Resolve a WhatsApp group JID from an invite code (one-time setup utility).
# Example: ./resolve_group_jid.sh AbCdEfGhIjKlMnOpQrStUv
# Run it only when you need to discover a new group's JID:


set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
export NODE_PATH="${NODE_PATH:-$HOME/.npm-global/lib/node_modules/openclaw/node_modules}"
exec node "$ROOT/resolve_group_jid.js" "${1:?Usage: $0 <INVITE_CODE>}"
