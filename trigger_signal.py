"""
Standalone trigger for the SignalTrader.
Imports the running app's objects and calls signal_trader.on_run() directly.
Run this while the uvicorn service is active.
"""
import sys
import os
import json
import asyncio

# Ensure the app's config is loaded
sys.path.insert(0, "/home/kingdavid/JARVIS-Platform/JARVIS-Core")

from jarvis.app.main import _build_paper, signal_trader

if signal_trader is None:
    # If the global is None, try to build it
    _build_paper()
    from jarvis.app import main as app_main
    signal_trader = app_main.signal_trader

if signal_trader is None:
    print("ERROR: signal_trader is still None. Check JARVIS_PAPER_SIGNALS=true in .env")
    sys.exit(1)

print("Running signal_trader.on_run()...")
result = signal_trader.on_run()
print(json.dumps(result, indent=2, default=str))
