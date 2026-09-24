"""page_flow.py — the retry / solve / blocked decision, as DATA.

Shared by all three browser engines and the HTTP path so they cannot
quietly disagree about whether a response is worth retrying, worth paying
a solver for, or worth reporting as a block. Three copies of that triage
drift, and the drift is silent: one engine reporting exit 3 where its twin
reports exit 0 on the same account (CLAUDE.md §1).

Policy and pure algorithms only. No JavaScript crosses this boundary —
Selenium's `execute_script` takes a function BODY with an explicit
`return` while Playwright and pyppeteer take `() => expr`, so a shared
module that carried a snippet would acquire one driver's dialect. What the
engines share here is a NAME for an operation and a decision about it.

Snapchat answers a profile request three ways, all measured 2026-09-24
from a datacentre address, and none of them is a refusal:

    content          HTTP 200  a public profile, OR an ordinary account
                               (username and Snapcode only) — both real
    user_not_found   HTTP 404  the site's own not-found page, rendered by
                               the profile route with `pageType: "NOT_FOUND"`
    (thirty requests in a row with no delay: 30 of 30 HTTP 200)

The states that follow them — `challenge`, `error`, `parse_error`,
`unknown` — have NOT been observed on this site. They are here because a
scraper that cannot name a refusal reports it as success, and the family
has paid for that lesson on every other site.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from product_parser import (STATE_CHALLENGE, STATE_CONTENT, STATE_ERROR,
                            STATE_PARSE_ERROR, STATE_UNKNOWN,
                            STATE_USER_UNAVAILABLE, detect_page_state)

# ---------------------------------------------------------------------------
# Readiness — for the browser engines only
# ---------------------------------------------------------------------------
#
# The browser engines do not read the account out of the DOM — it is in
# the page SOURCE, in `__NEXT_DATA__`, server-rendered. So what they wait
# for is a document that has finished arriving, not a painted grid, and a
# run that never sees the selector still parses correctly: this is a wait,
# not a gate.
#
# `script#__NEXT_DATA__` is the framework's own element id — the most
# durable anchor a Next.js page has — and `body` is the floor. A profile
# holds exactly one account, so the >1 CLAUDE.md §5 requires of a LISTING
# is not a thing that can be waited for here.
READY_SELECTOR = "script#__NEXT_DATA__, body"
MIN_CARD_MATCHES = 1
CONTENT_TIMEOUT_MS = 30_000
READY_POLL_MS = 500


def ready_selector(mode: str = "profile") -> str:
    return READY_SELECTOR


def min_matches(mode: str = "profile") -> int:
    return MIN_CARD_MATCHES


def content_timeout_ms(mode: str = "profile") -> int:
    return CONTENT_TIMEOUT_MS


def wait_for_count(count: Callable[[str], int], selector: str, minimum: int,
                   timeout_ms: int = CONTENT_TIMEOUT_MS,
                   poll_ms: int = READY_POLL_MS) -> int:
    """Poll `count(selector)` until it reaches `minimum`, or time out.

    The caller passes a counting primitive rather than a snippet, and the
    primitive must be a protocol call — `querySelectorAll` through the
    driver — never an evaluated STRING. CLAUDE.md §18: a site whose
    Content-Security-Policy lacks `unsafe-eval` kills
    `wait_for_function`-style string evaluation — and Snapchat's is such a
    site: measured 2026-09-24, its profile pages' `script-src` carries
    `'wasm-unsafe-eval'` and NOT `'unsafe-eval'`, so a string-evaluated
    wait would die with EvalError here. It is also the only spelling all
    three drivers share, since Selenium takes a function BODY where the
    other two take `() => expr`.
    """
    deadline = time.time() + (timeout_ms / 1000.0)
    seen = 0
    while True:
        try:
            seen = count(selector)
        except Exception:
            seen = 0
        if seen >= minimum or time.time() >= deadline:
            return seen
        time.sleep(poll_ms / 1000.0)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify(html, status: Optional[int] = None, url: str = "",
             mode: str = "profile") -> str:
    """Name what Snapchat answered with. See product_parser.detect_page_state.

    The argument ORDER is the contract: every caller writes
    `classify(html, status, url)`. CLAUDE.md §17 records a sibling repo
    whose two engines called `classify(html, url=...)` against a callee
    taking `status` second, and both crashed on their FIRST fetch —
    invisible to import, `--help`, `compileall` and four hundred green
    offline assertions, because none of those calls a function the way a
    live run does. `smoke_test.py` binds every call site against this
    signature for exactly that reason.

    `status` is threaded through rather than dropped. It is the only
    signal a 503 or a 403 gives, and a classifier that never receives it
    has to guess from a body that may not exist. On this site it is the
    404 that tells a missing account from a served one when the payload is
    ambiguous.
    """
    return detect_page_state(html, status, url)


STATE_POLICY = {
    # A profile page with an account on it — public or ordinary.
    STATE_CONTENT: {"retry": False, "solve": False, "blocked": False,
                    "parse": True},
    # Snapchat's not-found page: no such account. A real, complete answer — EXIT_NO_PRODUCTS,
    # never EXIT_BLOCKED. Retrying re-asks a question the site has already
    # answered, and rotating exits over it spends a proxy budget on an
    # account that is not there.
    STATE_USER_UNAVAILABLE: {"retry": False, "solve": False, "blocked": False,
                             "parse": False},
    # A captcha widget's own loader on the page. `solve` is True: the
    # loaders this state keys on are widgets captcha_solver.py handles, and
    # Snapchat's own CSP permits captcha scripts — a captcha is configured
    # here even though none has been rendered to this repo (CLAUDE.md §18:
    # "no challenge rendered" is not "no captcha configured").
    STATE_CHALLENGE: {"retry": True, "solve": True, "blocked": True,
                      "parse": False},
    # An HTTP error that is not a recognised refusal — a 500, a gateway's
    # own page, a truncated body. A wait, not a spend.
    STATE_ERROR: {"retry": True, "solve": False, "blocked": False,
                  "parse": False},
    # A page the site plainly served that this parser failed to read. OUR
    # bug (CLAUDE.md §20). One retry in case a response was truncated.
    STATE_PARSE_ERROR: {"retry": True, "solve": False, "blocked": False,
                        "parse": False},
    # Neither the site nor a recognised refusal — a proxy's own response,
    # Chromium's network-error page, an upstream error.
    STATE_UNKNOWN: {"retry": True, "solve": False, "blocked": False,
                    "parse": False},
}


def should_retry(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY[STATE_UNKNOWN])["retry"]


def should_solve(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY[STATE_UNKNOWN])["solve"]


def counts_as_blocked(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY[STATE_UNKNOWN])["blocked"]


def should_parse(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY[STATE_UNKNOWN])["parse"]


# Whether a blocked page is worth re-fetching at all. CONSULTED by the
# engines rather than merely documented — setting it False really does stop
# the retry loop. (CLAUDE.md §17: a policy constant nothing reads is the
# same defect as dead code, and this family shipped one for months.)
RETRY_ON_BLOCKED = True

# Retries to spend on a refusal when there is no pool to rotate through.
# Without a pool every retry leaves from the same address that was just
# refused, so more than one is repetition rather than a second attempt.
BLOCK_RETRIES_WITHOUT_POOL = 1

# ---------------------------------------------------------------------------
# The solve budget
# ---------------------------------------------------------------------------
#
# One paid solve per page. CLAUDE.md §23 records that this constant has
# read like an enforced limit in every repo in this family and was not one:
# every engine calls the captcha handler TWICE per attempt — once before
# the response is classified, so a challenge is cleared before anything is
# judged, and once after, for the state that says the page really is gated
# — and only the second call was counted. Measured from an address where a
# challenge rendered on every fetch, one page bought THREE solves.
#
# So the budget is not a number engines are trusted to respect. It is a
# function both call sites go through, and `smoke_test.py` asserts that the
# number of call sites equals the number of guards equals the number of
# increments.
SOLVES_PER_PAGE = 1


class SolveBudget:
    """One page's paid-solve allowance, shared by both call sites.

    `spend()` returns True at most SOLVES_PER_PAGE times and counts the
    spend itself, so neither caller can forget to.
    """

    def __init__(self, limit: int = SOLVES_PER_PAGE):
        self.limit = max(0, int(limit))
        self.spent = 0

    def may_spend(self) -> bool:
        return self.spent < self.limit

    def spend(self) -> bool:
        if not self.may_spend():
            return False
        self.spent += 1
        return True

    def __repr__(self) -> str:                # pragma: no cover - debugging
        return f"SolveBudget(spent={self.spent}/{self.limit})"


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def pagination_is_addressable(url: str = "", mode: str = "profile") -> bool:
    """Whether page N of this run can be fetched without walking to it.

    CLAUDE.md §18 says to ask this per ROUTE rather than per site. On this
    route the honest answer is that the question does not arise: a
    Snapchat profile has exactly ONE page. There is no page 2 to address, walk to,
    or construct.

    True is nonetheless the right return, and the reason is worth stating
    because the opposite reading is tempting. What a "page" means to this
    engine is one ACCOUNT, and every account has a real, independent
    address — `https://www.snapchat.com/@username`. That is what the
    concurrency machinery needs, and what makes the sidecar's "which pages
    failed by number" mean something a reader can act on.

    Deliberately NOT true of what lies past a profile's first 25 Spotlight
    videos: that is a cursor chain through Snapchat's protobuf API, which
    this repo does not implement.
    """
    return True


def pages_to_plan(pages_requested: int, pages_available: Optional[int]) -> int:
    """How many pages a run may ask for, given what is known to exist.

    Here `pages_available` is the number of handles `--url` named, and
    capping at it matters: asking for the eleventh of ten accounts is not
    an empty page, it is an index error waiting to happen.

    A profile page states no page count of its own, because it has none.
    """
    wanted = max(1, int(pages_requested or 1))
    if pages_available and pages_available > 0:
        return min(wanted, int(pages_available))
    return wanted


def concurrency_limit(cdp_endpoint: Optional[str]) -> Optional[int]:
    """1 when workers would collide, else None for "no limit imposed here".

    The Scraping Browser API allows ONE live connection per profile, so N
    workers sharing a `pid` collide with `profile_locked`. Several `pid`s,
    one run each, is the way to parallelise that path (CLAUDE.md §7).
    """
    return 1 if cdp_endpoint else None


def concurrency_for_mode(mode: str, concurrency: int) -> int:
    """Workers this mode can actually use.

    Every mode here — there is one — fetches independently addressed
    accounts, so nothing needs clamping to 1 the way a token chain does.
    The real cap is the number of handles, and the engine applies it,
    because this module does not know how many were asked for.
    """
    return max(1, int(concurrency or 1))


def sample_share(collected: int, total: Optional[int]) -> Optional[float]:
    """What fraction of the accounts asked for a run actually holds.

    CLAUDE.md §21: "complete" and "exhaustive" are different words, and a
    sidecar that says only the first is lying by omission. The distinction
    is milder on this route than on a capped search listing — a profile run
    asks for N accounts and either gets them or does not — but the number
    still belongs in the sidecar, because a run of 50 handles that holds 47
    rows and says "complete" is answering a different question from the one
    it was asked.
    """
    if not total or total <= 0 or collected < 0:
        return None
    return round(100.0 * collected / total, 4)
