#!/usr/bin/env bash
# FRIDAY Phase 4B validation — FRIDAY writes its own notes into the vault.
# Offline suites + 1 live Claude call (note written to a TEMPORARY vault)
# + a real-server smoke test on 127.0.0.1:8765 with a temp DB and temp vault.
# Your real vault (~/friday/FRIDAY-Knowledge-Vault) is checked, never written.
set -uo pipefail

cd "$(dirname "$0")/.."
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "=== FRIDAY PHASE 4B VALIDATION — FRIDAY WRITES TO ITS VAULT AREA ==="
echo "Branch : $(git rev-parse --abbrev-ref HEAD)"
echo "Commit : $(git rev-parse --short HEAD)"
echo "Python : $(python --version 2>&1)"

REAL_VAULT="${HOME}/friday/FRIDAY-Knowledge-Vault"
real_vault_fingerprint() {
  if [ -d "$REAL_VAULT" ]; then
    (cd "$REAL_VAULT" && find . -path ./.git -prune -o -type f -print0 | sort -z | xargs -0 sha256sum 2>/dev/null | sha256sum | cut -c1-16)
  else
    echo "absent"
  fi
}
REAL_BEFORE="$(real_vault_fingerprint)"

echo
echo "--- compile ---"
python -m compileall -q friday && echo "compile: PASS" || echo "compile: FAIL"

echo
echo "--- knowledge recorder + vault tests ---"
T="friday/tests/test_knowledge_recorder.py friday/tests/test_knowledge_vault.py"
python -m pytest -q $T 2>&1 | tail -1
python -O -m pytest -q $T 2>&1 | tail -1 | sed 's/^/(python -O) /'

echo
echo "--- Hermes + app regression (authority now includes WRITE_KNOWLEDGE) ---"
H="friday/tests/test_hermes_contract.py friday/tests/test_hermes_runtime.py friday/tests/test_hermes_scheduler.py friday/tests/test_app_hermes.py"
python -m pytest -q $H 2>&1 | tail -1
python -O -m pytest -q $H 2>&1 | tail -1 | sed 's/^/(python -O) /'

echo
echo "--- LIVE: command -> Hermes -> Claude -> analysis note in a TEMP vault (1 call) ---"
python -m pytest -q -s -rs friday/tests/test_app_live.py 2>&1 | grep -E "^\[APP\]|passed|failed|skipped|Error|assert" | tail -8

echo
echo "--- REAL SERVER smoke test (127.0.0.1:8765, temp DB, temp vault, no market scan, no Claude) ---"
SMOKE_DIR="$(mktemp -d)"
python -m friday.knowledge init --vault "$SMOKE_DIR/vault" >/dev/null
HUMAN_HASH_PY='import hashlib, sys
from pathlib import Path
v = Path(sys.argv[1]); h = hashlib.sha256()
for f in sorted(x for x in v.rglob("*") if x.is_file() and x.relative_to(v).parts[0] not in ("FRIDAY", ".git")):
    h.update(str(f.relative_to(v)).encode()); h.update(f.read_bytes())
print(h.hexdigest()[:16])'
HUMAN_BEFORE="$(python -c "$HUMAN_HASH_PY" "$SMOKE_DIR/vault")"
DB_PATH="$SMOKE_DIR/friday_smoke.db" HERMES_MARKET_SCAN_INTERVAL_SECONDS=0 \
FRIDAY_KNOWLEDGE_ENABLED=true FRIDAY_VAULT_PATH="$SMOKE_DIR/vault" \
  python -m uvicorn friday.app.main:app --host 127.0.0.1 --port 8765 --log-level warning \
  > "$SMOKE_DIR/server.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 40); do
  curl -sf http://127.0.0.1:8765/health >/dev/null 2>&1 && break
  sleep 0.5
done
python - "$SMOKE_DIR/vault" <<'PY'
import json, sys, time, urllib.request
from pathlib import Path
BASE = "http://127.0.0.1:8765"
vault = Path(sys.argv[1])
def get(p):
    return json.load(urllib.request.urlopen(BASE + p, timeout=10))
def post(p, body=None):
    req = urllib.request.Request(BASE + p, data=json.dumps(body or {}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        return json.load(urllib.request.urlopen(req, timeout=10))
    except urllib.error.HTTPError as e:
        return {"http_status": e.code}
def journal_text():
    return "\n".join(p.read_text(encoding="utf-8") for p in sorted((vault / "FRIDAY" / "Journal").glob("*.md")))
checks = {}
try:
    h = get("/health")
    k0 = get("/api/knowledge/status")
    checks["server HEALTHY, hermes RUNNING, execution locked"] = (
        h["status"] == "HEALTHY" and h["hermes"] == "RUNNING" and h["execution_locked"] is True)
    checks["knowledge recorder enabled on the temp vault"] = (
        k0["enabled"] is True and Path(k0["vault"]).resolve() == vault.resolve())
    time.sleep(2)
    checks["scheduled health check journalled"] = "HEALTH" in journal_text()
    checks["forbidden job refused over HTTP"] = post("/api/hermes/jobs", {"job_type": "PLACE_ORDER"}).get("http_status") == 400
    post("/api/hermes/kill", {"reason": "4B smoke drill"})
    time.sleep(0.5)
    checks["kill switch journalled"] = "4B smoke drill" in journal_text()
    post("/api/hermes/reset", {"confirmed_by": "validation"}); post("/api/hermes/start")
    k1 = get("/api/knowledge/status")
    checks["notes written, zero write failures"] = k1["notes_written"] >= 2 and k1["write_failures"] == 0
    friday_notes = [p for p in (vault / "FRIDAY").rglob("*.md") if p.name != "README.md"]
    checks["FRIDAY notes present in FRIDAY/"] = len(friday_notes) >= 1
    body = journal_text()
    checks["journal carries no execution authority"] = "execution_authorized: true" not in body
    print(f"[SMOKE] notes_written={k1['notes_written']} failures={k1['write_failures']} friday_notes={len(friday_notes)}")
except Exception as exc:
    checks[f"server reachable ({type(exc).__name__}: {exc})"] = False
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
print("SERVER SMOKE:", "PASS" if checks and all(checks.values()) else "FAIL")
PY
kill "$SERVER_PID" 2>/dev/null; wait "$SERVER_PID" 2>/dev/null
if grep -qiE "traceback|error" "$SMOKE_DIR/server.log"; then
  echo "server log (errors):"; grep -iE -A3 "traceback|error" "$SMOKE_DIR/server.log" | head -20
fi
HUMAN_AFTER="$(python -c "$HUMAN_HASH_PY" "$SMOKE_DIR/vault")"
[ "$HUMAN_BEFORE" = "$HUMAN_AFTER" ] && echo "PASS: human folders untouched ($HUMAN_AFTER)" || echo "FAIL: human folders changed"
rm -rf "$SMOKE_DIR"

echo
echo "--- full regression (live tests and soak skipped) ---"
python -m pytest -q friday/tests --ignore=friday/tests/test_claude_live_tasks.py \
  --ignore=friday/tests/test_hermes_live.py --ignore=friday/tests/test_hermes_soak.py \
  --ignore=friday/tests/test_app_live.py \
  --deselect friday/tests/test_claude_api.py::test_claude_live_non_trading_request 2>&1 | tail -1

echo
echo "--- integration checks ---"
python - <<'PY'
import pathlib
from friday.hermes.authority import Capability, HERMES_AUTHORITY, is_forbidden
from friday.hermes.jobs import JOB_CAPABILITIES
from friday.knowledge.recorder import KNOWLEDGE_RECORDER_VERSION
env = pathlib.Path(".env.example").read_text()
checks = {
    "WRITE_KNOWLEDGE granted, not forbidden": Capability.WRITE_KNOWLEDGE in HERMES_AUTHORITY and not is_forbidden("WRITE_KNOWLEDGE"),
    "every job type declares WRITE_KNOWLEDGE": all(Capability.WRITE_KNOWLEDGE in caps for jt, caps in JOB_CAPABILITIES.items() if jt.value != "VAULT_SYNC"),
    "no execution capability exists": not any(is_forbidden(c.value) for c in Capability),
    "recorder version 4B.1": KNOWLEDGE_RECORDER_VERSION == "4B.1",
    "/api/knowledge/status route": '"/api/knowledge/status"' in pathlib.Path("friday/app/main.py").read_text(),
    ".env.example documents knowledge settings": "FRIDAY_KNOWLEDGE_ENABLED" in env and "FRIDAY_SNAPSHOT_INTERVAL_SECONDS" in env,
}
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
print("ALL CHECKS:", "PASS" if all(checks.values()) else "FAIL")
PY

echo
echo "--- your real vault ---"
REAL_AFTER="$(real_vault_fingerprint)"
if [ "$REAL_BEFORE" = "$REAL_AFTER" ]; then
  echo "PASS: real vault unchanged by validation ($REAL_AFTER)"
else
  echo "FAIL: real vault changed during validation"
fi

echo
echo "--- security scan ---"
git grep -qE "sk-ant-[A-Za-z0-9_-]{10,}" && echo "FAIL: Anthropic key pattern found" || echo "PASS: no Anthropic API key detected"
git grep -qE "github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9]{30,}" && echo "FAIL: GitHub token found" || echo "PASS: no GitHub token detected"
git grep -qE "BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY" && echo "FAIL: private key material" || echo "PASS: no private key material detected"
git ls-files --error-unmatch .env >/dev/null 2>&1 && echo "FAIL: .env tracked" || echo "PASS: .env is not tracked"
git ls-files | grep -q "^\.venv/" && echo "FAIL: .venv tracked" || echo "PASS: .venv is not tracked"

echo
echo "=== END — paste everything above back to Claude ==="
