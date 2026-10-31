# Phase 13D — API authentication

## The threat this closes
"Keep it on 127.0.0.1" is good advice and does not make these routes safe. A browser sends requests
to `http://127.0.0.1:8000` from **any** page the owner has open. Before 13D, a page on the open
internet could POST to this API and the request would arrive from the loopback address looking
entirely local. The routes it could reach include:

- `/api/risk/kill` and `/api/risk/resume`
- `/api/hermes/kill`, `/api/hermes/reset`, `/api/hermes/pause`
- `/api/directives/resume` — **restarts a platform TERMINATOR deliberately shut down**
- `/api/directives/set` — rewrites the survival rules
- `/api/command` — free-text commands to Hermes

For that reason there is no "it came from localhost, so it is trusted" exemption.

## The rule
Every `POST`, `PUT`, `PATCH` and `DELETE` requires the shared secret in `JARVIS_API_TOKEN`:

```
Authorization: Bearer <token>        or        X-JARVIS-Token: <token>
```

Reads are untouched, so the dashboard works without holding a secret in browser code. Comparison is
constant-time, so a wrong token leaks nothing about the right one.

## Default is unchanged behaviour, and it says so
If `JARVIS_API_TOKEN` is unset, writes are allowed exactly as before and every such reply carries
`X-JARVIS-Auth: disabled`. `GET /health` reports `api_auth`, including the reason. That default
keeps an existing install running; it is **not** a recommendation.

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # put the result in .env
```

## Not covered
This is a shared secret, not user accounts: anyone holding it has full control. It does not replace
binding to 127.0.0.1 — keep both. Live trading remains locked regardless.
