#!/usr/bin/env bash
# FRIDAY Phase 2C-4 validation — Claude adapter integration.
# Runs the full suite INCLUDING 6 live Claude calls (one per task, each
# with an injection attempt). Read-only apart from pytest caches.
set -uo pipefail

cd "$(dirname "$0")/.."
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "=== FRIDAY PHASE 2C-4 VALIDATION ==="
echo "Branch : $(git rev-parse --abbrev-ref HEAD)"
echo "Commit : $(git rev-parse --short HEAD)"
echo "Python : $(python --version 2>&1)"

echo
echo "--- compile ---"
python -m compileall -q friday && echo "compile: PASS" || echo "compile: FAIL"

echo
echo "--- offline suite ---"
python -m pytest -q friday/tests --ignore=friday/tests/test_claude_live_tasks.py 2>&1 | tail -3

echo
echo "--- optimised mode (python -O: asserts stripped, invariants must hold) ---"
python -O -m pytest -q friday/tests/test_claude_adapter.py friday/tests/test_intelligence.py 2>&1 | tail -1

echo
echo "--- LIVE: 6 tasks x injection attempt (needs ANTHROPIC_API_KEY in .env) ---"
python -m pytest -q -s -rs friday/tests/test_claude_live_tasks.py 2>&1 | grep -E "^\[|passed|failed|skipped|Error|assert" | tail -12

echo
echo "--- dependency drift: friday/requirements.txt vs this environment ---"
python - <<'PY'
import re
from importlib import metadata
drift = 0
for line in open("friday/requirements.txt"):
    line = line.strip()
    if "==" not in line or line.startswith("#"):
        continue
    name, pinned = line.split("==", 1)
    dist = re.sub(r"\[.*\]", "", name)
    try:
        installed = metadata.version(dist)
    except metadata.PackageNotFoundError:
        installed = "NOT INSTALLED"
    if installed != pinned:
        drift += 1
        print(f"DRIFT: {dist:18} pinned {pinned:14} installed {installed}")
print("dependency drift:", "NONE" if drift == 0 else f"{drift} package(s)")
PY

echo
echo "--- security scan ---"
git grep -qE "sk-ant-[A-Za-z0-9_-]{10,}" && echo "FAIL: Anthropic key pattern found" || echo "PASS: no Anthropic API key detected"
git grep -qE "github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9]{30,}" && echo "FAIL: GitHub token found" || echo "PASS: no GitHub token detected"
git grep -qE "BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY" && echo "FAIL: private key material" || echo "PASS: no private key material detected"
git ls-files --error-unmatch .env >/dev/null 2>&1 && echo "FAIL: .env tracked" || echo "PASS: .env is not tracked"
git ls-files | grep -q "^\.venv/" && echo "FAIL: .venv tracked" || echo "PASS: .venv is not tracked"

echo
echo "=== END — paste everything above back to Claude ==="
