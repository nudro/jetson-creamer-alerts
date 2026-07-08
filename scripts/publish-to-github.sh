#!/usr/bin/env bash
# Finish publishing to GitHub (run once after: gh auth login)
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="${HOME}/bin:${PATH}"

SCAN_FILES=(app.py control_panel.py README.md resolve_group_jid.js resolve_group_jid.sh .env.example Modelfile.qwen-house requirements.txt .gitignore)

echo "=== Pre-push secrets scan ==="
fail=0

while IFS= read -r match; do
  [[ "$match" == *"120363000000000000@g.us"* ]] && continue
  echo "FAIL: unexpected JID pattern: $match"
  fail=1
done < <(git grep -E '[0-9]{12,18}@g\.us' HEAD -- "${SCAN_FILES[@]}" 2>/dev/null || true)

if git grep -qE '10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}' HEAD -- "${SCAN_FILES[@]}" 2>/dev/null; then
  echo "FAIL: private LAN IP found"
  fail=1
else
  echo "OK: no LAN IPs"
fi

if git grep -qE '/home/[a-zA-Z0-9_-]+' HEAD -- "${SCAN_FILES[@]}" 2>/dev/null; then
  echo "FAIL: hardcoded /home/ path found"
  fail=1
else
  echo "OK: no hardcoded home paths"
fi

if git grep -qE '\+[1-9][0-9]{10,14}' HEAD -- "${SCAN_FILES[@]}" 2>/dev/null; then
  echo "FAIL: phone number found"
  fail=1
else
  echo "OK: no phone numbers"
fi

if git grep -qE 'chat\.whatsapp\.com/[A-Za-z0-9]{10,}' HEAD -- "${SCAN_FILES[@]}" 2>/dev/null; then
  echo "FAIL: WhatsApp invite URL with code found"
  fail=1
else
  echo "OK: no invite URLs"
fi

if git log --all --oneline -- .env | grep -q .; then
  echo "FAIL: .env appears in git history"
  fail=1
else
  echo "OK: .env not in history"
fi

if [[ "$fail" -ne 0 ]]; then
  echo "Secrets scan failed — fix before pushing."
  exit 1
fi
echo "All checks passed."

if ! gh auth status >/dev/null 2>&1; then
  echo ""
  echo "GitHub CLI is not authenticated."
  echo ""
  echo "Create a classic token at: https://github.com/settings/tokens/new"
  echo "Required scopes:  repo  +  read:org"
  echo "(Fine-grained tokens often fail — use a classic token.)"
  echo ""
  echo "Then run:"
  echo "  export PATH=\"\$HOME/bin:\$PATH\""
  echo "  gh auth login --scopes \"repo,read:org\""
  echo "  # choose: Paste an authentication token"
  echo ""
  exit 1
fi

REPO="nudro/jetson-creamer-alerts"
if gh repo view "$REPO" >/dev/null 2>&1; then
  echo "Repo exists — pushing main..."
  git push -u origin main
else
  echo "Creating public repo $REPO and pushing..."
  if git remote get-url origin >/dev/null 2>&1; then
    git remote remove origin
  fi
  gh repo create "$REPO" --public --source=. --remote=origin --push
fi

echo "Done: https://github.com/$REPO"
