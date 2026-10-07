#!/usr/bin/env bash
# FRIDAY Phase 4C validation — vault sync (git) + recall (vault -> Claude as data).
# Offline suites + 1 live Claude call (Constitution/Risk Doctrine supplied as
# untrusted data, temp vault) + a real-server smoke test with a TEMPORARY git
# vault pushing to a local stand-in for GitHub. Your real vault is only
# inspected read-only (never committed, never pushed).
set -uo pipefail

cd "$(dirname "$0")/.."
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "=== FRIDAY PHASE 4C VALIDATION — VAULT SYNC + RECALL ==="
echo "Branch : $(git rev-parse --abbrev-ref HEAD)"
echo "Commit : $(git rev-parse --short HEAD)"
echo "Python : $(python --version 2>&1)"
echo "Git    : $(git --version)"

REAL_VAULT="${FRIDAY_VAULT_PATH:-${HOME}/friday/FRIDAY-Knowledge-Vault}"
REAL_VAULT="${REAL_VAULT/#\~/$HOME}"
real_vault_fingerprint() {
  if [ -d "$REAL_VAULT" ]; then
    (cd "$REAL_VAULT" && find . -path ./.git -prune -o -type f -print0 | sort -z | xargs -0 sha256sum 2>/dev/null | sha256sum | cut -c1-16)
  else
    echo "absent"
  fi
}
real_vault_head() { git -C "$REAL_VAULT" rev-parse --short HEAD 2>/dev/null || echo "none"; }
REAL_BEFORE="$(real_vault_fingerprint)"
REAL_HEAD_BEFORE="$(real_vault_head)"

echo
echo "--- compile ---"
python -m compileall -q friday && echo "compile: PASS" || echo "compile: FAIL"

echo
echo "--- 4C sync + recall tests ---"
python -m pytest -q friday/tests/test_knowledge_sync_recall.py 2>&1 | tail -1
python -O -m pytest -q friday/tests/test_knowledge_sync_recall.py 2>&1 | tail -1 | sed 's/^/(python -O) /'

echo
echo "--- knowledge (4A/4B) + Hermes + app regression ---"
R="friday/tests/test_knowledge_vault.py friday/tests/test_knowledge_recorder.py friday/tests/test_hermes_contract.py friday/tests/test_hermes_runtime.py friday/tests/test_hermes_scheduler.py friday/tests/test_app_hermes.py"
python -m pytest -q $R 2>&1 | tail -1
python -O -m pytest -q $R 2>&1 | tail -1 | sed 's/^/(python -O) /'

echo
echo "--- LIVE: command -> Hermes -> Claude with Constitution + Risk Doctrine as DATA (1 call) ---"
python -m pytest -q -s -rs friday/tests/test_app_live.py 2>&1 | grep -E "^\[APP\]|passed|failed|skipped|Error|assert" | tail -9

echo
echo "--- REAL SERVER smoke test (127.0.0.1:8765, temp DB, temp GIT vault + local stand-in remote) ---"
SMOKE_DIR="$(mktemp -d)"
V="$SMOKE_DIR/vault"; REMOTE="$SMOKE_DIR/remote.git"
G="git -c user.name=Validation -c user.email=validation@example.invalid -c commit.gpgsign=false"
python -m friday.knowledge init --vault "$V" >/dev/null
git init -q --bare -b main "$REMOTE"
git -C "$V" init -q -b main
git -C "$V" add -A && $G -C "$V" commit -q -m "initial vault"
git -C "$V" remote add origin "$REMOTE" && git -C "$V" push -q -u origin main
printf 'my staged idea\n' > "$V/04-Research/staged-idea.md" && git -C "$V" add 04-Research/staged-idea.md
DB_PATH="$SMOKE_DIR/friday_smoke.db" HERMES_MARKET_SCAN_INTERVAL_SECONDS=0 \
FRIDAY_KNOWLEDGE_ENABLED=true FRIDAY_VAULT_PATH="$V" FRIDAY_VAULT_PUSH=true \
FRIDAY_VAULT_SYNC_INTERVAL_SECONDS=900 \
  python -m uvicorn friday.app.main:app --host 127.0.0.1 --port 8765 --log-level warning \
  > "$SMOKE_DIR/server.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 40); do
  curl -sf http://127.0.0.1:8765/health >/dev/null 2>&1 && break
  sleep 0.5
done
python - "$V" "$REMOTE" <<'PY'
import json, subprocess, sys, time, urllib.request
BASE = "http://127.0.0.1:8765"
vault, remote = sys.argv[1], sys.argv[2]
def get(p):
    return json.load(urllib.request.urlopen(BASE + p, timeout=10))
def post(p, body=None):
    req = urllib.request.Request(BASE + p, data=json.dumps(body or {}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        return json.load(urllib.request.urlopen(req, timeout=120))
    except urllib.error.HTTPError as e:
        return {"http_status": e.code}
def git(*args, cwd=vault):
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True).stdout.strip()
checks = {}
try:
    h = get("/health")
    k = get("/api/knowledge/status")
    checks["server HEALTHY, hermes RUNNING, execution locked"] = (
        h["status"] == "HEALTHY" and h["hermes"] == "RUNNING" and h["execution_locked"] is True)
    checks["recall enabled, sync enabled with push"] = (
        k["recall"]["enabled"] is True and k["sync"]["enabled"] is True and k["sync"]["push_enabled"] is True)
    checks["vault-sync schedule registered"] = "vault-sync" in [s["name"] for s in get("/api/hermes/status")["schedules"]]
    time.sleep(2)                                    # health check journals, scheduled sync runs
    s1 = post("/api/knowledge/sync")
    remote_files = git("ls-tree", "-r", "--name-only", "main", cwd=remote).splitlines()
    checks["manual sync ran through Hermes"] = s1.get("status") == "SUCCEEDED" and s1["result"]["status"] in ("PUSHED", "NO_CHANGES")
    if not checks["manual sync ran through Hermes"]:
        print(f"[SMOKE] manual sync returned: status={s1.get('status')} error={s1.get('error')} result={s1.get('result')}")
    checks["FRIDAY journal pushed to the remote"] = any(f.startswith("FRIDAY/Journal/") for f in remote_files)
    checks["remote == local (fast-forward, no force)"] = git("rev-parse", "main", cwd=remote) == git("rev-parse", "HEAD")
    checks["your staged note NOT committed, still staged"] = (
        "04-Research/staged-idea.md" not in remote_files
        and git("diff", "--cached", "--name-only") == "04-Research/staged-idea.md")
    checks["FRIDAY commits touch only FRIDAY/"] = all(
        f.startswith("FRIDAY/") for f in git("log", "--author=FRIDAY", "--name-only", "--format=").split() if f)
    s2 = post("/api/knowledge/sync")
    checks["next sync has nothing to do (no self-journal churn)"] = s2.get("result", {}).get("status") == "NO_CHANGES"
    checks["forbidden job still refused"] = post("/api/hermes/jobs", {"job_type": "PLACE_ORDER"}).get("http_status") == 400
    k2 = get("/api/knowledge/status")
    print(f"[SMOKE] sync runs={k2['sync']['runs']} commits={k2['sync']['commits']} pushes={k2['sync']['pushes']} "
          f"last={k2['sync']['last_result']['status'] if k2['sync']['last_result'] else None} "
          f"friday_commits={len(git('log', '--author=FRIDAY', '--format=%h').split())}")
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
from friday.hermes.jobs import JOB_CAPABILITIES, JobType
from friday.hermes.scheduler import MIN_INTERVAL_SECONDS
from friday.hermes.budget import DEFAULT_RESERVE_PER_CALL
from friday.intelligence.claude import MAX_OUTPUT_TOKENS
from friday.knowledge.recall import DEFAULT_MAX_CHARS, KNOWLEDGE_RECALL_VERSION
from friday.knowledge.sync import VAULT_SYNC_VERSION
env = pathlib.Path(".env.example").read_text()
src = pathlib.Path("friday/knowledge/sync.py").read_text()
checks = {
    "authority = 4B set + READ_KNOWLEDGE + PUBLISH_KNOWLEDGE": {"READ_KNOWLEDGE", "PUBLISH_KNOWLEDGE"}
        <= {c.value for c in HERMES_AUTHORITY},
    "no execution capability exists": not any(is_forbidden(c.value) for c in Capability),
    "VAULT_SYNC cannot journal itself (no WRITE_KNOWLEDGE)": Capability.WRITE_KNOWLEDGE not in JOB_CAPABILITIES[JobType.VAULT_SYNC],
    "only analysis reads the vault": [j.value for j, c in JOB_CAPABILITIES.items() if Capability.READ_KNOWLEDGE in c] == ["INTELLIGENCE_ANALYSIS"],
    "vault sync at most every 300s": MIN_INTERVAL_SECONDS[JobType.VAULT_SYNC] >= 300,
    "sync never force-pushes": '"--force' not in src and '"-f"' not in src and '"+' not in src,
    "git never prompts (BatchMode, no terminal prompt)": "BatchMode=yes" in src and 'GIT_TERMINAL_PROMPT"] = "0"' in src,
    "recall block fits the per-call reserve": 4700 + DEFAULT_MAX_CHARS // 3 + MAX_OUTPUT_TOKENS <= DEFAULT_RESERVE_PER_CALL,
    "versions 4C.1": KNOWLEDGE_RECALL_VERSION == "4C.1" and VAULT_SYNC_VERSION == "4C.1",
    ".env.example documents 4C settings": all(k in env for k in (
        "FRIDAY_KNOWLEDGE_RECALL", "FRIDAY_VAULT_SYNC_INTERVAL_SECONDS", "FRIDAY_VAULT_PUSH")),
}
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
print("ALL CHECKS:", "PASS" if all(checks.values()) else "FAIL")
PY

echo
echo "--- your real vault (read-only report; nothing committed or pushed) ---"
python -m friday.knowledge sync-status --vault "$REAL_VAULT" 2>&1 | grep -E '"(git_repo|branch|remote_transport|ssh_command_configured|pending_friday_changes|unpushed_commits|error)"'
REAL_AFTER="$(real_vault_fingerprint)"
REAL_HEAD_AFTER="$(real_vault_head)"
if [ "$REAL_BEFORE" = "$REAL_AFTER" ] && [ "$REAL_HEAD_BEFORE" = "$REAL_HEAD_AFTER" ]; then
  echo "PASS: real vault unchanged by validation (files $REAL_AFTER, HEAD $REAL_HEAD_AFTER)"
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
