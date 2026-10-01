"""Print the live route table.

Used to derive docs/API.md from the running app rather than from assumptions, and
as a quick way to see what the composition root actually mounts. Run from the
backend/ directory:

    cd backend && python tests/_routes.py

Handles both plain routes and `_IncludedRouter` wrappers, because
`app.include_router(...)` contributes a mount object rather than a route.
"""
import sys
from pathlib import Path

# `python tests/_routes.py` puts tests/ on sys.path, not backend/, so the
# first-party modules (main, config, agents, ...) are not importable without
# this.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main  # noqa: E402

PREFIXES = ("/api", "/ws")


def walk(routes, prefix=""):
    """Yield (methods, path) for every reachable route, flattening mounts.

    Args:
        routes: The app's `.routes`, or a nested router's.
        prefix: Path prefix accumulated from enclosing `include_router` mounts.

    Yields:
        (comma-joined methods, full path) for each leaf route.
    """
    for route in routes:
        # An included router contributes a mount object, not a route. Its real
        # prefix lives on `include_context.prefix`; the wrapped `APIRouter`
        # already carries its own `prefix` inside each sub-route's path, so the
        # two must NOT be concatenated again (doing so yields "/api/api/health").
        original = getattr(route, "original_router", None)
        if original is not None:
            context = getattr(route, "include_context", None)
            mount_prefix = getattr(context, "prefix", "") or ""
            yield from walk(getattr(original, "routes", []) or [], prefix + mount_prefix)
            continue

        path = prefix + (getattr(route, "path", "") or "")
        nested = getattr(route, "routes", None)
        if nested and not getattr(route, "path", None):
            yield from walk(nested, path)
            continue
        methods = sorted(getattr(route, "methods", None) or ["WS"])
        yield ",".join(methods), path


rows = list(walk(main.app.routes))

print("TOTAL", len(rows))
for methods, path in sorted(rows, key=lambda row: (row[1], row[0])):
    print(f"{methods:20s} {path}")
