"""Server settings, from the environment."""
import os
from pathlib import Path

# Split shape (bix): the host collector writes this file; the server only reads it.
SNAPSHOT = os.environ.get("JOBS_SNAPSHOT", "")
# Single-process shape (a laptop): the server runs the collector itself.
COLLECTOR_CONFIG = os.environ.get("JOBS_CONFIG", "")

# Per-task turn slices the collector writes (the viewport's Context). Unset: no
# slices, and the Context panel is empty.
CONTEXT_DIR = os.environ.get("JOBS_CONTEXT", "")
CONTEXT_DIR = Path(CONTEXT_DIR) if CONTEXT_DIR else None

# Unset: log to stdout only.
LOG_DIR = os.environ.get("LOG_DIR", "")
LOG_DIR = Path(LOG_DIR) if LOG_DIR else None

# A snapshot older than this means the collector has stopped. It rewrites at
# least every heartbeatSeconds (30 by default).
STALE_SECONDS = int(os.environ.get("STALE_SECONDS", "120"))

# Cloudflare Access in front; the app checks the JWT itself so a missing Access
# app fails closed. Unset ACCESS_AUD refuses every request, unless LOCAL_ONLY.
ACCESS_TEAM_DOMAIN = os.environ.get("ACCESS_TEAM_DOMAIN", "bixcomputer.cloudflareaccess.com")
ACCESS_AUD = os.environ.get("ACCESS_AUD", "")
# The broker's unix socket (workbench, phase 3). Unset: the Open view's routes
# answer 404 and the page's workbench features are off.
BROKER_SOCKET = os.environ.get("JOBS_BROKER_SOCKET", "")
# A laptop instance on 127.0.0.1 with no Access in front: no JWT check, and the
# Host header must be localhost or 127.0.0.1 (so DNS rebinding can't read it).
LOCAL_ONLY = os.environ.get("LOCAL_ONLY", "") == "1"
