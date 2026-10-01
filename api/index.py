"""
Vercel serverless entry point.

Vercel's Python runtime auto-detects an ASGI `app` in this module and serves
it directly (no WSGI/Mangum shim needed). The real FastAPI app lives in
backend/main.py — everything else in backend/ (agents, connectors, config,
opsec, router) is deployed alongside this file and resolved via the sys.path
insert below, exactly as it resolves when run locally with `backend/` as the
working directory.
"""
import os
import sys

_BACKEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
sys.path.insert(0, os.path.abspath(_BACKEND_DIR))

from main import app  # noqa: E402
