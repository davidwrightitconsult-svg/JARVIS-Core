# FRIDAY Security Baseline

- No credentials in Git.
- `.env` files are ignored.
- Live trading remains disabled.
- Writes to the API need JARVIS_API_TOKEN (13D). Keep binding to 127.0.0.1 as well: a browser
  sends loopback requests from any page the owner has open, so "local only" is not authentication.
- Polymarket geoblock must be checked before any future live-order capability.
- Hermes receives least-privilege permissions.
- AI reasoning cannot bypass the deterministic Risk Governor.
- All execution actions require an auditable record.
- SSH keys are preferred over passwords.
- Firewall is enabled and only required services are exposed.
- Backups must be restorable, not merely created.
