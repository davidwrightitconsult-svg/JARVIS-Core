# FRIDAY Security Baseline

- No credentials in Git.
- `.env` files are ignored.
- Live trading remains disabled.
- Polymarket geoblock must be checked before any future live-order capability.
- Hermes receives least-privilege permissions.
- AI reasoning cannot bypass the deterministic Risk Governor.
- All execution actions require an auditable record.
- SSH keys are preferred over passwords.
- Firewall is enabled and only required services are exposed.
- Backups must be restorable, not merely created.
