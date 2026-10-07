#!/usr/bin/env bash
# FRIDAY Phase 5A validation — private research library + systemd service.
# Offline: no Claude calls, no network. Uses a TEMPORARY library; your real
# library (~/friday/library) is only reported on, never changed.
set -uo pipefail

cd "$(dirname "$0")/.."
if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

echo "=== FRIDAY PHASE 5A VALIDATION — RESEARCH LIBRARY + SERVICE ==="
echo "Branch : $(git rev-parse --abbrev-ref HEAD)"
echo "Commit : $(git rev-parse --short HEAD)"
echo "Python : $(python --version 2>&1)"

echo
echo "--- prerequisites ---"
python - <<'PY'
import sqlite3
try:
    import fitz
    print(f"PASS: PyMuPDF {fitz.VersionBind} (PDF extraction)")
except Exception as exc:
    print(f"FAIL: PyMuPDF missing ({exc}) — run: pip install -r friday/requirements.txt")
try:
    c = sqlite3.connect(":memory:")
    c.execute("create virtual table t using fts5(x, tokenize='porter unicode61')")
    print(f"PASS: SQLite {sqlite3.sqlite_version} with FTS5 (local search)")
except Exception as exc:
    print(f"FAIL: SQLite FTS5 unavailable ({exc})")
PY

echo
echo "--- compile ---"
python -m compileall -q friday && echo "compile: PASS" || echo "compile: FAIL"

echo
echo "--- research library tests ---"
python -m pytest -q -p no:warnings friday/tests/test_research_library.py 2>&1 | tail -1
python -O -m pytest -q -p no:warnings friday/tests/test_research_library.py 2>&1 | tail -1 | sed 's/^/(python -O) /'

echo
echo "--- CLI walkthrough on a TEMPORARY library ---"
TMPL="$(mktemp -d)/library"
python - "$TMPL" <<'PY'
import sys, fitz
from pathlib import Path
root = Path(sys.argv[1]).parent
doc = fitz.open()
for text in ["Favourite-longshot bias: bettors overvalue unlikely outcomes. " * 10,
             "Calibration: stated probabilities should match observed frequencies. " * 10]:
    doc.new_page().insert_textbox(fitz.Rect(40, 40, 560, 800), text, fontsize=9)
doc.set_metadata({"title": "Validation Paper"})
doc.save(str(root / "validation-paper.pdf"))
(root / "idea.md").write_text("# Base rate idea\n\n" + "Check base rates before narratives. " * 12)
PY
python -m friday.research init --library "$TMPL" >/dev/null && echo "init: OK"
python -m friday.research add "$(dirname "$TMPL")/validation-paper.pdf" --kind paper --library "$TMPL"
python -m friday.research add "$(dirname "$TMPL")/idea.md" --kind note --library "$TMPL"
python -m friday.research add "$(dirname "$TMPL")/idea.md" --kind note --library "$TMPL"
python -m friday.research search "longshot bias" --limit 1 --library "$TMPL" | head -2
python -m friday.research status --library "$TMPL" | grep -E '"documents"|"chunks"'
rm -rf "$(dirname "$TMPL")"

echo
echo "--- REAL SERVER smoke test (127.0.0.1:8765, temp DB + temp library, no Claude) ---"
SMOKE_DIR="$(mktemp -d)"
python -m friday.research init --library "$SMOKE_DIR/library" >/dev/null
DB_PATH="$SMOKE_DIR/friday.db" HERMES_MARKET_SCAN_INTERVAL_SECONDS=0 FRIDAY_KNOWLEDGE_ENABLED=false \
FRIDAY_LIBRARY_PATH="$SMOKE_DIR/library" \
  python -m uvicorn friday.app.main:app --host 127.0.0.1 --port 8765 --log-level warning \
  > "$SMOKE_DIR/server.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 40); do curl -sf http://127.0.0.1:8765/health >/dev/null 2>&1 && break; sleep 0.5; done
python - "$SMOKE_DIR" <<'PY'
import json, sys, urllib.request, uuid
from pathlib import Path
BASE, smoke = "http://127.0.0.1:8765", Path(sys.argv[1])
def get(p):
    return json.load(urllib.request.urlopen(BASE + p, timeout=10))
def upload(name, data, kind="book"):
    boundary = uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{name}\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(f"{BASE}/api/books?kind={kind}", data=body, method="POST",
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        return json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as e:
        return {"http_status": e.code}
checks = {}
try:
    h = get("/health")
    checks["HEALTHY, research READY, execution locked"] = (
        h["status"] == "HEALTHY" and h["components"].get("research") == "READY" and h["execution_locked"] is True)
    note = ("# Kelly sizing note\n\n" + "Fractional Kelly limits drawdowns in bankroll management. " * 12).encode()
    up = upload("kelly.md", note, kind="note")
    checks["upload ingested into the private library"] = up.get("status") == "ADDED"
    checks["re-upload detected as duplicate"] = upload("again.md", note, kind="note").get("status") == "DUPLICATE"
    checks["bad file rejected with a reason"] = upload("fake.pdf", b"not a pdf" * 50).get("status") == "REJECTED"
    found = get("/api/research/search?q=fractional%20kelly")
    checks["search finds it, labelled UNTRUSTED_DATA"] = (
        found["trust"] == "UNTRUSTED_DATA" and found["hits"] and found["hits"][0]["kind"] == "note")
    checks["no upload staging left behind"] = not any(
        p.is_file() or p.name.startswith(".") for p in (smoke / "library" / "inbox").rglob("*"))
    checks["nothing written inside the repo"] = not Path("friday/books").exists() or not any(Path("friday/books").iterdir())
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
echo "--- systemd unit ---"
UNIT_TMP="$(mktemp -d)/friday.service"
sed -e "s#__USER__#$(id -un)#g" -e "s#__GROUP__#$(id -gn)#g" -e "s#__REPO__#$PWD#g" -e "s#__HOME__#$HOME#g" \
  deploy/systemd/friday.service.template > "$UNIT_TMP"
if command -v systemd-analyze >/dev/null; then
  # Only FRIDAY's unit matters; Ubuntu's own units may print unrelated warnings.
  OUT="$(systemd-analyze verify "$UNIT_TMP" 2>&1 | grep -F "$UNIT_TMP" || true)"
  [ -z "$OUT" ] && echo "PASS: unit file verifies" || { echo "CHECK: $OUT"; }
fi
grep -q -- "--host 127.0.0.1" "$UNIT_TMP" && echo "PASS: service binds 127.0.0.1 only" || echo "FAIL: service not bound to localhost"
grep -q "^User=$(id -un)$" "$UNIT_TMP" && ! grep -q "^User=root" "$UNIT_TMP" && echo "PASS: runs as $(id -un), not root" || echo "FAIL: service user"
bash -n scripts/install_friday_service.sh && echo "PASS: installer syntax OK"
if systemctl list-unit-files friday.service >/dev/null 2>&1 && systemctl is-enabled friday.service >/dev/null 2>&1; then
  echo "service installed: $(systemctl is-enabled friday.service), $(systemctl is-active friday.service)"
else
  echo "service not installed yet (next step: sudo bash scripts/install_friday_service.sh)"
fi
rm -rf "$(dirname "$UNIT_TMP")"

echo
echo "--- full regression (live tests and soak skipped) ---"
python -m pytest -q -p no:warnings friday/tests --ignore=friday/tests/test_claude_live_tasks.py \
  --ignore=friday/tests/test_hermes_live.py --ignore=friday/tests/test_hermes_soak.py \
  --ignore=friday/tests/test_app_live.py \
  --deselect friday/tests/test_claude_api.py::test_claude_live_non_trading_request 2>&1 | tail -1

echo
echo "--- your real library (read-only report) ---"
REAL_LIB="${FRIDAY_LIBRARY_PATH:-$HOME/friday/library}"
python -m friday.research status --library "$REAL_LIB" 2>&1 | grep -E '"(library|initialised|documents|chunks|inbox_waiting)"' \
  || echo "not initialised yet (next step: python -m friday.research init)"

echo
echo "--- security scan ---"
git grep -qE "sk-ant-[A-Za-z0-9_-]{10,}" && echo "FAIL: Anthropic key pattern found" || echo "PASS: no Anthropic API key detected"
git grep -qE "github_pat_[A-Za-z0-9_]{20,}|ghp_[A-Za-z0-9]{30,}" && echo "FAIL: GitHub token found" || echo "PASS: no GitHub token detected"
git grep -qE "BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY" && echo "FAIL: private key material" || echo "PASS: no private key material detected"
git ls-files --error-unmatch .env >/dev/null 2>&1 && echo "FAIL: .env tracked" || echo "PASS: .env is not tracked"
git ls-files | grep -q "^\.venv/" && echo "FAIL: .venv tracked" || echo "PASS: .venv is not tracked"
git ls-files | grep -qiE "\.(pdf|epub)$" && echo "FAIL: book/paper files tracked in Git" || echo "PASS: no PDF/EPUB files tracked in Git"
git check-ignore -q friday/books/x.pdf && echo "PASS: old friday/books/ upload folder is git-ignored" || echo "FAIL: friday/books/ not ignored"

echo
echo "=== END — paste everything above back to Claude ==="
