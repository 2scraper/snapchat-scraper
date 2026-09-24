"""snapchat_payload.py — reading what snapchat.com server-renders.

Generic reading of the two structured sources a Snapchat page carries.
Nothing here knows about rows or modes; `product_parser.py` does that.

Where the data is
=================
snapchat.com is a Next.js application, and every page measured on
2026-09-24 — profiles, Spotlight, Discover, Lenses, a topic page, even
the not-found page — carries its whole server-side state in ONE script:

    <script id="__NEXT_DATA__" type="application/json">{...}</script>

`props.pageProps` is the page's data. On a profile it holds the account,
its live story, its highlights, its Spotlight videos with their
engagement counts, and its Lenses — everything this repo emits, in one
request.

The page ALSO carries JSON-LD, and it is not a substitute. Counted on the
captures this repo was built from:

    @nasa          10 blocks   ProfilePage, BreadcrumbList, 7 VideoObject, ItemList
    @mrbeast        6 blocks   ProfilePage, BreadcrumbList, 3 VideoObject, ItemList
                               (against FOUR Spotlight videos in the payload)
    @kyliejenner    0 blocks   the page opens on a live story

CLAUDE.md §24 records a sibling whose JSON-LD was a decoy beside the real
grid; here it is a partial, sometimes-absent view of the same data. So
`__NEXT_DATA__` is primary, and JSON-LD is read for the two facts only it
states — `dateCreated` and `dateModified` — and to tell a HIDDEN
subscriber count from a zero one (see `profile_ld`).
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

# Anchored on the element id Next.js itself looks the payload up by, not on
# any class in the rendered page: the id is part of the framework's
# contract, classes are build artefacts (CLAUDE.md §4).
_NEXT_DATA_RE = re.compile(
    r'<script\s+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)
_LD_JSON_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


class PayloadError(ValueError):
    """The page carried no payload, or one that would not parse.

    Distinct from "the payload said there is no such account", which is a
    fact about the account. Confusing the two is how a scraper reports a
    parsing bug as an empty result, or an empty result as a parsing bug
    (CLAUDE.md §20).
    """


def decode_page(raw: Any, content_type: Optional[str] = None) -> str:
    """Bytes (or str) to text, honouring a declared charset.

    CLAUDE.md §24: one site in this family serves its detail pages as
    EUC-JP and its listing pages as UTF-8, and a blind
    `bytes.decode("utf-8", "replace")` turned every title into replacement
    characters while the numbers still parsed. Snapchat is UTF-8
    everywhere measured, but the parser accepts bytes everywhere so that a
    future charset is a one-line fix rather than a silent corruption.
    """
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    charset = None
    if content_type:
        m = re.search(r"charset=([\w-]+)", content_type, re.I)
        if m:
            charset = m.group(1)
    if not charset:
        m = re.search(rb'charset=["\']?([\w-]+)', raw[:2048], re.I)
        if m:
            charset = m.group(1).decode("ascii", "ignore")
    for enc in (charset, "utf-8"):
        if not enc:
            continue
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def next_data(html: Any) -> Dict[str, Any]:
    """The whole `__NEXT_DATA__` object, or raise PayloadError."""
    text = decode_page(html)
    m = _NEXT_DATA_RE.search(text)
    if not m:
        raise PayloadError(
            f"no __NEXT_DATA__ script in the page ({len(text)} chars)")
    try:
        data = json.loads(m.group(1))
    except ValueError as exc:
        raise PayloadError(f"__NEXT_DATA__ did not parse: {exc}") from exc
    if not isinstance(data, dict):
        raise PayloadError("__NEXT_DATA__ is not an object")
    return data


def page_props(html: Any) -> Dict[str, Any]:
    """`props.pageProps`, or raise PayloadError."""
    data = next_data(html)
    props = (data.get("props") or {}).get("pageProps")
    if not isinstance(props, dict):
        raise PayloadError("__NEXT_DATA__ carried no props.pageProps")
    return props


def page_route(html: Any) -> Optional[str]:
    """The Next.js route that rendered the page (`/at/[username]` for a
    profile), or None. Which ROUTE rendered a page is what tells a profile
    apart from a Spotlight or a Discover page without reading its body."""
    try:
        route = next_data(html).get("page")
    except PayloadError:
        return None
    return route if isinstance(route, str) else None


def ld_blocks(html: Any) -> List[Dict[str, Any]]:
    """Every JSON-LD block that parses, in document order.

    A block that does not parse is skipped rather than raised: JSON-LD is
    a secondary source here, and one malformed block must not cost a row
    whose primary source is fine.
    """
    out: List[Dict[str, Any]] = []
    for raw in _LD_JSON_RE.findall(decode_page(html)):
        try:
            block = json.loads(raw)
        except ValueError:
            continue
        if isinstance(block, dict):
            out.append(block)
        elif isinstance(block, list):
            out.extend(b for b in block if isinstance(b, dict))
    return out


def profile_ld(html: Any) -> Optional[Dict[str, Any]]:
    """The page's `ProfilePage` JSON-LD block, or None when it has none."""
    for block in ld_blocks(html):
        if block.get("@type") == "ProfilePage":
            return block
    return None


def ld_follow_count(ld: Optional[Dict[str, Any]]) -> Optional[int]:
    """The FollowAction counter from a ProfilePage block.

    Returns None both when there is no block and when the block states NO
    counter — and the second case is the one that matters. Measured
    2026-09-24: on @khaby00, whose payload says `subscriberCount: "0"`,
    the JSON-LD `interactionStatistic` is an EMPTY LIST rather than a
    counter of zero. The site is saying "not shown", and that is what lets
    this repo write null instead of a zero that drags every average.
    """
    if not isinstance(ld, dict):
        return None
    entity = ld.get("mainEntity") or {}
    for stat in entity.get("interactionStatistic") or []:
        if not isinstance(stat, dict):
            continue
        kind = stat.get("interactionType") or {}
        kind = kind.get("@type") if isinstance(kind, dict) else kind
        if kind == "FollowAction":
            return to_int(stat.get("userInteractionCount"))
    return None


def value(node: Any) -> Any:
    """Unwrap Snapchat's protobuf wrapper types.

    The payload is a protobuf message serialised to JSON, so optional
    scalars arrive wrapped — `{"value": "kyliejenner"}` for a StringValue,
    `{"value": "1789825136"}` for an Int64Value — and a missing one is
    `null`. Read naively, every id is a dict.
    """
    if isinstance(node, dict) and set(node) == {"value"}:
        return node["value"]
    return node


def to_int(raw: Any) -> Optional[int]:
    """An integer from an int, a float or a numeric string; else None.

    Int64 values arrive as STRINGS ("4993", "1790220864"), because JSON
    cannot carry a 64-bit integer losslessly and protobuf's JSON mapping
    says so.
    """
    raw = value(raw)
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        return int(raw) if raw.is_integer() else None
    if isinstance(raw, str):
        s = raw.strip()
        if re.fullmatch(r"-?\d+", s):
            return int(s)
    return None


# ---------------------------------------------------------------------------
# Challenge markers
# ---------------------------------------------------------------------------
#
# snapchat.com is served by Google Cloud (`via: 1.1 google`,
# `server: API Gateway`) with no bot-management vendor in front of it that
# any capture shows. Counted 2026-09-24 across 26 served captures —
# profiles, Spotlight, Discover, Lenses, a topic page, the not-found page —
# every candidate below scored ZERO, and so did the bare word `captcha`.
#
# So this set is not a list of what Snapchat has been seen to render. No
# challenge has been observed. But one IS configured: the profile page's
# own Content-Security-Policy permits scripts from `hcaptcha.com`,
# `*.hcaptcha.com` and `www.google.com/recaptcha` (measured 2026-09-24).
# CLAUDE.md §18's point exactly — "no challenge rendered" is not "no
# captcha configured" — and the reason the first two loaders below are the
# site's own candidates rather than a vendor list copied from a sibling. It is the loader paths of the widgets a
# solver in this family can handle, chosen to be SPECIFIC: CLAUDE.md §24
# counted 21 occurrences of the bare word `captcha` on a good page fetched
# over the 2Captcha Scraping Browser, because its auto-solve extension
# injects hunter scripts into every page. A loader path is injected by
# nothing.
#
# `cf-turnstile` is NOT here, deliberately: that same extension injects it
# (CLAUDE.md §8, §19). `challenges.cloudflare.com` is, and CLAUDE.md §23
# records a site where it was INVERTED because the site loaded Turnstile
# itself — which Snapchat does not, 0 on 26 served pages. Count it again on
# any new route before trusting it.
BOT_CHALLENGE_MARKERS = (
    "recaptcha/api.js",
    "recaptcha/enterprise.js",
    "recaptcha/api2/anchor",
    "hcaptcha.com/1/api.js",
    # A RENDERED hCaptcha widget's own iframe path — what a browser DOM holds
    # once the loader has run, when the loader tag itself may be gone.
    "hcaptcha.com/captcha/",
    "challenges.cloudflare.com",
    "_cf_chl_opt",
    "px-captcha",
    "captcha-delivery.com",
)


def challenge_markers_present(html: Any) -> List[str]:
    """Which challenge markers a page carries, in the order they are listed.

    A LIST rather than a bool so a caller can say which marker fired —
    the difference between a log a reader can act on and one that says
    "blocked".
    """
    text = decode_page(html)
    return [m for m in BOT_CHALLENGE_MARKERS if m in text]


# Positive-asset detection (CLAUDE.md §8, §18): a page Snapchat actually
# serves is built out of its own static host; an interstitial, a proxy's
# error page or Chromium's own network-error page is not. Counted
# 2026-09-24: 5 to 39 references on every served page, the not-found page
# included. The threshold is 2 rather than 1 so a minimal real page is
# never read as somebody else's (CLAUDE.md §17's classification-order
# trap).
SITE_ASSET_MARKERS = ("static.snapchat.com",)
MIN_ASSET_REFERENCES = 2


def asset_reference_count(html: Any) -> int:
    text = decode_page(html)
    return sum(text.count(m) for m in SITE_ASSET_MARKERS)
