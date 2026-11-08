#!/usr/bin/env python3
"""Create private deployment state without printing credentials or replacing files."""
import os
import secrets
from pathlib import Path

root = Path(__file__).resolve().parent.parent
os.umask(0o077)
env_path = root / ".env"
if env_path.exists():
    raise SystemExit("Existing .env preserved. Initialization stopped.")

for relative in ("state", "state/workspace", "auth-profiles"):
    target = root / relative
    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        os.chown(target, 1000, 1000)

content = (
    "OPENCLAW_IMAGE=ghcr.io/openclaw/openclaw:2026.10.9\n"
    "OPENCLAW_CONFIG_DIR=./state\n"
    "OPENCLAW_WORKSPACE_DIR=./state/workspace\n"
    "OPENCLAW_AUTH_PROFILE_SECRET_DIR=./auth-profiles\n"
    "OPENCLAW_GATEWAY_BIND=lan\n"
    "OPENCLAW_TZ=America/New_York\n"
    f"OPENCLAW_GATEWAY_TOKEN={secrets.token_hex(32)}\n"
)
memory_info = Path('/proc/meminfo')
if memory_info.exists():
    memory_kib = int(next(line.split()[1] for line in memory_info.read_text().splitlines()
                          if line.startswith('MemTotal:')))
    if memory_kib < 3 * 1024 * 1024:
        content += (
            "ASSISTANT_MEMORY_LIMIT=1536m\n"
            "ASSISTANT_MEMORY_SWAP_LIMIT=3g\n"
            "NODE_OPTIONS=--max-old-space-size=1024\n"
        )
with env_path.open("x") as handle:
    handle.write(content)
print("Private .env and state created. Credentials were not printed.")
print("The container uses UID 1000. On Linux, initialize as root or verify directory ownership.")
