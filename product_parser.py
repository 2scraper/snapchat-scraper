"""product_parser.py — this IS the site.

Everything Snapchat-shaped that is not one of the handful of named
constants in the engines lives here (CLAUDE.md §1). The generic reading of
Snapchat's payload — `__NEXT_DATA__`, JSON-LD, protobuf wrappers — lives
one file over, in `snapchat_payload.py`.

What this repo reads, and why one request is enough
===================================================
A Snapchat profile page is served to anyone. Measured 2026-09-24 from a
datacentre address with no key, no proxy, no cookies and no account:

    GET https://www.snapchat.com/@nasa
        curl's own User-Agent      HTTP 200, ~672 KB
        a Chrome User-Agent        HTTP 200
        thirty in a row, no delay  30 of 30 HTTP 200

and its `__NEXT_DATA__` carries the account, its live story, its curated
highlights, up to 25 Spotlight videos WITH their view/share/comment/boost
counts, and its Lenses. So the three modes this repo has are three views
of one response, not three routes — which is also why they cannot drift
apart from each other.

Three answers, and none of them is a refusal
============================================
    a public profile         HTTP 200   `userProfile.$case == "publicProfileInfo"`
    an ordinary account      HTTP 200   `userProfile.$case == "userInfo"` —
                                        a username and a Snapcode, nothing else
    no such account          HTTP 404   the site's own not-found page, rendered
                                        by the same route, whose payload says
                                        `pageType: "NOT_FOUND"`

The first two are both CONTENT: the account exists, and the page says what
Snapchat shows of it. The third is a real answer too — EXIT_NO_PRODUCTS,
never EXIT_BLOCKED — and CLAUDE.md §8's rule is why it matters: a scraper
that reports it as blocked sends a user rotating proxies over an account
that is not there.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from snapchat_payload import (
    MIN_ASSET_REFERENCES,
    PayloadError,
    asset_reference_count,
    challenge_markers_present,
    decode_page,
    ld_follow_count,
    next_data,
    page_props,
    profile_ld,
    to_int,
    value,
)

logger = logging.getLogger("product_parser")

SOURCE = "snapchat.com"

# ---------------------------------------------------------------------------
# Hosts and paths
# ---------------------------------------------------------------------------
#
# `snapchat.com/add/{user}` answers HTTP 308 into `www.snapchat.com/@{user}`,
# and `story.snapchat.com/@{user}` serves the same profile — so all of them
# are accepted as INPUT and normalised, and only the canonical form is ever
# emitted or requested.
CANONICAL_HOST = "www.snapchat.com"
ACCEPTED_HOSTS = ("www.snapchat.com", "snapchat.com", "story.snapchat.com",
                  "m.snapchat.com")

# Hosts that ARE Snapchat and are not this repo's job. Refusing them with
# "is not on a Snapchat host" would be false, and CLAUDE.md §5 is explicit
# that a false refusal sends the reader looking for a typo that is not
# there.
SIBLING_HOSTS = {
    "map.snapchat.com": "Snap Map, which this repo does not implement",
    "ads.snapchat.com": "Snapchat's Ads Manager, which needs an account",
    "accounts.snapchat.com": "Snapchat's account pages, which need a login",
    "lens.snapchat.com": "Lens Studio, which this repo does not implement",
}

# A username as Snapchat's own routes accept it. Deliberately LOOSER than
# the rule Snapchat states for new sign-ups (3-15 characters, starting with
# a letter): older accounts predate that rule, and @a — one character —
# answers HTTP 200 with an account. A validator stricter than the site
# refuses real accounts, which is worse than one that lets the site say
# 404.
_HANDLE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,31}$")
_PROFILE_PATH_RE = re.compile(r"^/(?:@|add/)([A-Za-z0-9][A-Za-z0-9._-]{0,31})/?$")

# The Next.js route a profile is rendered by. What tells a profile page
# from any other Snapchat page without reading its body.
PROFILE_ROUTE = "/at/[username]"

MODES = ("profile", "spotlight", "story")


class NotAProfileUrl(ValueError):
    """The URL is a Snapchat URL, but not a profile one — refused WITH THE
    REASON (CLAUDE.md §5)."""


def normalise_handle(raw: str) -> str:
    """'@NASA', 'nasa', a profile URL -> 'nasa'."""
    s = (raw or "").strip()
    if not s:
        raise NotAProfileUrl("empty username")
    if "://" in s or "snapchat.com" in s.lower():
        return handle_from_url(s)
    s = s.lstrip("@")
    if not _HANDLE_RE.match(s):
        raise NotAProfileUrl(
            f"{raw!r} is not a Snapchat username: letters, digits, '.', '_' "
            "and '-', starting with a letter or digit, up to 32 characters")
    return s.lower()


def handle_from_url(url: str) -> str:
    """Pull the username out of a profile URL, or say why it is not one."""
    s = (url or "").strip()
    if "://" not in s:
        s = "https://" + s
    parts = urlsplit(s)
    host = (parts.netloc or "").lower().split(":")[0]
    if host in SIBLING_HOSTS:
        raise NotAProfileUrl(f"{url!r} is {SIBLING_HOSTS[host]}")
    if host not in ACCEPTED_HOSTS:
        raise NotAProfileUrl(
            f"{url!r} is not on a Snapchat host (got {host!r}); this "
            f"scraper reads {', '.join(ACCEPTED_HOSTS)}")
    path = parts.path or "/"
    m = _PROFILE_PATH_RE.match(path)
    if m:
        return m.group(1).lower()
    # A path UNDER a profile — /@user/spotlight/{id}, /@user/highlight/…
    # — names the account it belongs to, so take the account rather than
    # refusing a URL a user copied from the site.
    m = re.match(r"^/@([A-Za-z0-9][A-Za-z0-9._-]{0,31})/", path)
    if m:
        return m.group(1).lower()
    if path.startswith("/spotlight"):
        kind = ("a single Spotlight video; this repo reads Spotlight from "
                "its CREATOR's profile — pass the creator's username with "
                "--mode spotlight")
    elif path.startswith("/discover"):
        kind = "the Discover feed, which this repo does not implement"
    elif path.startswith("/lens") or path.startswith("/unlock"):
        kind = "a Lens page, which this repo does not implement"
    elif path.startswith("/explore") or path.startswith("/tags"):
        kind = "a topic page, which this repo does not implement"
    elif path == "/":
        kind = "the Snapchat home page, not an account"
    else:
        kind = "not a profile path"
    raise NotAProfileUrl(
        f"{url!r} is {kind}; this scraper reads profile pages of the form "
        "https://www.snapchat.com/@username")


def profile_url(handle: str, locale: Optional[str] = None) -> str:
    """The canonical profile URL for a username.

    `locale` is Snapchat's own `?locale=` parameter. Measured 2026-09-24:
    en-US, fi-FI and ja-JP returned byte-identical account and Spotlight
    data and a translated page title — it changes the CHROME, not the
    DATA. Without it the page follows the exit's country (a Finnish
    address got `fi-FI`), which is why the engines always send one: the
    rows do not change, but a `--dump-html` capture a reader can read does.
    """
    base = f"https://{CANONICAL_HOST}/@{normalise_handle(handle)}"
    return f"{base}?locale={locale}" if locale else base


def page_url(url: str, page: int) -> str:
    """There is no page 2 of a profile — and saying so is the point.

    CLAUDE.md §18 wants "is this listing addressable?" asked per URL before
    any page URL is planned. On a Snapchat profile the page states one
    account, and the cursors it carries for more Spotlight and highlights
    are followed through Snapchat's own protobuf API, which this repo does
    not implement. So `--pages` above 1 is refused by the engines rather
    than quietly refetching the same page N times.
    """
    if page == 1:
        return url
    raise NotAProfileUrl(
        "a Snapchat profile has exactly one page; --pages above 1 is refused "
        "rather than silently refetching the same account")


# ---------------------------------------------------------------------------
# Small readers
# ---------------------------------------------------------------------------


def _text(raw: Any) -> Optional[str]:
    raw = value(raw)
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    return s or None


def _bool(raw: Any) -> Optional[bool]:
    raw = value(raw)
    return raw if isinstance(raw, bool) else None


def _epoch_to_iso(raw: Any, scale: int = 1) -> Optional[str]:
    """Unix seconds (scale 1) or milliseconds (scale 1000) to ISO-8601 UTC.

    Zero is "not set" — every empty Spotlight slot carries
    `uploadDateMs: "0"` — so it becomes null, never 1970-01-01
    (CLAUDE.md §21: a numeric field whose absent state is 0 needs the
    absence recovered).
    """
    n = to_int(raw)
    if n is None or n <= 0:
        return None
    try:
        return datetime.fromtimestamp(n / scale, tz=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


def _iso(raw: Any) -> Optional[str]:
    """An ISO timestamp from JSON-LD, normalised to second precision."""
    s = _text(raw)
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _count(raw: Any) -> Optional[int]:
    n = to_int(raw)
    return n if n is not None and n >= 0 else None


# ---------------------------------------------------------------------------
# The account
# ---------------------------------------------------------------------------

PROFILE_PUBLIC = "publicProfileInfo"
PROFILE_USER = "userInfo"


def account(props: Dict[str, Any]) -> Tuple[Optional[str], Dict[str, Any]]:
    """(kind, info) for the page's account, or (None, {}) when there is none.

    `kind` is "publicProfileInfo" or "userInfo" — Snapchat's own oneof
    case names, passed through so a new case reads as unknown rather than
    being silently folded into one of the two.
    """
    up = props.get("userProfile")
    if not isinstance(up, dict):
        return None, {}
    kind = up.get("$case")
    info = up.get(kind) if isinstance(kind, str) else None
    return (kind, info) if isinstance(info, dict) else (None, {})


def is_not_found(props: Dict[str, Any]) -> bool:
    """True when the page is Snapchat's own not-found page.

    Read from the page's OWN verdict rather than from the HTTP status, and
    that is the fix rather than a style choice. The not-found page is
    rendered by the profile route with a different shape —
    `props.pageProps` is `{"status": 2, "pageProps": {"pageMetadata":
    {"pageType": "NOT_FOUND"}}}` — and it arrives under HTTP 404 over
    plain HTTP. Selenium has no status to give, and the first live
    Selenium run classified this exact page as `parse_error`, retried it
    twice and reported a PARTIAL run (exit 6) for a username that simply
    does not exist. The payload says NOT_FOUND to every engine alike.
    """
    if not isinstance(props, dict):
        return False
    inner = props.get("pageProps") if isinstance(props.get("pageProps"), dict) else props
    meta = inner.get("pageMetadata") or {}
    return isinstance(meta, dict) and meta.get("pageType") == "NOT_FOUND"


def _filled_spotlights(props: Dict[str, Any]) -> Tuple[List[Tuple[Dict[str, Any], Dict[str, Any]]], int]:
    """(pairs of (highlight, metadata) with a real video in them, empty slots).

    The page lists Spotlight as two PARALLEL arrays — `spotlightHighlights`
    (ids, media) and `spotlightStoryMetadata` (engagement, titles) — and
    they are joined by INDEX. Checked on 44 slots across four accounts:
    wherever both are filled, the metadata's own deep link names the same
    id as the highlight at that index, 44 of 44.

    A slot can be EMPTY in both at once — 14 of 25 on @kyliejenner, stable
    over three refetches — carrying `viewCount: "0"` and no id. Those are
    placeholders, not videos with no views, and they are dropped here and
    counted.
    """
    highlights = props.get("spotlightHighlights") or []
    metas = props.get("spotlightStoryMetadata") or []
    pairs = []
    empty = 0
    for i, hl in enumerate(highlights):
        meta = metas[i] if i < len(metas) and isinstance(metas[i], dict) else {}
        if not isinstance(hl, dict) or not _text(hl.get("storyId")):
            empty += 1
            continue
        pairs.append((hl, meta))
    return pairs, empty


def _highlight_collections(props: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [h for h in (props.get("curatedHighlights") or [])
            if isinstance(h, dict)]


def _story_snaps(props: Dict[str, Any]) -> List[Dict[str, Any]]:
    story = props.get("story")
    if not isinstance(story, dict):
        return []
    return [s for s in (story.get("snapList") or []) if isinstance(s, dict)]


def _profile_row(props, html, url, scraped_at, row_cls, handle):
    kind, info = account(props)
    username = (_text(info.get("username")) or handle or "").lower() or None
    canonical = f"https://{CANONICAL_HOST}/@{username}" if username else url

    if kind != PROFILE_PUBLIC:
        # An ordinary account: Snapchat publishes its username and a
        # Snapcode, and that is the whole of what can be said. Emitted as a
        # row because "this account exists" is itself the answer a lookup
        # wants — and the not-found page is how the site says it does not.
        return row_cls(
            source=SOURCE, scraped_at=scraped_at, url=canonical,
            sku=username, title=_text(info.get("displayName")),
            username=username, public_profile=False,
            snapcode_url=_text(info.get("snapcodeImageUrl")),
            page=1, position=1)

    ld = profile_ld(html)
    entity = (ld or {}).get("mainEntity") or {}
    subscribers = _count(info.get("subscriberCount"))
    if subscribers == 0 and ld_follow_count(ld) is None:
        # Hidden, not zero — see snapchat_payload.ld_follow_count. When the
        # page carries no JSON-LD at all there is nothing to tell the two
        # apart with, and a zero on a PUBLIC profile is overwhelmingly the
        # hidden case, so it is still null: a guess presented as a fact is
        # worse than a missing value (CLAUDE.md §8).
        subscribers = None

    pairs, _ = _filled_spotlights(props)
    bpid = _text(info.get("businessProfileId"))
    return row_cls(
        source=SOURCE, scraped_at=scraped_at, url=canonical,
        sku=bpid or username,
        title=_text(info.get("title")),
        username=username,
        public_profile=True,
        business_profile_id=bpid,
        host_user_id=_text(info.get("hostUserId")),
        entity_type=_text(entity.get("@type")),
        subscriber_count=subscribers,
        spotlight_count=len(pairs),
        highlight_count=len(_highlight_collections(props)),
        story_snap_count=len(_story_snaps(props)),
        lens_count=len([x for x in (props.get("lenses") or [])
                        if isinstance(x, dict)]),
        has_story=_bool(info.get("hasStory")),
        has_curated_highlights=_bool(info.get("hasCuratedHighlights")),
        has_spotlight_highlights=_bool(info.get("hasSpotlightHighlights")),
        bio=_text(info.get("bio")),
        website_url=_text(info.get("websiteUrl")),
        address=_text(info.get("address")),
        category=_text(info.get("categoryStringId")),
        subcategory=_text(info.get("subcategoryStringId")),
        badge=to_int(info.get("badge")),
        profile_picture_url=_text(info.get("profilePictureUrl")),
        hero_image_url=_text(info.get("squareHeroImageUrl")),
        snapcode_url=_text(info.get("snapcodeImageUrl")),
        created_at=_iso((ld or {}).get("dateCreated")),
        modified_at=_iso((ld or {}).get("dateModified")),
        page=1, position=1)


# Snapchat's own filler text. Every Spotlight is `name: "Spotlight Snap"`
# unless the creator titled it, and 21 of 22 filled slots measured carry
# `description: "Another Spotlight Snap brought to you by Snapchat"`.
# Written through, a column of identical boilerplate reads as data.
_BOILERPLATE = ("Spotlight Snap", "Another Spotlight Snap brought to you by Snapchat")


def _real_text(raw: Any) -> Optional[str]:
    s = _text(raw)
    return None if s in _BOILERPLATE else s


# `contextType` values on a Spotlight's context cards, as measured on 49
# cards across four accounts: 2 is the sound ("Original Sound" /
# the creator), 3 is the creator's own card. Only 2 is read.
_CONTEXT_SOUND = 2


def _spotlight_rows(props, url, scraped_at, row_cls, handle):
    rows = []
    for hl, meta in _filled_spotlights(props)[0]:
        sid = _text(hl.get("storyId"))
        vm = meta.get("videoMetadata") or {}
        stats = meta.get("engagementStats") or {}
        creator = vm.get("creator") or {}
        person = creator.get(creator.get("$case")) if isinstance(
            creator.get("$case"), str) else None
        person = person if isinstance(person, dict) else {}
        snap = (hl.get("snapList") or [{}])[0] or {}
        urls = snap.get("snapUrls") or {}

        title = _text(meta.get("llmTitle")) or _real_text(vm.get("name"))
        sound = next((c for c in meta.get("contextCards") or []
                      if isinstance(c, dict)
                      and c.get("contextType") == _CONTEXT_SOUND), None)
        duration_ms = to_int(vm.get("durationMs"))
        # An empty stats object is a slot whose metadata did not arrive,
        # and its counts are unknown rather than zero.
        has_stats = isinstance(meta.get("engagementStats"), dict)

        rows.append(row_cls(
            source=SOURCE, scraped_at=scraped_at,
            url=f"https://{CANONICAL_HOST}/spotlight/{sid}",
            sku=sid, title=title,
            username=(_text(person.get("username")) or handle),
            creator_name=_text(person.get("name")),
            view_count=_count(stats.get("viewCount")) if has_stats else None,
            share_count=_count(stats.get("shareCount")) if has_stats else None,
            comment_count=_count(stats.get("commentCount")) if has_stats else None,
            boost_count=_count(stats.get("boostCount")) if has_stats else None,
            recommend_count=_count(stats.get("recommendCount")) if has_stats else None,
            description=_real_text(vm.get("description"))
            or _real_text(meta.get("description")),
            generated_description=_text(meta.get("llmDescription")),
            caption=_text(vm.get("embeddedTextCaption")),
            hashtags=[h for h in (meta.get("hashtags") or []) if isinstance(h, str)],
            duration_s=(round(duration_ms / 1000, 3) if duration_ms else None),
            width=to_int(vm.get("width")) or None,
            height=to_int(vm.get("height")) or None,
            uploaded_at=(_epoch_to_iso(vm.get("uploadDateMs"), 1000)
                         or _epoch_to_iso(snap.get("timestampInSec"))),
            video_url=_text(vm.get("contentUrl")) or _text(urls.get("mediaUrl")),
            thumbnail_url=(_text(vm.get("thumbnailUrl"))
                           or _text(hl.get("thumbnailUrl"))),
            sound_title=_text((sound or {}).get("title")),
            sound_artist=_text((sound or {}).get("subtitle")),
            page=1, position=len(rows) + 1))
    return rows


_MEDIA_TYPES = {0: "image", 1: "video"}

# The content id in a Snapchat CDN path: `cf-st.sc-cdn.net/d/{id}.{size}…`.
_CDN_ID_RE = re.compile(r"sc-cdn\.net/d/([A-Za-z0-9_-]{8,})")


def _cdn_id(*urls: Optional[str]) -> Optional[str]:
    for u in urls:
        m = _CDN_ID_RE.search(u or "")
        if m:
            return m.group(1)
    return None


def _snap_key(snap: Dict[str, Any], highlight_id: Optional[str]) -> Optional[str]:
    """The id a snap row is keyed on.

    A live-story snap carries its own `snapId`. A HIGHLIGHT snap does not:
    `snapId` is `{"value": ""}` on 743 of 743 highlight snaps measured
    2026-09-24. What it does carry is its media, whose CDN path names the
    content — stable across refetches (56 of 56 identical on @nasa) — and
    the same content can be saved into several highlights, so the id alone
    is not unique per row while `highlight_id/content_id` is. A few snaps
    carry no media URL at all (9 of 56 on @nasa), and fall back to their
    position inside the highlight.
    """
    sid = _text(snap.get("snapId"))
    if sid:
        return sid
    urls = snap.get("snapUrls") or {}
    cid = _cdn_id(_text(urls.get("mediaUrl")), _text(urls.get("mediaPreviewUrl")))
    if highlight_id and cid:
        return f"{highlight_id}/{cid}"
    idx = to_int(snap.get("snapIndex"))
    if highlight_id and idx is not None:
        return f"{highlight_id}#{idx}"
    return None


def _snap_row(row_cls, snap, scraped_at, url, username, collection,
              highlight_id, highlight_title, position):
    urls = snap.get("snapUrls") or {}
    return row_cls(
        source=SOURCE, scraped_at=scraped_at, url=url,
        sku=_snap_key(snap, highlight_id), title=highlight_title,
        username=username, collection=collection, highlight_id=highlight_id,
        snap_index=to_int(snap.get("snapIndex")),
        media_type=_MEDIA_TYPES.get(to_int(snap.get("snapMediaType"))),
        media_url=_text(urls.get("mediaUrl")),
        preview_url=_text(urls.get("mediaPreviewUrl")),
        posted_at=_epoch_to_iso(snap.get("timestampInSec")),
        page=1, position=position)


def _story_rows(props, url, scraped_at, row_cls, handle):
    rows = []
    for snap in _story_snaps(props):
        rows.append(_snap_row(row_cls, snap, scraped_at, url, handle,
                              "story", None, None, len(rows) + 1))
    for coll in _highlight_collections(props):
        hid = _text(coll.get("highlightId"))
        title = _text(coll.get("storyTitle"))
        for snap in coll.get("snapList") or []:
            if isinstance(snap, dict):
                rows.append(_snap_row(row_cls, snap, scraped_at, url, handle,
                                      "highlight", hid, title, len(rows) + 1))
    return rows


def parse_page(html: Any, url: str, scraped_at: str, row_cls: Any,
               mode: str = "profile") -> Tuple[List[Any], Dict[str, Any]]:
    """One profile page to (rows, diagnostics) for `mode`.

    Raises PayloadError for a page that is not a profile page, or whose
    payload does not parse — which is OUR problem and must never be
    reported as an empty account (CLAUDE.md §20). A profile with nothing
    of the kind asked for (an ordinary account in `--mode spotlight`, a
    creator with no live story) returns no rows and says why in the
    diagnostics: that is the site's answer, not a failure.
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    route = next_data(html).get("page")
    if route != PROFILE_ROUTE:
        raise PayloadError(
            f"the page was rendered by {route!r}, not by the profile route "
            f"{PROFILE_ROUTE!r}")
    props = page_props(html)
    kind, info = account(props)
    if kind is None:
        raise PayloadError("the profile page carried no userProfile")
    handle = (_text(info.get("username")) or "").lower() or None
    canonical = f"https://{CANONICAL_HOST}/@{handle}" if handle else url

    pairs, empty_slots = _filled_spotlights(props)
    diag: Dict[str, Any] = {
        "account_kind": kind,
        "public_profile": kind == PROFILE_PUBLIC,
        "spotlight_filled": len(pairs),
        "spotlight_empty_slots": empty_slots,
        "highlights": len(_highlight_collections(props)),
        "story_snaps": len(_story_snaps(props)),
        # Cursors for what lies beyond this page. Recorded, not followed:
        # they are consumed by Snapchat's protobuf API, which this repo
        # does not implement, and a non-empty one is how a reader knows
        # the page's 25 is not the account's total.
        "more_spotlight": bool(_text(props.get("spotlightHighlightsCursor"))),
        "more_highlights": bool(_text(props.get("curatedHighlightsCursor"))),
        "viewer_country": _text((props.get("viewerInfo") or {}).get("country")),
    }

    if mode == "profile":
        rows = [_profile_row(props, html, canonical, scraped_at, row_cls, handle)]
    elif kind != PROFILE_PUBLIC:
        # An ordinary account publishes no story, highlights or Spotlight
        # to a logged-out visitor. An answer, not a failure.
        diag["empty_reason"] = "no_public_profile"
        rows = []
    elif mode == "spotlight":
        rows = _spotlight_rows(props, canonical, scraped_at, row_cls, handle)
    else:
        rows = _story_rows(props, canonical, scraped_at, row_cls, handle)
    if not rows and "empty_reason" not in diag:
        diag["empty_reason"] = f"no_{mode}"
    return rows, diag


# ---------------------------------------------------------------------------
# Page states
# ---------------------------------------------------------------------------

STATE_CONTENT = "content"
# The site's own not-found page (`pageType: "NOT_FOUND"`, HTTP 404).
# A real answer to the question asked, not a block.
STATE_USER_UNAVAILABLE = "user_not_found"
STATE_CHALLENGE = "challenge"
STATE_ERROR = "error"
# A page the site plainly served that this parser could not read. OUR bug,
# named so it cannot be reported as "no such account" (CLAUDE.md §20).
STATE_PARSE_ERROR = "parse_error"
STATE_UNKNOWN = "unknown"


def _coerce_status(status: Any) -> Optional[int]:
    """An HTTP status as an int, or None — whatever type it arrived as.

    The 2Captcha Scraper API returns the upstream status as a STRING, and a
    sibling's first live run of that path crashed on `'>=' between str and
    int` (CLAUDE.md §16: run every path a credential gates).
    """
    if status is None or isinstance(status, bool):
        return None
    if isinstance(status, int):
        return status
    try:
        return int(str(status).strip())
    except (TypeError, ValueError):
        return None


def detect_page_state(html: Any, status: Optional[int] = None,
                      url: str = "") -> str:
    """Name what Snapchat answered with.

    The argument ORDER is the contract: every caller writes
    `detect_page_state(html, status, url)`. CLAUDE.md §17 records a repo
    whose engines passed `status` in the wrong place and crashed on their
    FIRST fetch; `smoke_test.py` binds every call site against this
    signature for that reason.

    The ORDER OF THE CHECKS is by how much each signal PROVES (CLAUDE.md
    §17): the site's own payload saying what it rendered outranks a
    status, and any threshold comes last.
    """
    status = _coerce_status(status)
    text = decode_page(html)

    # 1. A challenge widget's own loader. Measured zero on every served
    #    capture, so a hit is a page this site did not serve as content.
    if challenge_markers_present(text):
        return STATE_CHALLENGE

    # 2. The site's own payload. Snapchat renders its not-found page
    #    through the profile route too, and says so in its own data —
    #    which is stronger than the 404 beside it, because a gateway's 404
    #    would carry no such payload.
    try:
        data = next_data(text)
    except PayloadError:
        data = None
    if data is not None:
        route = data.get("page")
        props = (data.get("props") or {}).get("pageProps") or {}
        if route == PROFILE_ROUTE:
            kind, _ = account(props) if isinstance(props, dict) else (None, {})
            if kind is not None:
                return STATE_CONTENT
            if is_not_found(props):
                return STATE_USER_UNAVAILABLE
            return STATE_PARSE_ERROR
        # A Snapchat page of some other kind.
        if status is not None and status >= 400:
            return STATE_ERROR
        return STATE_UNKNOWN

    # 3. A status the site gave us, with no payload to explain it.
    if status is not None and status >= 400:
        return STATE_ERROR

    # 4. Only now, the threshold. A page with no payload that is built out
    #    of Snapchat's own assets is a Snapchat page this parser did not
    #    understand; one that is not is somebody else's — a proxy's error,
    #    Chromium's network-error page (which carries the site's hostname
    #    in its title and would fool a title check), a gateway.
    if asset_reference_count(text) >= MIN_ASSET_REFERENCES:
        return STATE_PARSE_ERROR
    return STATE_UNKNOWN
