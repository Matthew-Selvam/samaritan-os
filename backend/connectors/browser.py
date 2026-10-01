"""
browser.py — Playwright browser automation for CRAWLER
======================================================
Renders JS-heavy pages that a static ``httpx`` fetch returns empty for. Three
capabilities CRAWLER previously could not do:

* ``render(url)``   — wait for network-idle, return the **rendered DOM**
  (``document.documentElement.outerHTML``), which is what the static regex
  extraction in the agent actually needs.
* ``screenshot(url)`` — full-page PNG for visual review / vision models.
* ``extract(url)`` — evaluate a JS expression in the page and JSON-serialise
  the result (cookies, meta tags, XHR-visible state, framework data).

Safety, because the target URL is user-supplied:

* Every URL goes through :func:`connectors.net.fetch`-style host guarding —
  :func:`net.guard_host` runs **before** the browser is launched, and each
  request the page makes (subresources, XHR, redirects) is intercepted and
  re-checked. A public page that tries to pull ``169.254.169.254`` or
  ``http://localhost:6379`` gets that request aborted.
* ``.onion`` targets are allowed only through the Tor proxy (OPSEC).
* Size caps on the rendered HTML and the screenshot.
* Full isolation flags (``--no-sandbox`` is deliberately NOT used; we keep
  Chromium's own sandbox).

Degradation contract: if Playwright is not installed the module still imports
and :func:`run_browser` returns ``{"error": "playwright not installed",
"available": False, …}`` so CRAWLER's static httpx path continues unchanged.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from typing import Any

from . import net

#: Cap on rendered HTML returned to the agent (chars).
MAX_HTML_CHARS = 4_000_000
#: Cap on screenshot bytes written to disk.
MAX_SCREENSHOT_BYTES = 12 * 1024 * 1024
#: Default navigation timeout (ms).
DEFAULT_TIMEOUT_MS = 30_000

_viewport = (1280, 900)


# ── Availability ─────────────────────────────────────────────────────────────

def playwright_installed() -> bool:
    """``True`` when the ``playwright`` Python package can be imported."""
    try:
        import playwright.async_api  # noqa: F401  (lazy optional probe)
        return True
    except ImportError:
        return False


def browsers_installed() -> bool:
    """``True`` when Playwright *and* a browser binary are both present.

    The Python package alone is not enough — ``playwright install chromium``
    must also have run. Checked by looking for the browser download cache.
    """
    if not playwright_installed():
        return False
    for root in (os.getenv("PLAYWRIGHT_BROWSERS_PATH"),
                 os.path.expanduser("~/Library/Caches/ms-playwright"),
                 os.path.expanduser("~/.cache/ms-playwright")):
        if root and os.path.isdir(root) and any(
            name.startswith(("chromium", "chrome", "firefox", "webkit"))
            for name in os.listdir(root)
        ):
            return True
    return False


def available() -> bool:
    """``True`` when a browser session can actually be launched right now."""
    return browsers_installed()


def health() -> dict[str, Any]:
    """Report the browser stack's readiness for the OPS view.

    Returns:
        ``{"available", "playwright", "browsers", "reason"}``.
    """
    pw = playwright_installed()
    bins = browsers_installed()
    reason = None
    if not pw:
        reason = "playwright not installed — pip install playwright && playwright install chromium"
    elif not bins:
        reason = "playwright installed but no browser binary — run: playwright install chromium"
    return {"available": pw and bins, "playwright": pw, "browsers": bins, "reason": reason}


# ── Request interception (the SSRF gate for subresources) ────────────────────

def _make_router(allow_private: bool, *, use_opsec: bool, allowed_schemes: set[str]) -> Any:
    """Build a Playwright route handler that aborts unsafe subresource requests.

    A rendered page issues dozens of requests we did not ask for. Without this,
    ``http://evil.example/`` could ``<img src="http://127.0.0.1:6379/">`` and
    the browser would happily reach a private service from our network.

    Args:
        allow_private: honour ``ALLOW_PRIVATE_NETWORK`` for self-hosted targets.
        use_opsec: keep the Tor proxy for subresources.
        allowed_schemes: schemes the page may load.

    Returns:
        An async callable suitable for ``page.route("**/*", handler)``.
    """
    async def handler(route: Any, request: Any) -> None:
        url = request.url
        try:
            parsed = __import__("urllib.parse", fromlist=["urlparse"]).urlparse(url)
            scheme = (parsed.scheme or "").lower()
            host = parsed.hostname or ""
        except Exception:  # noqa: BLE001
            await route.abort()
            return

        if scheme not in allowed_schemes:
            await route.abort()
            return
        # data:/blob: are page-local, never network — but we exclude them from
        # the allowlist so a page cannot smuggle bytes through them either.
        try:
            await net.guard_host(host, allow_private=allow_private)
        except Exception:  # noqa: BLE001 — any guard failure aborts the subresource
            await route.abort()
            return
        try:
            await route.continue_()
        except Exception:  # noqa: BLE001
            await route.abort()

    return handler


# ── Core operations ──────────────────────────────────────────────────────────

async def render(
    url: str,
    *,
    wait_until: str = "networkidle",
    timeout_ms: int = DEFAULT_TIMEOUT_MS,
    wait_for_selector: str | None = None,
    scroll: bool = False,
    screenshot_path: str | None = None,
    user_agent: str | None = None,
    allow_private: bool | None = None,
    use_opsec: bool = True,
) -> dict[str, Any]:
    """Render *url* in a real browser and return the post-JS DOM.

    Args:
        url: absolute http(s) URL (or ``.onion`` with OPSEC on).
        wait_until: Playwright load-state — ``load``, ``domcontentloaded``,
            ``networkidle``, ``commit``.
        timeout_ms: navigation timeout.
        wait_for_selector: extra CSS selector to await after load.
        scroll: scroll to the bottom once (triggers lazy-loaded content).
        screenshot_path: also save a full-page PNG here.
        user_agent: override the browser UA string.
        allow_private: permit private-network targets (defaults to the env flag).
        use_opsec: route through Tor when OPSEC is enabled.

    Returns:
        ``{"html", "title", "url", "final_url", "status", "screenshot_path",
        "aborted_requests", "console", "available", "error"}``. On failure
        ``html`` is ``""`` and ``error`` explains why. Never raises.
    """
    result: dict[str, Any] = {
        "html": "", "title": None, "url": url, "final_url": url, "status": None,
        "screenshot_path": None, "aborted_requests": [], "console": [],
        "available": available(), "error": None,
    }

    if not playwright_installed():
        result["error"] = "playwright not installed"
        result["available"] = False
        return result
    if not browsers_installed():
        result["error"] = "playwright browser binary missing — run: playwright install chromium"
        result["available"] = False
        return result

    # ── Guard the top-level URL before touching the network ────────────────
    from urllib.parse import urlparse
    parsed = urlparse(url or "")
    if (parsed.scheme or "").lower() not in net.ALLOWED_SCHEMES:
        result["error"] = f"unsupported scheme: {parsed.scheme or '(none)'}"
        return result
    try:
        await net.guard_host(parsed.hostname or "", allow_private=allow_private)
    except net.NetError as exc:
        result["error"] = f"blocked: {exc}"
        return result

    proxy = net._opsec_proxy() if use_opsec else None
    pw = None
    browser = None
    try:
        from playwright.async_api import async_playwright  # lazy, optional

        pw = await async_playwright().start()
        launch_kwargs: dict[str, Any] = {
            "headless": True,
            "args": [
                "--disable-dev-shm-usage", "--no-first-run",
                "--no-default-browser-check", "--disable-background-networking",
                "--disable-sync", "--metrics-recording-only",
            ],
        }
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}
            launch_kwargs["args"].append("--proxy-bypass-list=<-loopback>")
        browser = await pw.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            viewport={"width": _viewport[0], "height": _viewport[1]},
            user_agent=user_agent or net.DEFAULT_UA,
            java_script_enabled=True,
            ignore_https_errors=False,
        )
        page = await context.new_page()
        page.set_default_timeout(timeout_ms)

        aborted: list[str] = []

        async def _handler(route: Any, request: Any) -> None:
            url_ = request.url
            try:
                p = __import__("urllib.parse", fromlist=["urlparse"]).urlparse(url_)
                scheme = (p.scheme or "").lower()
                host = p.hostname or ""
            except Exception:  # noqa: BLE001
                await route.abort()
                return
            # data:/blob:/about: are page-local and never leave the browser.
            if scheme in ("data", "blob", "about"):
                await route.continue_()
                return
            if scheme not in net.ALLOWED_SCHEMES:
                if len(aborted) < 25:
                    aborted.append(f"{scheme}: {url_[:160]}")
                await route.abort()
                return
            try:
                await net.guard_host(host, allow_private=allow_private)
            except Exception:  # noqa: BLE001
                if len(aborted) < 25:
                    aborted.append(url_[:160])
                await route.abort()
                return
            try:
                await route.continue_()
            except Exception:  # noqa: BLE001
                await route.abort()

        await page.route("**/*", _handler)
        page.on("console", lambda m: result["console"].append(f"{m.type}: {m.text}"[:200]))

        response = await page.goto(url, wait_until=wait_until, timeout=timeout_ms)
        if response is not None:
            result["status"] = response.status
        if wait_for_selector:
            try:
                await page.wait_for_selector(wait_for_selector, timeout=min(timeout_ms, 15_000))
            except Exception:  # noqa: BLE001 — the page still renders without it
                pass
        if scroll:
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await page.wait_for_timeout(800)
            except Exception:  # noqa: BLE001
                pass

        html = await page.content()
        result["html"] = html[:MAX_HTML_CHARS]
        result["title"] = (await page.title()) or None
        result["final_url"] = page.url
        result["aborted_requests"] = aborted

        if screenshot_path:
            shot = await _screenshot(page, screenshot_path)
            result["screenshot_path"] = shot if not isinstance(shot, str) else None
            if isinstance(shot, str):
                result["error"] = shot
        return result

    except Exception as exc:  # noqa: BLE001 — never crash a pipeline
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    finally:
        for closable in (locals().get("context"), browser):
            if closable is None:
                continue
            try:
                await closable.close()
            except Exception:  # noqa: BLE001 — teardown must never mask the result
                pass
        try:
            if pw is not None:
                await pw.stop()
        except Exception:  # noqa: BLE001
            pass


async def _screenshot(page: Any, path: str, *, full_page: bool = True) -> str | None:
    """Save a screenshot, honouring the size cap.

    Args:
        page: an open Playwright page.
        path: destination path.
        full_page: capture the whole scroll height.

    Returns:
        ``None`` on success, else an error message.
    """
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
        await page.screenshot(path=path, full_page=full_page)
        if os.path.getsize(path) > MAX_SCREENSHOT_BYTES:
            return "screenshot exceeded the size cap and was discarded"
        return None
    except Exception as exc:  # noqa: BLE001
        return f"screenshot failed: {exc}"


async def evaluate(url: str, expression: str, **kw: Any) -> dict[str, Any]:
    """Evaluate a JS expression in a rendered page and return the value.

    Args:
        url: absolute http(s) URL.
        expression: JavaScript source; its resolved value is JSON-serialised.
        **kw: forwarded to :func:`render`.

    Returns:
        ``{"value", "html", "title", "url", "available", "error"}``. Never raises.
    """
    if not playwright_installed() or not browsers_installed():
        return {"value": None, "html": "", "title": None, "url": url,
                "available": False, "error": "playwright not installed"}
    from urllib.parse import urlparse
    parsed = urlparse(url or "")
    if (parsed.scheme or "").lower() not in net.ALLOWED_SCHEMES:
        return {"value": None, "html": "", "title": None, "url": url,
                "available": available(), "error": "unsupported scheme"}

    proxy = net._opsec_proxy() if kw.get("use_opsec", True) else None
    pw = browser = None
    try:
        from playwright.async_api import async_playwright  # lazy, optional

        try:
            await net.guard_host(parsed.hostname or "", allow_private=kw.get("allow_private"))
        except net.NetError as exc:
            return {"value": None, "html": "", "title": None, "url": url,
                    "available": available(), "error": f"blocked: {exc}"}

        pw = await async_playwright().start()
        launch: dict[str, Any] = {"headless": True,
                                  "args": ["--disable-dev-shm-usage", "--no-first-run",
                                           "--disable-background-networking"]}
        if proxy:
            launch["proxy"] = {"server": proxy}
        browser = await pw.chromium.launch(**launch)
        context = await browser.new_context(user_agent=net.DEFAULT_UA)
        page = await context.new_page()
        await page.route("**/*", _make_router(kw.get("allow_private"),
                                              use_opsec=kw.get("use_opsec", True),
                                              allowed_schemes=net.ALLOWED_SCHEMES))
        await page.goto(url, wait_until=kw.get("wait_until", "networkidle"),
                        timeout=kw.get("timeout_ms", DEFAULT_TIMEOUT_MS))
        value = await page.evaluate(expression)
        return {"value": value, "html": (await page.content())[:MAX_HTML_CHARS],
                "title": await page.title(), "url": page.url,
                "available": True, "error": None}
    except Exception as exc:  # noqa: BLE001
        return {"value": None, "html": "", "title": None, "url": url,
                "available": available(), "error": f"{type(exc).__name__}: {exc}"}
    finally:
        try:
            if browser is not None:
                await browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if pw is not None:
                await pw.stop()
        except Exception:  # noqa: BLE001
            pass


async def screenshot(url: str, path: str | None = None, **kw: Any) -> dict[str, Any]:
    """Render *url* and capture a full-page screenshot.

    Args:
        url: absolute http(s) URL.
        path: destination; a temp file is created when omitted.
        **kw: forwarded to :func:`render`.

    Returns:
        ``{"screenshot_path", "bytes", "title", "url", "available", "error"}``.
        ``error`` is ``None`` on success. Never raises.
    """
    target = path or os.path.join(tempfile.gettempdir(), "signal-os-shot.png")
    res = await render(url, screenshot_path=target, **kw)
    out = {"screenshot_path": res.get("screenshot_path") or (None if res.get("error") else target),
           "bytes": 0, "title": res.get("title"), "url": res.get("final_url"),
           "available": res.get("available", False), "error": res.get("error")}
    if out["screenshot_path"] and os.path.isfile(out["screenshot_path"]):
        try:
            out["bytes"] = os.path.getsize(out["screenshot_path"])
        except OSError:
            pass
    return out


async def run_browser(url: str, **kwargs: Any) -> dict[str, Any]:
    """Top-level entry point: render a URL for CRAWLER.

    Args:
        url: absolute http(s) URL.
        **kwargs: ``action`` (``"render"`` | ``"screenshot"`` | ``"evaluate"``),
            ``wait_until``, ``timeout_ms``, ``wait_for_selector``, ``scroll``,
            ``screenshot_path``, ``expression``, ``allow_private``.

    Returns:
        A dict carrying the rendered DOM and metadata. When Playwright is not
        installed it is ``{"error": "playwright not installed",
        "available": False, ...}`` and the caller's static path continues.
    """
    action = str(kwargs.get("action", "render")).lower()
    try:
        if action == "screenshot":
            return await screenshot(url, kwargs.get("screenshot_path"),
                                    **{k: v for k, v in kwargs.items()
                                       if k in ("wait_until", "timeout_ms", "scroll",
                                                "allow_private", "use_opsec")})
        if action == "evaluate":
            return await evaluate(
                url, str(kwargs.get("expression", "document.title")),
                **{k: v for k, v in kwargs.items()
                   if k in ("wait_until", "timeout_ms", "allow_private", "use_opsec")},
            )
        return await render(
            url,
            wait_until=kwargs.get("wait_until", "networkidle"),
            timeout_ms=int(kwargs.get("timeout_ms", DEFAULT_TIMEOUT_MS)),
            wait_for_selector=kwargs.get("wait_for_selector"),
            scroll=bool(kwargs.get("scroll", False)),
            screenshot_path=kwargs.get("screenshot_path"),
            allow_private=kwargs.get("allow_private"),
            use_opsec=bool(kwargs.get("use_opsec", True)),
        )
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {exc}", "available": available(),
                "url": url, "html": ""}


__all__ = [
    "MAX_HTML_CHARS", "playwright_installed", "browsers_installed", "available",
    "health", "render", "evaluate", "screenshot", "run_browser",
]