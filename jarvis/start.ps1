Set-Location $PSScriptRoot
if (!(Test-Path .venv)) { py -m venv .venv }
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
if (!(Test-Path .env)) { Copy-Item .env.example .env }
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
