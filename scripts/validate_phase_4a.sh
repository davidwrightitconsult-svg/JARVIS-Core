#!/usr/bin/env bash
# FRIDAY Phase 4A validation — knowledge vault contract (offline, no API calls).
set -uo pipefail

cd "$(dirname "$0")/.."
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "=== FRIDAY PHASE 4A VALIDATION — KNOWLEDGE VAULT CONTRACT ==="
echo "Branch : $(git rev-parse --abbrev-ref HEAD)"
echo "Commit : $(git rev-parse --short HEAD)"
echo "Python : $(python --version 2>&1)"

echo
echo "--- compile ---"
python -m compileall -q friday && echo "compile: PASS" || echo "compile: FAIL"

echo
echo "--- vault contract tests ---"
python -m pytest -q friday/tests/test_knowledge_vault.py 2>&1 | tail -1
python -O -m pytest -q friday/tests/test_knowledge_vault.py 2>&1 | tail -1 | sed 's/^/(python -O) /'

echo
echo "--- init into a TEMPORARY vault (your real vault is not touched) ---"
TMPV="$(mktemp -d)/vault"
python -m friday.knowledge init --vault "$TMPV" | sed -n '1,16p'
python - "$TMPV" <<'PY'
import sys
from friday.knowledge import KnowledgeVault, NoteMeta, NoteType, VaultPermissionError, SecretDetectedError
v = KnowledgeVault(sys.argv[1])
checks = {}
checks["second init creates nothing"] = v.init() == []
try:
    v.write_note("00-Constitution/FRIDAY-Constitution.md", NoteMeta(type=NoteType.SYSTEM, title="x"), "hijack", overwrite=True)
    checks["Constitution write refused"] = False
except VaultPermissionError:
    checks["Constitution write refused"] = True
p = v.write_note("FRIDAY/Journal/validation.md", NoteMeta(type=NoteType.JOURNAL, title="4A validation"), "FRIDAY can write here.")
checks["FRIDAY zone write allowed"] = p.is_file()
try:
    v.write_note("FRIDAY/Analyses/leak.md", NoteMeta(type=NoteType.ANALYSIS, title="x"), "sk-ant-api03-" + "x" * 30)
    checks["secret refused"] = False
except SecretDetectedError:
    checks["secret refused"] = True
note = v.read_note("00-Constitution/FRIDAY-Constitution.md")
checks["human notes read as untrusted data"] = note.trust == "UNTRUSTED_DATA" and note.zone == "HUMAN"
for k, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}: {k}")
print("ALL CHECKS:", "PASS" if all(checks.values()) else "FAIL")
PY
rm -rf "$(dirname "$TMPV")"

echo
echo "--- full regression (live tests and soak skipped) ---"
python -m pytest -q friday/tests --ignore=friday/tests/test_claude_live_tasks.py \
  --ignore=friday/tests/test_hermes_live.py --ignore=friday/tests/test_hermes_soak.py \
  --ignore=friday/tests/test_app_live.py \
  --deselect friday/tests/test_claude_api.py::test_claude_live_non_trading_request 2>&1 | tail -1

echo
echo "--- security scan ---"
git grep -qE "sk-ant-[A-Za-z0-9_-]{10,}" && echo "FAIL: Anthropic key pattern found" || echo "PASS: no Anthropic API key detected"
git grep -qE "github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9]{30,}" && echo "FAIL: GitHub token found" || echo "PASS: no GitHub token detected"
git grep -qE "BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY" && echo "FAIL: private key material" || echo "PASS: no private key material detected"
git ls-files --error-unmatch .env >/dev/null 2>&1 && echo "FAIL: .env tracked" || echo "PASS: .env is not tracked"
git ls-files | grep -q "^\.venv/" && echo "FAIL: .venv tracked" || echo "PASS: .venv is not tracked"

echo
echo "=== END — paste everything above back to Claude ==="
