#!/usr/bin/env python3
"""puppeteer_scraper.py — Snapchat public profiles, Spotlight and stories.

    python3 puppeteer_scraper.py --url nasa
    python3 puppeteer_scraper.py --url nasa,mrbeast,kyliejenner --mode spotlight
    python3 puppeteer_scraper.py --url "https://www.snapchat.com/add/nasa" --mode story --format both

What this engine does, and why the browser is not the default
=============================================================
A Snapchat profile page is a Next.js page, and its `__NEXT_DATA__` script
carries the whole account — subscribers, bio, category, website, ids —
plus its live story, its curated highlights, up to 25 Spotlight videos
with their view, share, comment and boost counts, and its Lenses. Measured
2026-09-24 from a datacentre address with no key, no proxy, no cookies
and no account:

    GET https://www.snapchat.com/@nasa
        curl's own User-Agent      HTTP 200
        a Chrome User-Agent        HTTP 200
        thirty in a row, no delay  30 of 30 HTTP 200

So the default transport is plain HTTP, and the browser is what
`--transport auto` falls back to the moment the site refuses — which it
has not been seen to do. The page is served by Google Cloud with no
bot-management vendor in front of it on any capture.

Three modes, one request
========================
    --mode profile     one row per account
    --mode spotlight   one row per Spotlight video the profile lists
    --mode story       one row per snap: the live story and every highlight

All three read the same page, so a mode never costs an extra request and
the three cannot disagree about an account.

One page per account, and no page 2
===================================
A profile has exactly one page. `--pages` above 1 is REFUSED rather than
quietly refetching the same account N times. The page carries cursors for
Spotlight and highlights beyond what it lists; they are consumed by
Snapchat's protobuf API, which this repo does not implement, and the
sidecar says when an account has more than the page showed. `--url` takes
a comma-separated list of usernames instead, and each has its own
address, so `--concurrency` is genuinely usable here.
"""

import argparse
import json
import logging
import queue
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# Imported at MODULE level on purpose. CLAUDE.md §10: a sibling repo
# imported `launch`/`connect` inside its launch path, so the module
# imported cleanly with no pyppeteer installed — the offline suite's engine
# group never skipped, and the CI job that exists to fail on unexpected
# skips could not have caught a broken import. smoke_test.py asserts this
# import is here with an `ast` walk.
import asyncio
import concurrent.futures
from pyppeteer import launch, connect
from pyppeteer.errors import PyppeteerError, NetworkError
from pyppeteer.errors import TimeoutError as PPTimeout

from captcha_solver import (detect_recaptcha_v3, detect_recaptcha_in_page,
                            detect_hcaptcha, reconcile_detections,
                            solve_recaptcha, CaptchaUnsolvable,
                            injection_for, RECAPTCHA_DISCOVERY_JS)
from output_writer import (COMPLETE_STOP_REASONS, ROW_CLASS_BY_MODE,
                           dedupe_by_key, finish_run,
                           utc_now, EXIT_API_ERROR, EXIT_NO_PRODUCTS,
                           SOURCE_DEFAULT)
import page_flow
from page_flow import SolveBudget
from http_transport import HttpSession, TransportError
import product_parser as parser
from product_parser import (NotAProfileUrl, STATE_CONTENT,
                            STATE_UNKNOWN, STATE_USER_UNAVAILABLE,
                            detect_page_state, normalise_handle, parse_page,
                            profile_url)
from snapchat_payload import PayloadError
from proxy_pool import (from_args as proxy_pool_from_args, to_playwright, mask,
                        ROTATE_MODES, ProxyError, ProxyPool)
import env_config

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("puppeteer_scraper")

# The one name the shared logic below uses for "the driver failed". Each
# engine binds it to its own library's exception, so everything from
# `_prime_session` downwards is byte-comparable across the three — which is
# what makes "the engines must agree" checkable rather than aspirational.
DriverError = (PyppeteerError, NetworkError, PPTimeout,
               concurrent.futures.TimeoutError, TimeoutError)

# Every remote call is bounded (CLAUDE.md §8). pyppeteer's `connect`
# imposes no timeout of its own.
CDP_CONNECT_TIMEOUT = 30

# Bound HERE rather than imported by name, so every engine visibly carries
# the same surface; the parser is the one place the list is defined.
MODES = parser.MODES
DEFAULT_MODE = "profile"

# Every remote call is bounded (CLAUDE.md §8). Playwright's request API
# takes a per-call timeout and does not impose one of its own.
REQUEST_TIMEOUT_MS = 30_000
NAVIGATION_TIMEOUT_MS = 60_000

# How many account links mean "the page rendered". Must be > 1 (CLAUDE.md
# §5) — and on a profile page there is exactly one account, so the
# readiness anchor is the profile HEADER rather than a count of links.
# Named here rather than in page_flow so an engine's constant stays in an
# engine, per CLAUDE.md §1.
MIN_CARD_MATCHES = page_flow.MIN_CARD_MATCHES

# The one page per handle. Stated as a constant so the refusal in
# `parse_args` and the plan in `_run_profile` cannot disagree.
PAGES_PER_PROFILE = 1


@dataclass
class PageOutcome:
    """One fetch attempt's result, in request order rather than arrival order.

    CLAUDE.md §8: merging by arrival order makes the output depend on which
    worker finished first. Workers return these and the caller sorts.
    """
    number: int
    url: str = ""
    rows: List[Any] = field(default_factory=list)
    state: str = STATE_UNKNOWN
    status: Optional[int] = None
    blocked: bool = False
    error: Optional[str] = None
    diagnostics: Dict[str, Any] = field(default_factory=dict)
    attempted: bool = True


def _mask_credentials(text: Any) -> str:
    """Mask every credential in a string, not just the first.

    CLAUDE.md §8: a masker that handles the first occurrence prints the
    password the other four times and looks like it is working — Playwright
    repeats a CDP endpoint five times in one error, once in the message and
    four more in its call log.
    """
    import re
    out = str(text)
    out = re.sub(r"(?i)\b((?:client)?key|token|api[_-]?key|password)=[^&\s\"']+",
                 r"\1=***", out)
    out = re.sub(r"(wss?://)([^:/@\s]+):([^@\s]+)@", r"\1\2:***@", out)
    return out


def _chrome_ua(chromium_version: str) -> str:
    """A user agent built from the Chromium actually installed.

    CLAUDE.md §8: a hardcoded version drifts from whatever is installed,
    and claiming an older Chrome than the JS engine and TLS handshake
    report is itself a mismatch.
    """
    major = (chromium_version or "").split(".")[0] or "140"
    return (f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{major}.0.0.0 Safari/537.36")


def _proxy_failure(exc: Exception) -> str:
    """Name a dead proxy, or "" for anything else.

    CLAUDE.md §8: Chromium reports a dead proxy as a generic error, not a
    timeout, and the two want opposite responses — a timeout deserves
    another try at the SAME exit, a dead proxy a DIFFERENT one. Catching
    only the timeout type let this escape as a traceback in a sibling repo.
    """
    text = str(exc)
    for marker in ("ERR_PROXY_CONNECTION_FAILED", "ERR_TUNNEL_CONNECTION_FAILED",
                   "ERR_PROXY_AUTH_UNSUPPORTED", "ERR_UNEXPECTED_PROXY_AUTH",
                   "ERR_PROXY_CERTIFICATE_INVALID"):
        if marker in text:
            return marker
    return ""


class _Loop:
    """One event loop on a background thread, with enforced timeouts.

    `last_error` is class-level on purpose: the exception that explains a
    failed connect arrives on the loop's exception handler rather than on
    the awaited coroutine, so the two have to meet somewhere.

    The shared policy in `page_flow` is written against plain synchronous
    callables, which is the right shape for two of the three drivers.
    Bridging here keeps that policy in one place rather than growing an
    async copy of it that would drift.

    The second benefit is what this family's rules require: every call gets
    an explicit timeout. `.result(timeout)` returns control even when the
    browser never answers, which pyppeteer's own API does not offer.
    """

    last_error = None

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._serve, daemon=True,
                                        name="pyppeteer-loop")
        self._thread.start()

    def _serve(self):
        asyncio.set_event_loop(self.loop)
        self.loop.set_exception_handler(self._on_loop_exception)
        self.loop.run_forever()

    @staticmethod
    def _on_loop_exception(loop, context):
        # BOTH keys, not one or the other: asyncio puts its own words in
        # `message` and the library's in `exception`, and an `or` between
        # them looks at the exception and never sees the message — which is
        # why these kept printing after they were "handled". Only teardown
        # noise is swallowed; anything else still reaches the default
        # handler, because silencing the loop wholesale would hide real
        # faults under a successful-looking run.
        message = " | ".join(str(context.get(k)) for k in
                             ("exception", "message") if context.get(k))
        # Remembered, not just filtered. When the CONNECT fails, the real
        # reason lands here in a task nobody awaits, while the caller sits
        # on a coroutine that never returns — so the run would report a
        # 90-second timeout for something the service said instantly.
        exc = context.get("exception")
        if exc is not None:
            _Loop.last_error = f"{type(exc).__name__}: {exc}"
        if any(m in message for m in (
                "Target closed", "Connection closed", "No session with given id",
                "Task was destroyed but it is pending",
                # asyncio uses BOTH spellings and they are not
                # interchangeable: a dead connect surfaces as "Task
                # exception was never retrieved", and a filter carrying
                # only the "Future" wording printed a full traceback under
                # an error the engine had already handled.
                "Future exception was never retrieved",
                "Task exception was never retrieved",
                "Event loop is closed")):
            logger.debug("Ignoring teardown noise from pyppeteer: %s", message)
            return
        loop.default_exception_handler(context)

    def run(self, awaitable, timeout: Optional[float] = 60.0):
        """Run any AWAITABLE on the loop, not only a coroutine.

        `asyncio.run_coroutine_threadsafe` requires a coroutine and rejects
        anything else with "A coroutine object is required" — and pyppeteer
        is not consistent about which it hands back: `page.goto` returns a
        coroutine while `CDPSession.send` returns a Future. That difference
        cost a real bug: the fingerprint's client hints reported as failed
        while the command had in fact been dispatched, so the identity was
        applied HALF and the log said it had not been applied at all. Both
        halves of that are worse than either.
        """
        if asyncio.iscoroutine(awaitable):
            coro = awaitable
        else:
            async def _await(value):
                return await value
            coro = _await(awaitable)
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return future.result(timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(f"pyppeteer call did not return within {timeout}s")

    def close(self):
        """Stop the loop, CANCELLING whatever it still has in flight.

        Stopping it outright leaves pyppeteer's websocket reader and
        keepalive pending, and asyncio then prints "Task was destroyed but
        it is pending!" with a traceback for each — AFTER the output has
        been written. Four tracebacks under a successful run is how a
        reader learns to ignore the log.
        """
        def _cancel_and_stop():
            pending = [t for t in asyncio.all_tasks(self.loop)
                       if t is not asyncio.current_task(self.loop)]
            for task in pending:
                task.cancel()
            self.loop.stop()
        try:
            self.loop.call_soon_threadsafe(_cancel_and_stop)
            self._thread.join(timeout=5)
        except RuntimeError:
            pass


# A `fetch` made from inside the page.
#
# On the site's own origin, so it carries the same cookies, the same proxy
# and the same user agent the browser has — which is the whole reason a
# browser is involved at all. A function EXPRESSION, never an evaluated
# string: Snapchat's CSP carries `'wasm-unsafe-eval'` and NOT
# `'unsafe-eval'` (measured 2026-09-24), so an evaluated string dies with
# EvalError here — CLAUDE.md §18 records a sibling whose run died on
# exactly that.
_FETCH_JS = """
async (spec) => {
  const init = {method: spec.method, headers: spec.headers,
                credentials: 'include'};
  if (spec.body) { init.body = spec.body; }
  const response = await fetch(spec.url, init);
  return {status: response.status, text: await response.text()};
}
"""


class _BrowserSession:
    """A browser, a page on snapchat.com, and a fetch primitive bound to it.

    A rotation is a FRESH BROWSER (CLAUDE.md §8): cookies a bot manager
    issued against exit A and replayed from exit B are a stronger signal
    than either address alone. So this object is torn down and rebuilt
    rather than having its proxy swapped underneath it.
    """

    def __init__(self, loop, browser, page, proxy_url: Optional[str],
                 client_version: str, user_agent: Optional[str],
                 owns_browser: bool = True):
        self.loop = loop
        self.browser = browser
        self.context = browser
        self.page = page
        self.proxy_url = proxy_url
        self.client_version = client_version
        self.user_agent = user_agent
        # False when we CONNECTED to somebody else's browser rather than
        # launching one. It decides how this session ends, and getting it
        # wrong is not cosmetic — see `close`.
        self.owns_browser = owns_browser
        self._url = ""

    # -- transport ---------------------------------------------------------

    def _fetch(self, url: str, method: str = "GET",
               headers: Optional[Dict[str, str]] = None,
               body: Optional[str] = None):
        spec = {"url": url, "method": method, "headers": headers or {},
                "body": body}
        try:
            return self.loop.run(self.page.evaluate(_FETCH_JS, spec),
                                 timeout=REQUEST_TIMEOUT_MS / 1000.0)
        except Exception as exc:                       # noqa: BLE001
            raise _TransportError(_mask_credentials(exc)) from exc

    def get_text(self, url: str) -> Tuple[Optional[int], Optional[str]]:
        result = self._fetch(url) or {}
        return result.get("status"), result.get("text")

    def post_json(self, url: str, headers: Dict[str, str],
                  body: Dict[str, Any]) -> Tuple[Optional[int], Any]:
        result = self._fetch(url, "POST", headers,
                             json.dumps(body)) or {}
        status, text = result.get("status"), result.get("text")
        try:
            return status, json.loads(text) if text else None
        except (TypeError, ValueError):
            # A refusal is not JSON. Hand the body back as text so the
            # classifier can name it rather than the run dying on a decode.
            return status, text

    def goto(self, url: str) -> Optional[int]:
        response = self.loop.run(
            self.page.goto(url, {"waitUntil": "domcontentloaded",
                                 "timeout": NAVIGATION_TIMEOUT_MS}),
            timeout=NAVIGATION_TIMEOUT_MS / 1000.0 + 5)
        self._url = url
        return getattr(response, "status", None)

    def content(self) -> str:
        try:
            return self.loop.run(self.page.content(), timeout=30) or ""
        except Exception:                              # noqa: BLE001
            return ""

    def count_selector(self, selector: str) -> int:
        try:
            found = self.loop.run(self.page.querySelectorAll(selector),
                                  timeout=30)
            return len(found or [])
        except Exception:                              # noqa: BLE001
            return 0

    def evaluate(self, js: str, arg: Any = None) -> Any:
        """Run a function EXPRESSION in the page. See `_FETCH_JS` above."""
        try:
            coro = (self.page.evaluate(js, arg) if arg is not None
                    else self.page.evaluate(js))
            return self.loop.run(coro, timeout=30)
        except Exception:                              # noqa: BLE001
            return None

    @property
    def url(self) -> str:
        try:
            return self.page.url or self._url
        except Exception:
            return self._url

    def close(self):
        """End the session — and over CDP, end OURS rather than theirs.

        `Browser.close()` in pyppeteer sends `Browser.close` over the
        protocol, which tells the browser on the other end to shut down.
        That is right for a Chromium this process launched and WRONG for a
        Scraping Browser profile we merely connected to: it ends a remote
        session somebody is paying for, and the next run against the same
        `pid` meets whatever state that left behind. `disconnect()` closes
        our WebSocket and leaves the browser alone.

        Playwright's `connect_over_cdp` disconnects on `close()` by
        definition, so its engine needs no equivalent — but it must not
        close a CONTEXT it adopted rather than created, which is the same
        mistake one level down.
        """
        try:
            if self.owns_browser:
                self.loop.run(self.browser.close(), timeout=20)
            else:
                self.loop.run(self.browser.disconnect(), timeout=20)
        except Exception:
            pass
        try:
            self.loop.close()
        except Exception:
            pass


class _TransportError(RuntimeError):
    """A transport-level failure, already masked."""


class RemoteBrowserError(RuntimeError):
    """The Scraping Browser API refused the connection."""


# ---------------------------------------------------------------------------
# Launching
# ---------------------------------------------------------------------------


def _launch_local(pw, args, pool: Optional[ProxyPool]) -> _BrowserSession:
    """A local Chromium, optionally behind one exit from the pool."""
    from proxy_pool import split_credentials

    loop = _Loop()
    proxy_url = pool.current if pool else (args.proxy or None)
    launch_args = ["--no-sandbox", "--disable-dev-shm-usage"]
    host_only, username, password = (None, None, None)
    if proxy_url:
        host_only, username, password = split_credentials(proxy_url)
        # Credentials NEVER go into argv — `--proxy-server=` becomes part
        # of the browser's command line, readable by anything that can run
        # `ps` and kept in shell history (CLAUDE.md §8). The host and port
        # stay, because which exit a run used is the point of the log and
        # is not the secret.
        launch_args.append(f"--proxy-server={host_only}")

    browser = loop.run(launch(headless=args.headless, args=launch_args,
                              handleSIGINT=False, handleSIGTERM=False,
                              handleSIGHUP=False), timeout=90)
    page = loop.run(browser.newPage(), timeout=30)

    if username:
        # The one place pyppeteer CAN authenticate a proxy.
        loop.run(page.authenticate({"username": username,
                                    "password": password}), timeout=30)

    user_agent = None
    fingerprint = None
    if args.fingerprint:
        from fingerprint_client import get_fingerprint, fingerprint_user_agent
        fingerprint = get_fingerprint(args.twocaptcha_key, tags=args.fp_tags,
                                      country=args.fp_country)
        user_agent = fingerprint_user_agent(fingerprint)
    if not user_agent:
        version = loop.run(browser.version(), timeout=20) or ""
        user_agent = _chrome_ua(version.split("/")[-1] if "/" in version
                                else version)
    loop.run(page.setUserAgent(user_agent), timeout=20)
    loop.run(page.setViewport({"width": 1366, "height": 900}), timeout=20)
    if fingerprint is not None:
        _apply_fingerprint(loop, page, fingerprint, user_agent)
    return _BrowserSession(loop, browser, page, proxy_url,
                           "", user_agent)


def _apply_fingerprint(loop, page, fingerprint, user_agent) -> None:
    """Give the identity everything the fingerprint states, not just a UA.

    CLAUDE.md §24 measured a BARE user-agent override being served on the
    first navigation and refused on the next three, while a complete
    identity was served throughout. `page.setUserAgent` on its own is that
    bare override: it changes `navigator.userAgent` and leaves
    `navigator.userAgentData` — and the `Sec-CH-UA` header — reporting the
    real browser.

    So this engine applies the same set its Playwright twin does: the
    client hints beside the user agent, the screen, the timezone, and the
    init script that carries `navigator.languages`, the platform and the
    WebGL strings. Best effort throughout — a fingerprint is cover, and no
    run should die because cover was imperfect.
    """
    from fingerprint_client import (user_agent_metadata, accept_language,
                                    playwright_init_script)

    # The init script goes in through the RAW protocol command, not through
    # `page.evaluateOnNewDocument`. That wrapper treats its argument as a
    # function EXPRESSION and emits `(<arg>)(…)`, so the shared module's
    # ready-to-run `(() => {…})();` becomes a syntax error that Chromium
    # drops in silence — the call reports success and nothing is installed.
    #
    # Measured 2026-09-21 rather than reasoned about: a run reported the
    # fingerprint applied while the page returned `deviceMemory` 8 against
    # the fingerprint's 32, `hardwareConcurrency` 4 against 32, and the
    # real SwiftShader renderer string. CLAUDE.md §1 names this exact
    # hazard — the drivers disagree about what a snippet IS — which is why
    # the shared module emits source and each engine installs it its own
    # way.
    session = None
    try:
        session = loop.run(page.target.createCDPSession(), timeout=20)
        # `Page.enable` FIRST, and it is not a formality. Without it the
        # protocol still answers `{"identifier": "1"}` — a success — and
        # never runs the script. Measured side by side on the same
        # fingerprint: without it the page reported `deviceMemory` 8,
        # with it 32, which is what the fingerprint states.
        loop.run(session.send("Page.enable", {}), timeout=20)
        loop.run(session.send("Page.addScriptToEvaluateOnNewDocument",
                              {"source": playwright_init_script(fingerprint)}),
                 timeout=20)
    except Exception as exc:                           # noqa: BLE001
        logger.warning("Could not install the fingerprint's init script: %s",
                       _mask_credentials(exc))

    screen = (fingerprint.get("screen") or {})
    if screen.get("width") and screen.get("height"):
        try:
            loop.run(page.setViewport({
                "width": int(screen.get("outerWidth") or screen["width"]),
                "height": max(400, int(screen.get("outerHeight")
                                       or screen["height"] - 120)),
                "deviceScaleFactor": float(screen.get("deviceScaleFactor") or 1),
            }), timeout=20)
        except Exception:                              # noqa: BLE001
            pass

    metadata = user_agent_metadata(fingerprint)
    if not metadata:
        logger.warning("The fingerprint carried no brand list, so its client "
                       "hints are left alone: a HALF identity is worse than "
                       "none (CLAUDE.md §24).")
        return
    payload = {"userAgent": user_agent, "userAgentMetadata": metadata}
    language = accept_language(fingerprint)
    if language:
        payload["acceptLanguage"] = language
    platform = (fingerprint.get("navigator") or {}).get("platform")
    if platform:
        payload["platform"] = platform
    try:
        if session is None:
            session = loop.run(page.target.createCDPSession(), timeout=20)
        loop.run(session.send("Network.setUserAgentOverride", payload),
                 timeout=20)
        # Kept, never detached: detaching REVERTS the override, and the
        # call succeeds either way — measured on the Playwright twin.
        page._2captcha_cdp_session = session
        timezone = (fingerprint.get("intl") or {}).get("timeZone")
        if timezone:
            loop.run(session.send("Emulation.setTimezoneOverride",
                                  {"timezoneId": timezone}), timeout=20)
    except Exception as exc:                           # noqa: BLE001
        logger.warning("Could not apply the fingerprint's client hints (%s) — "
                       "the run continues, but navigator.userAgentData will "
                       "disagree with the user agent.",
                       _mask_credentials(exc))


def _connect_remote(pw, args) -> _BrowserSession:
    """Connect to the 2Captcha Scraping Browser API over CDP.

    Never sets a user agent, a fingerprint or a proxy on top: the remote
    browser brings its own, and stacking a second creates a contradiction
    rather than better cover (CLAUDE.md §8). Unlike chromedriver, pyppeteer
    takes a full `ws://user:pass@host:port` and authenticates on the
    WebSocket upgrade, so an authenticated endpoint works here.
    """
    # Retried, and the reason is measured — see the same passage in
    # playwright_scraper.py. Three raw WebSocket upgrades to a live
    # Scraping Browser endpoint on 2026-09-21: `HTTP 500` instantly on the
    # first, connected on the other two.
    loop = _Loop()
    browser = None
    attempts = max(1, int(getattr(args, "retries", 2)) + 1)
    for attempt in range(1, attempts + 1):
        _Loop.last_error = None
        try:
            browser = loop.run(connect(browserWSEndpoint=args.cdp_endpoint),
                               timeout=CDP_CONNECT_TIMEOUT)
            break
        except Exception as exc:                       # noqa: BLE001
            # The awaited call times out; the REAL reason is whatever the
            # loop's handler caught. Preferring it turns "did not return
            # within 30s" into "server rejected WebSocket connection: HTTP
            # 500", which is the difference between checking your network
            # and reading CLAUDE.md §20.
            reason = _mask_credentials(_Loop.last_error or exc)
            if attempt >= attempts or "profile_locked" in reason:
                loop.close()
                raise RemoteBrowserError(
                    f"could not connect to --cdp-endpoint: "
                    f"{reason}") from exc
            logger.warning("Scraping Browser refused the WebSocket upgrade "
                           "(%s) — attempt %d/%d, retrying in %.1fs. This is "
                           "usually the service, not the request.",
                           str(reason).strip()[:120], attempt, attempts,
                           args.retry_delay)
            time.sleep(args.retry_delay)
    pages = loop.run(browser.pages(), timeout=30) or []
    page = pages[0] if pages else loop.run(browser.newPage(), timeout=30)
    return _BrowserSession(loop, browser, page, None,
                           "", None,
                           owns_browser=False)


def _open_http(args, pool: Optional[ProxyPool]) -> HttpSession:
    """The default transport: the page, without a browser in front.

    Snapchat server-renders the whole account object, so a browser buys
    nothing on this route — it only costs a Chromium start per run. The
    browser is what `--transport auto` falls back to when the site
    actually challenges, which on this route it has not.
    """
    proxy_url = pool.current if pool else (args.proxy or None)
    user_agent = _chrome_ua("")
    if args.fingerprint:
        # An HTTP client can carry the user agent and the language list but
        # not the client hints, the platform or the WebGL strings — so it
        # can only ever wear HALF an identity, which CLAUDE.md §24 measures
        # as worse than none on a site that scores self-consistency.
        # Refused rather than half-applied.
        logger.warning("--fingerprint is ignored on the HTTP transport: an "
                       "HTTP client cannot carry client hints, so it would "
                       "wear half an identity, which is worse than none. "
                       "Use --transport browser for a full one.")
    return HttpSession(proxy_url, user_agent, "")


def _open_session(pw, args, pool: Optional[ProxyPool]):
    if getattr(args, "transport", "auto") in ("auto", "http") \
            and not args.cdp_endpoint:
        return _open_http(args, pool)
    session = (_connect_remote(pw, args) if args.cdp_endpoint
               else _launch_local(pw, args, pool))
    if args.proxy_rotate == "per-run" or not pool:
        logger.info("Browser up%s", f" via {mask(session.proxy_url)}"
                    if session.proxy_url else "")
    return session


# ---------------------------------------------------------------------------
# Bootstrapping the session on the site's own origin
# ---------------------------------------------------------------------------


def _prime_session(session, args, url: str) -> Optional[int]:
    """Load a real page so a browser context carries the site's own cookies.

    A no-op on the HTTP transport, which has no cookie jar worth warming
    and would only pay for one extra page.

    This does NOT read a client version out of the page: Snapchat's profile
    route takes no such parameter, and carrying the call anyway would be a
    request per run that nothing consumes.
    """
    if isinstance(session, HttpSession):
        return None
    status = None
    try:
        status = session.goto(url)
    except DriverError as exc:
        failure = _proxy_failure(exc)
        if failure:
            raise
        logger.warning("Could not open %s (%s) — the fetch is tried anyway.",
                       url, _mask_credentials(exc))
    # Bounded, and deliberately short: the readiness wait is insurance
    # against a page that has not painted, not the fetch itself. A profile
    # page's payload is in the SOURCE, so a run whose wait times out still
    # parses correctly — which is why this warns rather than failing.
    page_flow.wait_for_count(session.count_selector,
                             page_flow.ready_selector(args.mode),
                             page_flow.min_matches(args.mode),
                             timeout_ms=min(10_000,
                                            page_flow.content_timeout_ms(
                                                args.mode)))
    return status


def handle_captcha_if_present(session, args, budget: SolveBudget) -> bool:
    """Route to the browser handler, or say why there is nothing to do.

    The annotation on this used to promise a `_BrowserSession`, which
    stopped being true the moment a transport without a page existed. On
    the HTTP transport there is no document to inject a token into and no
    DOM to detect a widget in, so this returns False and says so ONCE per
    run rather than per page — a warning repeated forty times is a warning
    nobody reads.
    """
    if isinstance(session, HttpSession):
        if args.solve_captcha != "never" and not getattr(
                args, "_http_solve_warned", False):
            args._http_solve_warned = True
            logger.warning("A challenge cannot be solved on the HTTP "
                           "transport: there is no page to inject a token "
                           "into. --transport auto (the default) starts a "
                           "browser when the site refuses.")
        return False
    return _handle_captcha_in_browser(session, args, budget)


def _handle_captcha_in_browser(session, args,
                               budget: SolveBudget) -> bool:
    """Detect and, if it is worth paying for, solve a challenge.

    Both call sites — before classification and after — go through the same
    `SolveBudget`, which is the CLAUDE.md §23 fix: `SOLVES_PER_PAGE` read
    like an enforced limit in every repo in this family and was not one,
    because only the second of the two calls was counted. One page bought
    three solves on a site where a challenge rendered on every fetch.

    A missing key or a solver error is a WARNING and the run continues
    (CLAUDE.md §8): detection is not the same as blocking, and a run that
    already has data must not die because a solve failed.
    """
    if args.solve_captcha == "never":
        return False
    html = session.content()
    if not html:
        return False
    static = detect_recaptcha_v3(html, session.url)
    live = None
    try:
        live = detect_recaptcha_in_page(session.evaluate, session.url)
    except Exception:                              # noqa: BLE001
        live = None
    # hCaptcha first: it carries a `g-recaptcha-response` field and a
    # `data-sitekey` for compatibility, which the reCAPTCHA probe would
    # otherwise take for its own.
    challenge = (detect_hcaptcha(html, session.url)
                 or reconcile_detections(static, live))
    if challenge is None:
        return False
    if not budget.may_spend():
        logger.warning("A challenge is present and this page's solve budget "
                       "(%d) is already spent — not paying twice for one "
                       "page.", budget.limit)
        return False
    if not args.twocaptcha_key:
        logger.warning("A captcha is present and no --twocaptcha-key was "
                       "given; continuing unsolved. The run reports exit 3 "
                       "if it really was blocked.")
        return False
    if not budget.spend():
        return False
    if args.cdp_endpoint:
        # The token is MINTED over plain HTTPS from this machine and then
        # installed into a browser that is somewhere else entirely. A
        # Scraping Browser endpoint carries a `country-` segment, so the
        # solve can be issued on one continent and replayed from another —
        # and a token a challenge issuer binds to the solving address is
        # then worthless on arrival. Said out loud rather than left to be
        # discovered from a bill: nothing here can fix it, and the remedy
        # is the endpoint's own auto-solve (`Captcha.setAutoSolve`), which
        # runs where the browser is.
        logger.warning("Solving over --cdp-endpoint mints the token from "
                       "THIS machine and installs it into a remote browser, "
                       "so it may be issued on a different exit than the one "
                       "that will use it. If the token is refused, that is "
                       "the likeliest reason.")
    try:
        token = solve_recaptcha(challenge, args.twocaptcha_key,
                                min_score=args.min_score,
                                api_version=args.captcha_api)
    except CaptchaUnsolvable as exc:
        logger.warning("Captcha not solved: %s", _mask_credentials(exc))
        return False
    except Exception as exc:                      # noqa: BLE001
        logger.warning("Captcha solver failed: %s", _mask_credentials(exc))
        return False
    script, arg = injection_for(challenge, token)
    if session.evaluate(script, arg) is None:
        logger.warning("Could not inject the solved token into the page.")
        return False
    logger.info("Captcha solved and token injected.")
    return True


# ---------------------------------------------------------------------------
# One fetch, with the family's retry / block policy around it
# ---------------------------------------------------------------------------


def _dump(args, name: str, payload: Any) -> None:
    """Write the exact bytes a call returned.

    On SUCCESS too, not only on failure (CLAUDE.md §9): a run can return
    the right count with a field silently unpopulated, and then the exact
    payload is the only way to tell a parsing bug from a too-early
    snapshot.
    """
    if not args.dump_html:
        return
    path = f"{args.out}_{name}.json"
    try:
        with open(path, "w", encoding="utf-8") as handle:
            if isinstance(payload, (dict, list)):
                json.dump(payload, handle, ensure_ascii=False)
            else:
                handle.write(str(payload))
        logger.info("Wrote %s", path)
    except OSError as exc:
        logger.warning("Could not write %s: %s", path, exc)

def _call(session, args, url: str, budget: SolveBudget,
          label: str) -> Tuple[Optional[int], Any, str]:
    """GET one profile page and classify the answer. No retries here.

    Two transports, one contract: `get_text` returns `(status, text)` on
    both, so everything above this line is identical whether a browser or
    an HTTP client did the work.

    The browser path deliberately reads `content()` rather than the
    navigation response's body: a navigation can be redirected and a
    `goto` response then describes the wrong document, while `content()`
    is always the document that is actually there.
    """
    if isinstance(session, HttpSession):
        status, text = session.get_text(url)
    else:
        status = session.goto(url)
        text = session.content()
    state = detect_page_state(text, status, url)
    logger.debug("%s -> http %s, state %s", label, status, state)
    return status, text, state


def _fetch_with_policy(session_box: Dict[str, Any], pw, args,
                       pool: Optional[ProxyPool], url: str, label: str
                       ) -> Tuple[Optional[int], Any, str, bool]:
    """One call plus the retry / rotate / solve policy around it.

    `session_box` holds the live session so a rotation can replace it: a
    rotation is a fresh browser, never a proxy swapped under a live
    session (CLAUDE.md §8).
    """
    budget = SolveBudget()
    attempts = max(1, int(args.retries) + 1)
    blocked_seen = False
    status = payload = None
    state = STATE_UNKNOWN

    for attempt in range(1, attempts + 1):
        session = session_box["session"]
        # First of the two solve call sites: clear a challenge BEFORE the
        # answer is judged, so a gated page is not classified on its
        # interstitial.
        if args.solve_captcha == "always":
            handle_captcha_if_present(session, args, budget)
        try:
            status, payload, state = _call(session, args, url, budget, label)
        except (_TransportError, TransportError) as exc:
            state = parser.STATE_ERROR
            payload = str(exc)
            status = None
            exit_failed = _proxy_failure(exc)
            if exit_failed:
                # A dead proxy is NOT a timeout, and the two want opposite
                # responses: a timeout deserves another try at the SAME
                # exit, a dead proxy a DIFFERENT one. Chromium reports it
                # as a generic error rather than as a timeout, which is how
                # this escaped as a traceback in a sibling repo
                # (CLAUDE.md §8).
                logger.warning("%s failed at the EXIT, not at the site: %s "
                               "via %s. Rotating rather than retrying the "
                               "same address.", label, exit_failed,
                               mask(session.proxy_url))
                if pool:
                    pool.advance(exit_failed)
                    session_box["session"].close()
                    session_box["session"] = _open_session(pw, args, pool)
                    _prime_session(session_box["session"], args,
                                   session_box["prime_url"])
            else:
                logger.warning("%s failed after %d attempt(s): %s",
                               label, attempt, exc)

        if page_flow.counts_as_blocked(state):
            blocked_seen = True
            # `auto` means HTTP until the site says otherwise, and this is
            # otherwise. An HTTP client has nowhere to put a solved token,
            # no cookie jar a challenge issuer will accept and no DOM to
            # find a widget in, so the only useful response to a refusal is
            # to stop being an HTTP client.
            #
            # Once per run, and then never again: a site that challenged
            # once will challenge again, and flapping between transports
            # would pay the browser's start-up cost on every page while
            # looking like it was trying something new.
            if (getattr(args, "transport", "auto") == "auto"
                    and isinstance(session_box["session"], HttpSession)):
                logger.warning("%s was refused over plain HTTP (%s) — "
                               "starting a browser and retrying. This is "
                               "what --transport auto is for, and it happens "
                               "once per run.", label, state)
                session_box["session"].close()
                args.transport = "browser"
                session_box["session"] = _open_session(pw, args, pool)
                _prime_session(session_box["session"], args,
                               session_box["prime_url"])
                continue
            # Second call site, same budget.
            if page_flow.should_solve(state):
                handle_captcha_if_present(session, args, budget)

        if not page_flow.should_retry(state) or attempt >= attempts:
            break
        if page_flow.counts_as_blocked(state):
            if not page_flow.RETRY_ON_BLOCKED:
                break
            budget_left = (args.proxy_block_retries if pool
                           else page_flow.BLOCK_RETRIES_WITHOUT_POOL)
            if attempt > budget_left:
                break
            if pool and pool.rotates_per_page():
                pool.advance(f"state {state}")
                logger.info("Rotating exit and rebuilding the browser — a "
                            "rotation is a fresh browser, never a proxy "
                            "swapped under a live session.")
                session_box["session"].close()
                session_box["session"] = _open_session(pw, args, pool)
                _prime_session(session_box["session"], args,
                               session_box["prime_url"])
        logger.info("%s: state %s, retrying (%d/%d) in %.1fs",
                    label, state, attempt, attempts - 1, args.retry_delay)
        time.sleep(args.retry_delay)

    return status, payload, state, blocked_seen


# ---------------------------------------------------------------------------
# The retry / rotate policy, shared by every fetch
# ---------------------------------------------------------------------------


def _rotate_if_per_page(session_box, pw, args, pool, why: str) -> bool:
    """Take a new exit between pages, when `--proxy-rotate per-page` asked.

    This is what that mode NAMES and, before this, not what it did:
    `pool.advance()` was reached only from a dead exit or a refusal, so a
    run whose pages all succeeded stayed on one address for its whole
    life. The flag read like a traffic-spreading control and was a
    recovery control — a setting that looks configurable and is not
    (CLAUDE.md §3 says that about `.env`; it is the same defect here).

    A rotation is a FRESH BROWSER (CLAUDE.md §8): cookies a bot manager
    issued against exit A and replayed from exit B are a stronger signal
    than either address alone, so the session is torn down and rebuilt
    rather than having its proxy swapped underneath it.

    Free of mid-run consequences on this route, because there is no chain
    to break: each account is fetched by its own address and nothing one
    fetch receives is an input to the next. That is a property of the
    ROUTE, not a measurement of Snapchat's tolerance for rotation.
    """
    if not pool or not pool.rotates_per_page() or len(pool) < 2:
        return False
    pool.advance(why)
    session_box["session"].close()
    session_box["session"] = _open_session(pw, args, pool)
    _prime_session(session_box["session"], args, session_box["prime_url"])
    return True

def _worker_pool(pool: Optional[ProxyPool], worker_index: int):
    """A private ProxyPool for one worker, starting at a different exit.

    Each worker gets its OWN pool object holding the same exits rotated to
    a different offset, so workers start on distinct addresses and no
    thread needs a lock — the concurrency is safe by construction rather
    than by discipline (CLAUDE.md §7).
    """
    if not pool:
        return None
    proxies = pool.proxies
    offset = worker_index % len(proxies)
    return ProxyPool(proxies[offset:] + proxies[:offset], rotate="per-run")


# ---------------------------------------------------------------------------
# --mode profile
# ---------------------------------------------------------------------------


def _handles(args) -> List[str]:
    """`--url` to a list of usernames, refusing each bad one by name.

    A list is validated as a WHOLE before any fetch: finding out that the
    fourth handle was a search URL after three requests have been spent is
    worse than finding out before the first.
    """
    raw = str(args.url or "")
    out: List[str] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        out.append(normalise_handle(part))
    if not out:
        raise NotAProfileUrl("--url named no usernames")
    # Deduplicate while keeping order, so `--url nasa,nasa` is one fetch
    # rather than two identical rows the dedupe then drops.
    seen = set()
    unique = []
    for h in out:
        if h.lower() in seen:
            logger.info("Username %r named twice in --url; fetching it once.", h)
            continue
        seen.add(h.lower())
        unique.append(h)
    return unique


def _fetch_one_profile(session_box, pw, args, pool, handle: str,
                       scraped_at: str) -> PageOutcome:
    """One account: fetch, classify, parse. Never raises for a site answer."""
    url = profile_url(handle, args.locale)
    label = f"@{handle}"
    status, text, state, blocked = _fetch_with_policy(
        session_box, pw, args, pool, url, label)

    _dump(args, f"profile_{handle}", text)

    outcome = PageOutcome(number=1, url=url, state=state, status=status,
                          blocked=blocked)

    # The state policy decides whether to parse, rather than this engine
    # comparing the state against a literal of its own. CLAUDE.md §17: a
    # policy READ by one engine and hardcoded by its twin is how three
    # engines come to disagree about the same page.
    if not page_flow.should_parse(state):
        if state == STATE_USER_UNAVAILABLE:
            # A real answer to the question asked, not a refusal.
            logger.warning("%s: Snapchat served its own not-found page — "
                           "there is no account with this username. It is "
                           "not a block, and rotating exits will not change "
                           "it.", label)
        return outcome

    try:
        rows, diag = parse_page(text, url, scraped_at,
                                ROW_CLASS_BY_MODE[args.mode], args.mode)
    except PayloadError as exc:
        # A page Snapchat served that this parser did not understand. Its
        # own outcome, so it cannot be reported as "no such account" and
        # send the reader to check the username instead of the parser
        # (CLAUDE.md §20).
        logger.error("%s: the page was served and did not parse: %s", label, exc)
        outcome.state = parser.STATE_PARSE_ERROR
        outcome.error = str(exc)
        return outcome

    outcome.rows = rows
    outcome.diagnostics = diag
    if not rows:
        # An answer, not a failure: an ordinary account publishes no
        # Spotlight or story to a logged-out visitor, and a creator can
        # simply have no live story today.
        logger.info("%s: no %s rows (%s)", label, args.mode,
                    diag.get("empty_reason"))
    return outcome


def _fetch_profiles_concurrently(pw, args, pool, handles, scraped_at, workers):
    """N workers, each owning its own browser and its own exit.

    A worker owns ONE exit for its lifetime and starts at a different
    offset in the pool, so no thread needs a lock — the concurrency is
    safe by construction rather than by discipline (CLAUDE.md §7).
    """
    work: "queue.Queue[Tuple[int, str]]" = queue.Queue()
    for i, handle in enumerate(handles):
        work.put((i, handle))
    results: Dict[int, PageOutcome] = {}
    lock = threading.Lock()

    def worker(index: int):
        worker_pool = _worker_pool(pool, index)
        box = {"session": None, "prime_url": profile_url(handles[0])}
        try:
            box["session"] = _open_session(pw, args, worker_pool)
            _prime_session(box["session"], args, box["prime_url"])
            while True:
                try:
                    slot, handle = work.get_nowait()
                except queue.Empty:
                    return
                try:
                    outcome = _fetch_one_profile(box, pw, args, worker_pool,
                                                 handle, scraped_at)
                except Exception as exc:                      # noqa: BLE001
                    # A worker that raises must neither hang the run nor
                    # lose its siblings' pages (CLAUDE.md §10).
                    logger.error("worker %d failed on @%s: %s", index, handle,
                                 _mask_credentials(exc))
                    outcome = PageOutcome(number=slot + 1,
                                          url=profile_url(handle),
                                          state=STATE_UNKNOWN,
                                          error=_mask_credentials(exc))
                with lock:
                    results[slot] = outcome
                work.task_done()
        finally:
            if box["session"] is not None:
                box["session"].close()

    threads = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # Restored to the order --url named them, never arrival order
    # (CLAUDE.md §8).
    return [results[i] for i in sorted(results)]


def _run_profile(session_box, pw, args, pool) -> Tuple[List[Any], Dict[str, Any]]:
    handles = _handles(args)
    scraped_at = utc_now()

    # A "page" here is one ACCOUNT, and the cap is how many were named.
    # Asking for the eleventh of ten accounts is not an empty page, it is
    # an index error waiting to happen — so the plan goes through the
    # shared policy rather than being recomputed here.
    planned = page_flow.pages_to_plan(len(handles), len(handles))
    handles = handles[:planned]

    # And whether those accounts may be fetched independently is a policy
    # question, not an engine one. It is True on this route because every
    # account has its own address; the neighbouring video feed is a cursor
    # chain and would answer False.
    addressable = page_flow.pagination_is_addressable(args.url, args.mode)
    workers = min(max(1, int(args.concurrency)), len(handles))
    if workers > 1 and not addressable:
        logger.info("--concurrency lowered to 1: this route cannot be "
                    "fetched independently.")
        workers = 1
    workers = page_flow.concurrency_for_mode(args.mode, workers)
    workers = min(workers, len(handles))

    if workers > 1:
        # The session opened for the sequential path is not used by the
        # workers — each owns its own — so it is closed here rather than
        # left idle holding an exit.
        session_box["session"].close()
        session_box["session"] = _open_session(pw, args, pool)
        outcomes = _fetch_profiles_concurrently(pw, args, pool, handles,
                                                scraped_at, workers)
    else:
        outcomes = []
        for i, handle in enumerate(handles):
            if i and args.delay:
                time.sleep(args.delay)
            if i and pool and pool.rotates_per_page():
                _rotate_if_per_page(session_box, pw, args, pool,
                                    f"before @{handle}")
            outcomes.append(_fetch_one_profile(session_box, pw, args, pool,
                                               handle, scraped_at))

    rows: List[Any] = []
    seen: set = set()
    failed: List[int] = []
    unavailable: List[str] = []
    not_public: List[str] = []
    more_beyond_page: List[str] = []
    empty_slots = 0
    countries = set()
    blocked = False
    for i, outcome in enumerate(outcomes):
        blocked = blocked or outcome.blocked
        if outcome.state == STATE_USER_UNAVAILABLE:
            unavailable.append(handles[i])
            continue
        if not page_flow.should_parse(outcome.state) or outcome.error:
            failed.append(i + 1)
            continue
        diag = outcome.diagnostics or {}
        if diag.get("public_profile") is False:
            not_public.append(handles[i])
        if diag.get("more_spotlight") or diag.get("more_highlights"):
            more_beyond_page.append(handles[i])
        empty_slots += int(diag.get("spotlight_empty_slots") or 0)
        if diag.get("viewer_country"):
            countries.add(diag["viewer_country"])
        # `page` is WHICH ACCOUNT of the run a row came from, so that
        # `page`+`position` is unique across a multi-account run — CLAUDE.md
        # §18 records a sibling where 60 of 119 rows claimed a position
        # another row already had because the page was never threaded in.
        for row in outcome.rows:
            row.page = i + 1
        # Merged in the order --url named them, not arrival order.
        rows.extend(dedupe_by_key(outcome.rows, seen))

    # `stop_reason` names what ENDED the run, and the distinctions here are
    # the ones a consumer acts on.
    if blocked:
        stop_reason = "blocked"
    elif failed:
        stop_reason = "page_failed"
    elif unavailable and not rows and len(unavailable) == len(outcomes):
        # Every username asked for was not found. A complete, correct
        # answer — not an empty run and not a fetch failure.
        stop_reason = "user_unavailable"
    else:
        stop_reason = "completed"

    meta: Dict[str, Any] = {
        "stop_reason": stop_reason,
        "pages_completed": len(outcomes) - len(failed),
        "pages_failed": failed or None,
        "blocked": blocked,
        "handles_requested": len(handles),
        "handles_unavailable": unavailable or None,
        "handles_without_public_profile": not_public or None,
        # Accounts whose page carried a cursor for MORE Spotlight or
        # highlights than it listed. A run of those holds the page's slice,
        # not the account's whole history, and the sidecar says so rather
        # than leaving "complete" to be misread as "exhaustive"
        # (CLAUDE.md §21).
        "handles_with_more_than_page": more_beyond_page or None,
        "spotlight_empty_slots": empty_slots,
        # The country Snapchat says the request came from — its own
        # `viewerInfo.country`. Worth a line in the sidecar because it is
        # how a reader checks that a proxy or a Scraping Browser
        # `country-` segment did what it was asked.
        "viewer_countries": sorted(countries) or None,
    }
    return rows, meta


# Every mode reads the same page; the parser picks what to emit.
_RUNNERS = {mode: _run_profile for mode in MODES}


class _driver_context:
    """The driver's own lifetime, as a context manager.

    Playwright needs one (`sync_playwright()`); pyppeteer and Selenium do
    not, and theirs is a no-op holding the same shape. Keeping it here means
    `scrape()` and the worker loop are identical in all three files.
    """

    def __enter__(self):
        return None            # pyppeteer needs no driver-level handle

    def __exit__(self, *exc):
        return False


def scrape(args) -> int:
    pool = proxy_pool_from_args(args)
    handles = _handles(args)
    if pool and args.concurrency > 1:
        logger.info("%d worker(s) over %d exit(s).", args.concurrency, len(pool))
    elif args.concurrency > 1 and not pool:
        # Warn, do not refuse (CLAUDE.md §7).
        logger.warning("--concurrency %d with no proxy pool sends %dx the "
                       "traffic from one address, which is a faster way to "
                       "get it scored than to gather data.",
                       args.concurrency, args.concurrency)

    prime_url = profile_url(handles[0])

    rows: List[Any] = []
    meta: Dict[str, Any] = {}
    with _driver_context() as pw:
        session_box = {"session": _open_session(pw, args, pool),
                       "prime_url": prime_url}
        try:
            _prime_session(session_box["session"], args, prime_url)
            rows, meta = _RUNNERS[args.mode](session_box, pw, args, pool)
        finally:
            session_box["session"].close()

    extra = {k: v for k, v in meta.items()
             if k not in ("stop_reason", "pages_completed", "pages_failed",
                          "blocked")}
    extra["engine"] = "puppeteer"
    extra["category"] = args.category
    extra["transport"] = getattr(args, "transport", "auto")

    unavailable = meta.get("handles_unavailable")
    if unavailable:
        logger.info("%d of %d username(s) have no account: %s",
                    len(unavailable), meta.get("handles_requested"),
                    ", ".join("@" + h for h in unavailable))
    not_public = meta.get("handles_without_public_profile")
    if not_public and args.mode != "profile":
        logger.info("%d account(s) have no public profile, so Snapchat shows "
                    "them no Spotlight and no story: %s", len(not_public),
                    ", ".join("@" + h for h in not_public))
    more = meta.get("handles_with_more_than_page")
    if more and args.mode != "profile":
        logger.info("%d account(s) have more Spotlight/highlights than their "
                    "page lists; this run holds the page's slice: %s",
                    len(more), ", ".join("@" + h for h in more))

    return finish_run(
        rows, args.out, args.format, args.allow_empty,
        blocked=bool(meta.get("blocked")),
        stop_reason=meta.get("stop_reason", "completed"),
        pages_requested=len(handles),
        pages_completed=int(meta.get("pages_completed") or 0),
        pages_failed=meta.get("pages_failed") or None,
        start_url=prime_url, final_url=prime_url,
        mode=args.mode, source=SOURCE_DEFAULT, extra=extra)


def parse_args(argv: Optional[List[str]] = None):
    p = argparse.ArgumentParser(
        description="Scrape Snapchat public profiles, Spotlight videos and "
                    "stories from the profile page Snapchat server-renders "
                    "to anyone.")
    p.add_argument("--url", default=None,
                   help="A Snapchat username ('nasa', '@nasa') or a profile "
                        "URL (https://www.snapchat.com/@nasa, .../add/nasa), "
                        "or a comma-separated list of them. Falls back to "
                        "SNAPCHAT_URL from the environment or .env.")
    p.add_argument("--mode", choices=MODES, default=DEFAULT_MODE,
                   help="profile (default): one row per account. spotlight: "
                        "one row per Spotlight video the profile lists, with "
                        "views, shares, comments and boosts. story: one row "
                        "per snap in the live story and every highlight. All "
                        "three read the same page — one request per "
                        "account whichever you pick.")
    p.add_argument("--pages", type=int, default=1,
                   help="Kept for the family's flag contract and capped at "
                        "1: a Snapchat profile has exactly one page. Ask for "
                        "more accounts with a comma-separated --url instead. "
                        "A value above 1 is refused rather than silently "
                        "refetching the same account.")
    p.add_argument("--category", default=None,
                   help="Label to tag the run with in the sidecar. Defaults "
                        "to the first username. Snapchat has no category "
                        "route for accounts, so this names the RUN rather "
                        "than a site concept.")
    p.add_argument("--locale", default="en-US",
                   help="Snapchat's `?locale=` parameter. Measured "
                        "2026-09-24: it changes the CHROME and not the DATA "
                        "— en-US, fi-FI and ja-JP returned byte-identical "
                        "account and Spotlight data. Without it the page "
                        "follows the exit's country, so it is always sent; "
                        "it will not change a single column.")
    p.add_argument("--format", choices=("json", "csv", "both"), default="json")
    p.add_argument("--out", default="snapchat_profiles",
                   help="Output file prefix.")
    p.add_argument("--delay", type=float, default=0.0,
                   help="Seconds to wait between accounts.")
    p.add_argument("--retries", type=int, default=2,
                   help="Retries per request for a transient fault.")
    p.add_argument("--retry-delay", type=float, default=2.0)
    p.add_argument("--concurrency", type=int, default=1,
                   help="Workers. Genuinely usable here: every handle has "
                        "its own address, so there is no token chain to "
                        "serialise. Capped at the number of handles.")
    p.add_argument("--proxy", default=None,
                   help="One proxy URL. Credentials go through the driver's "
                        "own fields, never onto a command line.")
    p.add_argument("--proxy-file", default=None,
                   help="File of proxy URLs, one per line.")
    p.add_argument("--proxy-rotate", choices=ROTATE_MODES, default="per-run")
    p.add_argument("--proxy-shuffle", action="store_true")
    p.add_argument("--proxy-block-retries", type=int, default=2,
                   help="Exits to try when a request is refused.")
    p.add_argument("--twocaptcha-key", default=None,
                   help="2Captcha API key. Also read from TWOCAPTCHA_KEY.")
    p.add_argument("--captcha-api", choices=("v1", "v2"), default="v2")
    p.add_argument("--solve-captcha", choices=("never", "when-blocked", "always"),
                   default="when-blocked",
                   help="when-blocked (default) pays only for a page that is "
                        "actually gated. No challenge has been observed on "
                        "Snapchat from the addresses this repo was built on, "
                        "so this path is readiness rather than routine.")
    p.add_argument("--min-score", type=float, default=0.3,
                   help="Minimum reCAPTCHA v3 score to accept.")
    p.add_argument("--transport", choices=("auto", "http", "browser"),
                   default="auto",
                   help="auto (default): the profile page over plain HTTPS, "
                        "falling back to a browser if the site ever "
                        "challenges. http: never start a browser, and report "
                        "a challenge rather than trying to clear it. "
                        "browser: always drive one. The page is "
                        "server-rendered, so the browser buys nothing here "
                        "except somewhere to put a solved token. "
                        "--cdp-endpoint implies browser.")
    p.add_argument("--cdp-endpoint", default=None,
                   help="ws:// endpoint of the 2Captcha Scraping Browser API. "
                        "Also read from SNAPCHAT_CDP_ENDPOINT.")
    p.add_argument("--fingerprint", action="store_true",
                   help="Fetch a device fingerprint from the 2Captcha "
                        "Fingerprint API and apply it. Browser transport "
                        "only — see --transport.")
    p.add_argument("--fp-country", default=None)
    p.add_argument("--fp-tags", default="Windows",
                   help="ONE OS-family tag. The API rejects a list, and "
                        "rejects 'Chrome' and 'Desktop' — measured, and the "
                        "reason this default is a single word.")
    p.add_argument("--dump-html", action="store_true",
                   help="Write the exact bytes a run received, on success "
                        "too. A run can return the right count with a field "
                        "silently unpopulated.")
    p.add_argument("--allow-empty", action="store_true",
                   help="Write an empty result instead of leaving the "
                        "previous good output in place.")
    headless = p.add_mutually_exclusive_group()
    headless.add_argument("--headless", dest="headless", action="store_true",
                          default=True)
    headless.add_argument("--headful", dest="headless", action="store_false")

    args = p.parse_args(argv)
    env_config.apply(args)

    if not args.url:
        p.error("no --url given, and SNAPCHAT_URL is not set in the "
                "environment or .env.")
    try:
        handles = _handles(args)
    except NotAProfileUrl as exc:
        p.error(str(exc))

    if args.pages > PAGES_PER_PROFILE:
        p.error(
            f"--pages {args.pages} is refused: a Snapchat profile has exactly "
            "one page, so fetching more would refetch the same account and "
            "report a complete run of duplicates. Name more accounts in "
            "--url instead, comma-separated.")

    if args.cdp_endpoint and (args.proxy or args.proxy_file):
        p.error("--cdp-endpoint already proxies; attaching --proxy stacks a "
                "second exit and creates a mismatch rather than better cover.")
    if args.concurrency < 1:
        p.error("--concurrency must be at least 1.")
    if args.concurrency > 1:
        limit = page_flow.concurrency_limit(args.cdp_endpoint)
        if limit and args.concurrency > limit:
            p.error("the Scraping Browser API allows one live connection per "
                    "profile, so workers collide (profile_locked). Use "
                    "several pids, one run each.")
        if args.concurrency > len(handles):
            logger.info("--concurrency %d lowered to %d: there are only %d "
                        "account(s) to fetch.", args.concurrency,
                        len(handles), len(handles))
            args.concurrency = len(handles)
    if args.category is None:
        args.category = handles[0]
    return args


if __name__ == "__main__":
    args = parse_args()
    if args.fingerprint and not args.twocaptcha_key:
        logger.error("--fingerprint needs --twocaptcha-key (the Fingerprint "
                     "API uses the same key, though it is a separate "
                     "subscription from solving).")
        sys.exit(2)
    if args.fingerprint and args.cdp_endpoint:
        logger.warning("--fingerprint is ignored with --cdp-endpoint: the "
                       "Scraping Browser supplies its own fingerprint, and "
                       "stacking a second creates a mismatch rather than "
                       "better cover.")
    try:
        sys.exit(scrape(args))
    except NotAProfileUrl as exc:
        logger.error("%s", exc)
        sys.exit(2)
    except ProxyError as exc:
        logger.error("%s", exc)
        sys.exit(2)
    except PyppeteerError as exc:
        text = _mask_credentials(exc)
        if "profile_locked" in text or "connect to --cdp-endpoint" in text:
            logger.error("%s", text)
            sys.exit(EXIT_API_ERROR)
        raise
