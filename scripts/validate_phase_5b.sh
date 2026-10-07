#!/usr/bin/env bash
# FRIDAY Phase 5B validation — web articles by URL + ingestion as Hermes jobs.
# Offline suites, then REAL internet fetches into a TEMPORARY library
# (one public article, plus refused attempts at local/private addresses),
# then a real-server smoke test. No Claude calls. Your real library is
# only reported on.
set -uo pipefail

cd "$(dirname "$0")/.."
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "=== FRIDAY PHASE 5B VALIDATION — WEB ARTICLES + INGESTION JOBS ==="
echo "Branch : $(git rev-parse --abbrev-ref HEAD)"
echo "Commit : $(git rev-parse --short HEAD)"
echo "Python : $(python --version 2>&1)"

echo
echo "--- compile ---"
python -m compileall -q friday && echo "compile: PASS" || echo "compile: FAIL"

echo
echo "--- 5B fetcher + ingestion tests ---"
python -m pytest -q -p no:warnings friday/tests/test_research_ingest.py 2>&1 | tail -1
python -O -m pytest -q -p no:warnings friday/tests/test_research_ingest.py 2>&1 | tail -1 | sed 's/^/(python -O) /'

echo
echo "--- research library (5A) + Hermes + app regression ---"
R="friday/tests/test_research_library.py friday/tests/test_hermes_contract.py friday/tests/test_hermes_runtime.py friday/tests/test_hermes_scheduler.py friday/tests/test_app_hermes.py friday/tests/test_knowledge_recorder.py"
python -m pytest -q -p no:warnings $R 2>&1 | tail -1

echo
echo "--- LIVE internet: fetcher rules against real DNS (TEMPORARY library) ---"
TMPL="$(mktemp -d)/library"
python -m friday.research init --library "$TMPL" >/dev/null
python - "$TMPL" <<'PY'
import sys
from friday.research import ResearchLibrary
lib = ResearchLibrary(sys.argv[1])
checks = {}
sources = {"Wikipedia article (HTML)": ("https://en.wikipedia.org/wiki/Prediction_market", "article", "prediction market"),
           "IETF RFC 2119 (plain text)": ("https://www.rfc-editor.org/rfc/rfc2119.txt", "paper", "key words")}
fetched = {}
for label, (url, kind, query) in sources.items():
    r = lib.add_url(url, kind=kind)
    fetched[label] = r
    print(f"[LIVE] {label}: {r.status} «{r.title}» {r.chunks} passages{(' — ' + r.reason) if r.reason else ''}")
ok_docs = [r for r in fetched.values() if r.status == "ADDED"]
checks["public documents fetched and ingested"] = len(ok_docs) == len(sources)
checks["searchable, URL recorded as source"] = all(
    lib.search(q) for _, _, q in sources.values()) and all(
    lib.get(r.doc_id)["source"].startswith("https://") for r in ok_docs)
checks["same URL again is a duplicate (not re-fetched)"] = \
    lib.add_url("https://www.rfc-editor.org/rfc/rfc2119.txt").status == "DUPLICATE"
refusals = {
    "https://127.0.0.1/": "non-public",
    "https://localhost/": "not a public",
    "https://169.254.169.254/latest/meta-data/": "non-public",
    "https://localtest.me/": "non-public",          # public DNS name that resolves to 127.0.0.1
    "https://192.168.1.1/": "non-public",
    "http://example.com/": "only https",
    "https://example.com:8443/": "443",
}
for url, why in refusals.items():
    r = lib.add_url(url)
    passed = r.status == "REJECTED" and why in (r.reason or "")
    checks[f"refused {url}"] = passed
    if not passed:
        print(f"   {url}: {r.status} {r.reason}")
for name, passed in checks.items():
    print(f"{'PASS' if passed else 'FAIL'}: {name}")
print("LIVE FETCHER:", "PASS" if all(checks.values()) else "FAIL")
PY
rm -rf "$(dirname "$TMPL")"

echo
echo "--- REAL SERVER smoke test (127.0.0.1:8765, temp DB + temp library + temp vault, no Claude) ---"
SMOKE_DIR="$(mktemp -d)"
python -m friday.research init --library "$SMOKE_DIR/library" >/dev/null
python -m friday.knowledge init --vault "$SMOKE_DIR/vault" >/dev/null
printf '# Kelly note\n\n%s\n' "$(printf 'Fractional Kelly limits drawdowns. %.0s' $(seq 1 15))" > "$SMOKE_DIR/library/inbox/note/kelly.md"
DB_PATH="$SMOKE_DIR/friday.db" HERMES_MARKET_SCAN_INTERVAL_SECONDS=0 \
FRIDAY_KNOWLEDGE_ENABLED=true FRIDAY_VAULT_PATH="$SMOKE_DIR/vault" \
FRIDAY_LIBRARY_PATH="$SMOKE_DIR/library" \
  python -m uvicorn friday.app.main:app --host 127.0.0.1 --port 8765 --log-level warning \
  > "$SMOKE_DIR/server.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 40); do curl -sf http://127.0.0.1:8765/health >/dev/null 2>&1 && break; sleep 0.5; done
python - "$SMOKE_DIR" <<'PY'
import json, sys, time, urllib.request
from pathlib import Path
BASE, smoke = "http://127.0.0.1:8765", Path(sys.argv[1])
def get(p):
    return json.load(urllib.request.urlopen(BASE + p, timeout=10))
def post(p, body=None):
    req = urllib.request.Request(BASE + p, data=json.dumps(body or {}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        return json.load(urllib.request.urlopen(req, timeout=120))
    except urllib.error.HTTPError as e:
        return {"http_status": e.code}
checks = {}
try:
    h = get("/health")
    checks["HEALTHY, research READY, execution locked"] = (
        h["status"] == "HEALTHY" and h["components"].get("research") == "READY" and h["execution_locked"] is True)
    checks["research-inbox schedule registered"] = "research-inbox" in [
        s["name"] for s in get("/api/hermes/status")["schedules"]]
    time.sleep(2)                                   # startup inbox run picks up note/kelly.md
    url = post("/api/research/url", {"url": "https://www.rfc-editor.org/rfc/rfc8174.txt", "kind": "article"})
    checks["URL ingested through a Hermes job"] = (
        url.get("job_type") == "RESEARCH_INGEST_URL" and url.get("status") == "SUCCEEDED"
        and url["result"]["status"] in ("ADDED", "DUPLICATE"))
    checks["http:// refused at the door"] = post("/api/research/url", {"url": "http://example.com"}).get("http_status") == 400
    bad = post("/api/research/url", {"url": "https://169.254.169.254/latest/meta-data/"})
    checks["metadata address refused (REJECTED, no retry)"] = (
        bad.get("status") == "SUCCEEDED" and bad["result"]["status"] == "REJECTED" and bad.get("attempts") == 1)
    docs = get("/api/research/documents")
    kinds = sorted(d["kind"] for d in docs)
    checks["inbox note + article in library"] = kinds == ["article", "note"]
    checks["no server paths exposed"] = all("original" not in d for d in docs)
    journal = "\n".join(p.read_text() for p in (smoke / "vault" / "FRIDAY" / "Journal").glob("*.md"))
    checks["research activity journalled in the vault"] = "**RESEARCH**" in journal and "**RESEARCH INBOX**" in journal
    print(f"[SMOKE] documents={len(docs)} kinds={kinds}")
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
python -m pytest -q -p no:warnings friday/tests --ignore=friday/tests/test_claude_live_tasks.py \
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
from friday.research.fetch import FETCHER_VERSION, MAX_FETCH_BYTES, MAX_REDIRECTS, user_agent
src = pathlib.Path("friday/research/fetch.py").read_text()
checks = {
    "INGEST_RESEARCH granted, not forbidden": Capability.INGEST_RESEARCH in HERMES_AUTHORITY and not is_forbidden("INGEST_RESEARCH"),
    "no execution capability exists": not any(is_forbidden(c.value) for c in Capability),
    "only research jobs can ingest": {j.value for j, c in JOB_CAPABILITIES.items()
                                      if Capability.INGEST_RESEARCH in c} <= {"RESEARCH_DIGEST", "RESEARCH_INGEST_INBOX", "RESEARCH_INGEST_URL"},
    "inbox pick-up at most every 300s": MIN_INTERVAL_SECONDS[JobType.RESEARCH_INGEST_INBOX] >= 300,
    "fetcher: no proxies/env, manual redirects": "trust_env=False" in src and "follow_redirects=False" in src,
    "fetcher: 25 MB per download, 3 redirects": MAX_FETCH_BYTES == 25 * 1024 * 1024 and MAX_REDIRECTS == 3,
    "fetcher version 5B.2": FETCHER_VERSION == "5B.2",
    "User-Agent carries a contact URL": "(+http" in user_agent() or "(+" in user_agent(),
    ".env.example documents inbox interval": "FRIDAY_RESEARCH_INBOX_INTERVAL_SECONDS" in pathlib.Path(".env.example").read_text(),
}
for name, ok in checks.items():
    print(f"{'PASS' if ok else 'FAIL'}: {name}")
print("ALL CHECKS:", "PASS" if all(checks.values()) else "FAIL")
PY

echo
echo "--- your real library + service (read-only report) ---"
python -m friday.research status --library "${FRIDAY_LIBRARY_PATH:-$HOME/friday/library}" 2>&1 \
  | grep -E '"(documents|chunks|inbox_waiting)"' || echo "library not initialised"
if systemctl is-enabled friday.service >/dev/null 2>&1; then
  echo "friday.service: $(systemctl is-active friday.service) (restart it to load 5B: sudo systemctl restart friday)"
fi

echo
echo "--- security scan ---"
git grep -qE "sk-ant-[A-Za-z0-9_-]{10,}" && echo "FAIL: Anthropic key pattern found" || echo "PASS: no Anthropic API key detected"
git grep -qE "github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9]{30,}" && echo "FAIL: GitHub token found" || echo "PASS: no GitHub token detected"
git grep -qE "BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY" && echo "FAIL: private key material" || echo "PASS: no private key material detected"
git ls-files --error-unmatch .env >/dev/null 2>&1 && echo "FAIL: .env tracked" || echo "PASS: .env is not tracked"
git ls-files | grep -q "^\.venv/" && echo "FAIL: .venv tracked" || echo "PASS: .venv is not tracked"
git ls-files | grep -qiE "\.(pdf|epub)$" && echo "FAIL: book/paper files tracked in Git" || echo "PASS: no PDF/EPUB files tracked in Git"

echo
echo "=== END — paste everything above back to Claude ==="
