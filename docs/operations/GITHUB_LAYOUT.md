# GitHub Repository Layout

## Core repository
FRIDAY-Autonomous-Trading-Platform

Contains source code, documentation, tests, deployment, configuration templates, Obsidian vault and phase reports.

## Data repository
FRIDAY-Trading-Data

Contains datasets, dataset manifests, checksums, experiment datasets and approved historical/operational data. Large files should use Git LFS or release/object storage as appropriate.

## Secret policy
Never commit:
- API keys
- wallet private keys
- passwords
- authentication tokens
- `.env`
- certificates/private keys unless explicitly non-secret
