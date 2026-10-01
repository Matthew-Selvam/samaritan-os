"""
api/ — Signal-OS API package (WS-API owns the routers, WS-SEC owns deps).

Five routers, each declaring its own prefix so `main.py` can mount them with a
bare `include_router(...)`:

    from api.investigate import router as investigate_router   # prefix /api
    from api.cases       import router as cases_router         # prefix /api/cases
    from api.agents      import router as agents_router        # prefix /api
    from api.ops         import router as ops_router, ws_router  # /api + /ws
    from api.reports     import router as reports_router       # prefix /api

`api/ops.py` additionally owns the shared compatibility layer (store
resolution, auth/rate-limit dependencies, pagination, the pipeline WebSocket
hub, PDF rendering, CORS), so the other four modules import their plumbing from
there instead of duplicating it.

Every WS-SEC module is imported defensively: this package imports cleanly and
keeps working before `security.py` / `auth.py` / `schemas.py` / `rate_limit.py`
land, and reuses them as soon as they exist.
"""