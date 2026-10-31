# Deploying Phase 14A / 14B / 15 to the server

Everything below is PAPER. Nothing in this release can place an order anywhere.

## 1. Pull and run the tests on the box
```bash
cd ~/jarvis            # or wherever the checkout lives
git fetch origin
git status --short     # must be clean; if not, see "local changes" below
git checkout main && git pull
source .venv/bin/activate && pip install -r requirements.txt
python -m pytest -q jarvis/tests
```
Expect `865 passed, 11 skipped`. If the count is lower, the checkout is behind.

**Local changes.** If `git status` is not clean, do NOT merge over them. Save them first:
```bash
git checkout -b server-local-$(date +%F) && git add -A && git commit -m "server-local changes" && git push -u origin HEAD
```
Then go back to `main`. The same mistake cost a day in Phase 11; `server-phase11-wip` is still on the
remote as the record of it.

## 2. Set the API token — do this before anything else
```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```
Put it in `.env` as `JARVIS_API_TOKEN=...`. Until it is set, `/health` reports
`api_auth.protects: "nothing"` and **any page open in your browser can POST to this API**, including
the kill switch and the directive resume. "It only listens on localhost" is not authentication:
a browser will send a request to 127.0.0.1 from any site you happen to be visiting.

## 3. Turn the new phases on, one at a time
The defaults in `.env.example` are deliberate. 14A and 14B are **on** by default because they make
the numbers honest; 15 is **off** because it places orders.

```ini
# 14A — depth-aware fills (on by default; leave it on)
JARVIS_PAPER_DEPTH_AWARE=true
JARVIS_PAPER_MAX_SLIPPAGE=0.02
JARVIS_PAPER_MIN_DEPTH_USD=5

# 14B — costs (on by default; leave it on)
JARVIS_PAPER_FEES=true
JARVIS_PAPER_FEE_MULTIPLIER=1.0
JARVIS_PAPER_GAS_USD=0

# 15 — arbitrage. Turn this on only after a few days of 14A/14B data.
JARVIS_PAPER_ARB=false
```

Restart, then check:
```bash
curl -s localhost:8000/health | python -m json.tool | head -20
curl -s localhost:8000/api/paper | python -m json.tool > /tmp/paper.json
curl -s localhost:8000/api/strategies | python -m json.tool > /tmp/strategies.json
```

## 4. What to look at, and in what order

**`/api/paper` → `capacity`.** The question this answers is "at what size does this edge
disappear?". Read `fee_share_of_gross` first: if the fee is taking most of the gross result, the
strategy is trading too close to 0.50 to be worth running. Then `max_profitable_stake` — the largest
size that both filled completely and made money with a confidence interval clear of zero. `null`
means no size qualified yet.

**`/api/paper` → `settlement` → `gain_from_exiting`.** Negative means the pre-resolution exit is
costing money and `JARVIS_PAPER_SETTLE_BEFORE_MINUTES` should change.

**`/api/trigger/status` → `book_desync`.** How often the live book had to be dropped because it
contradicted the feed's own best bid/ask. A high and rising number means the incremental book
maintenance is losing frames.

## 5. Expect the numbers to get worse
This is the point of the release, not a regression.

> **Any paper record produced before Phase 14 is void for validation purposes.** It was measured
> against a market with infinite liquidity at the best price and no cost of access. Neither is a
> market. Entries will now be refused in thin markets, stakes will be cut, realised prices will be
> worse than the quotes that triggered them, and a flat round trip will lose the fee.

A strategy that was only profitable under the old model was never profitable. The 9C gate's
confidence intervals need to be rebuilt from scratch on post-14 data: `MIN_SCORED=100` over
`MIN_DAYS=7`, so plan on at least a week before any of it means anything.

## 6. Before Phase 15 is turned on
`JARVIS_PAPER_ARB=true` makes FRIDAY buy both sides of a market. Two things to check first:

1. **Confirm the fee rates.** `jarvis/paper/costs.py` carries the published schedule, but it has
   changed more than once and public sources disagree on sports. Check Polymarket's own fee page and
   correct `TAKER_RATES` if it differs. The arbitrage decision is *entirely* a fee calculation — a
   wrong rate here turns losing trades into "opportunities".
2. **Watch `arbitrage.slippage_vs_expected`** once it is running. That is realised minus expected
   locked profit, and it measures the one thing the model cannot: whether the gaps the scan finds
   are still there by the time an order could reach the exchange. **If it is persistently negative,
   the gaps are stale and this strategy does not work, whatever the scan says.**

## 7. Live trading is not in this release
`LIVE` is hard-rejected in the Risk Governor and the validation gate cannot approve it. There is no
wallet key, no signer and no order submission anywhere in the codebase. For the arbitrage strategy
specifically, live trading would need a leg-failure policy — what to do when one leg fills and the
other does not — and that has not been designed, let alone tested. Paper trading cannot test it,
because in paper both legs always fill.
