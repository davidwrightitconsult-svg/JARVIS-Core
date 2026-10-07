# FRIDAY — Autonomous Quantitative Trading Intelligence

FRIDAY is the rebranded evolution of the Polymarket AI Trading Lab. It is designed for Windows 11 / 8 GB RAM and defaults to PAPER mode.

## FRIDAY personality
- Pleasant British-English female voice target
- Intelligent, concise, quick-witted, calm under pressure
- Can challenge a requested action and propose a calculated alternative
- Learns from stored research, decisions and historical outcomes
- Never fabricates market data or claims a trade filled when it did not

## Interface
The local dashboard includes:
- voice input + typed command interface
- browser fallback British-English speech synthesis
- optional ElevenLabs TTS
- animated Global Brain strategy sphere
- live market scanner
- strategy constellation
- books/research activity
- trade/activity monitor
- account/risk summary
- FRIDAY command console

## Voice
ElevenLabs is optional. Set ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID in `.env`. If unavailable, the browser's `en-GB` speech synthesis is used.

Recommended voice-design brief: `A warm, intelligent British woman in her early thirties; poised, articulate, quick-thinking, lightly witty, calm during market stress, confident but never theatrical; natural UK English; fast but highly intelligible.`

## Install and run (Ubuntu / jarvis020)
Run from the **repository root** (FRIDAY imports the `friday` package):
```bash
cd ~/friday/FRIDAY-Autonomous-Trading-Platform
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # then add ANTHROPIC_API_KEY etc.
python -m uvicorn friday.app.main:app --host 127.0.0.1 --port 8000
```
Keep `--host 127.0.0.1`: the API (including the Hermes controls) has no
authentication yet and must not be exposed to the network.

## Hermes (Phase 3)
Hermes starts with FRIDAY and runs scheduled health checks and read-only
market scans. Free-text commands go through Hermes to the Claude
intelligence contract. Controls (local only):
```bash
curl -s localhost:8000/api/hermes/status
curl -s -X POST localhost:8000/api/hermes/pause
curl -s -X POST localhost:8000/api/hermes/resume
curl -s -X POST localhost:8000/api/hermes/kill -H 'Content-Type: application/json' -d '{"reason":"manual stop"}'
curl -s -X POST localhost:8000/api/hermes/reset -H 'Content-Type: application/json' -d '{"confirmed_by":"David"}'
```
Open http://127.0.0.1:8000

## Important
PAPER is the default. Live order execution is deliberately disabled. The scanner is not an executor. A future live adapter must pass geoblock, liquidity, spread, edge, drawdown, exposure, position-size and kill-switch gates before an order can be considered.

Only ingest books/material you legally possess or that are openly licensed/public-domain. FRIDAY converts research into hypotheses and requires quantitative validation before strategy promotion.
