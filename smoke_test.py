"""
smoke_test.py — the offline suite for snapchat-scraper.

One file of plain functions with fixtures loaded from
`fixtures_generated.json`. No pytest, no conftest, no fixtures directory
(CLAUDE.md §10); `tests/test_smoke.py` wraps this as a single pytest test so
`pytest` works as an entry point without a second copy of the checks.

    python3 smoke_test.py            run everything
    python3 smoke_test.py -v         print every check as it passes

It must pass with NO engine library installed at all: every
`import playwright_scraper` / `selenium_scraper` / `puppeteer_scraper` is
guarded and the skip is RECORDED, because "skipped, engine absent" reads
identically to a real import error. CI installs each engine in its own venv
and fails if that engine's group reports a skip.

The fixtures are cut from real captures taken 2026-09-24 by
`make_fixtures.py`, which also PROVES each trimmed fixture parses
identically to its untrimmed original — every row of every mode, and the
same page state — before writing anything.

What the fixtures deliberately reproduce
----------------------------------------
Each is a trap this repo measured, and the fixture exists so that fixing it
stays fixed:

  * @khaby00 — `subscriberCount: "0"` beside a JSON-LD
    `interactionStatistic` that is an EMPTY LIST: hidden, not zero;
  * @kyliejenner — 14 of 25 Spotlight slots EMPTY (placeholders with
    `viewCount: "0"`), a live story, 645 highlight snaps with an empty
    `snapId`, and no JSON-LD at all;
  * @nasa — an Organization, 9 highlight snaps with no media URL, and a
    highlights cursor saying the page is not the account's whole history;
  * @mrbeast — a Person with every Spotlight slot filled and a JSON-LD
    that lists 3 of the payload's 4 videos;
  * @espn — an ordinary account: a username and a Snapcode, nothing else;
  * a username with no account: HTTP 404 and `pageType: "NOT_FOUND"`,
    which must classify the same WITHOUT the status (Selenium has none);
  * @nasa under `?locale=ja-JP`, to pin that a locale moves the CHROME and
    not the DATA.

What is NOT in them is described in make_fixtures.py: other people's
comments, the translation table and the per-visit config ids are absent
because the trim keeps only what the parser reads.
"""

import argparse
import ast
import csv
import hashlib
import inspect
import io
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile
import threading
import types
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import asdict, fields

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
# `HERE` is what the family core calls this. Both names are bound because
# the shared checks below were lifted verbatim from a sibling repo, and
# renaming inside them would stop the two copies being comparable — which
# is the whole point of sharing them.
HERE = REPO_ROOT
sys.path.insert(0, REPO_ROOT)

import product_parser                                        # noqa: E402
import page_flow                                             # noqa: E402
import output_writer                                         # noqa: E402
import proxy_pool                                            # noqa: E402
import env_config                                            # noqa: E402
import snapchat_payload                                      # noqa: E402
import make_fixtures                                         # noqa: E402
from output_writer import Profile, Spotlight, Snap           # noqa: E402

FAILURES = []
PASSED = 0
SKIPS = []
VERBOSE = False

ENGINES = ("playwright_scraper", "selenium_scraper", "puppeteer_scraper")

DRIVER_IMPORTS = {
    "playwright_scraper": "playwright",
    "selenium_scraper": "selenium",
    "puppeteer_scraper": "pyppeteer",
}

# The family's CLI contract (CLAUDE.md §9). Re-derived rather than copied:
#
#   grep -ohE '"--[a-z0-9-]+"' */playwright_scraper.py | sort | uniq -c
#
# across the sibling repos in ~/2scraper.
CONTRACT_FLAGS = {
    "--url", "--pages", "--category", "--format", "--out", "--delay",
    "--retries", "--retry-delay", "--concurrency", "--proxy", "--proxy-file",
    "--proxy-rotate", "--proxy-shuffle", "--proxy-block-retries",
    "--twocaptcha-key", "--captcha-api", "--solve-captcha", "--min-score",
    "--cdp-endpoint", "--allow-empty", "--dump-html", "--headless",
    "--headful", "--mode", "--fingerprint", "--fp-country", "--fp-tags",
    "--locale",
}

# This repo's own addition. `--transport` exists because the profile page is
# server-rendered and a browser buys nothing on it, so the default is plain
# HTTP and the browser is a fallback rather than the engine.
SITE_FLAGS = {"--transport"}

# CLAUDE.md §12, and ASSEMBLED from pieces rather than written out — which
# is what lets the scan cover this file too (§22).
BANNED_WORDING = (
    "cloud " + "browser", "anti" + "detect browser",
    "2scraper Anti" + "detect Browser",
    "gate." + "2prx.com", "ANTI" + "DETECT_LOCAL_API",
)

BANNED_FLAGS = ("--anti" + "detect", "--country-code", "--country")

_FIXTURE_PATH = os.path.join(REPO_ROOT, "fixtures_generated.json")


def _load_fixtures():
    if not os.path.exists(_FIXTURE_PATH):
        raise SystemExit(
            f"fixtures_generated.json is missing from {REPO_ROOT}.\n"
            f"Regenerate it with `python3 make_fixtures.py`, which needs "
            f"your own captures — see that file's docstring. If it is "
            f"present but ignored, check .gitignore's "
            f"`!fixtures_generated.json` exception.")
    with open(_FIXTURE_PATH, encoding="utf-8") as handle:
        return json.load(handle)


FIX = _load_fixtures()
PROFILES = FIX["profiles"]

SCRAPED_AT = "2026-09-24T12:00:00Z"
FIXTURE_URL = "https://www.snapchat.com/@fixture"


def page(name):
    """One fixture back into the minimal page the parser accepts.

    The same reconstruction make_fixtures.py verifies against the original
    capture — imported rather than copied, so the two cannot drift.
    """
    return make_fixtures.as_page(PROFILES[name]["payload"])


def status_of(name):
    return PROFILES[name].get("status", 200)


def rows(name, mode="profile"):
    """Every row a fixture parses to in `mode`."""
    out, _diag = product_parser.parse_page(
        page(name), FIXTURE_URL, SCRAPED_AT,
        output_writer.ROW_CLASS_BY_MODE[mode], mode)
    return out


def row(name):
    """The single Profile row a fixture parses to, or None."""
    got = rows(name, "profile")
    return got[0] if got else None


def diag(name, mode="profile"):
    return product_parser.parse_page(
        page(name), FIXTURE_URL, SCRAPED_AT,
        output_writer.ROW_CLASS_BY_MODE[mode], mode)[1]


def props(name):
    return PROFILES[name]["payload"]["next_data"]["props"]["pageProps"]


def _SERVED_PAGES():
    """(name, html) for every capture this repo knows is good."""
    return [(n, page(n)) for n in PROFILES if n != "not_found"]


# Everything the 2Captcha Scraping Browser's auto-solve extension injects
# into a page, captured verbatim on 2026-09-22 by a sibling repo from a real
# `--cdp-endpoint` fetch of a page its site SERVED. The extension is the
# same whichever site it runs on, so the material is too.
#
# This is the fixture CLAUDE.md §24 asks every repo in this family to hold:
# 16 `chrome-extension://` tags, 4 `hunter.js`, `cf-turnstile` once,
# `data-ts-input` once and `captcha-widgets` twice. Every one of those is a
# marker some repo in this family has carried at some point.
CDP_INJECTED = (
    '<script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/conte'
    'nt/captcha/captchafox/interceptor.js"></script><script src="chrome-ext'
    'ension://kjmkgkdkpedkejedfhmfcenooemhbpbo/content/captcha/mt_captcha/i'
    'nterceptor.js"></script><script src="chrome-extension://kjmkgkdkpedkej'
    'edfhmfcenooemhbpbo/content/captcha/turnstile/interceptor.js"></script>'
    '<script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/conte'
    'nt/captcha/turnstile/hunter.js" data-ts-input="cf-turnstile-response">'
    '</script><script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhb'
    'pbo/content/captcha/amazon_waf/interceptor.js"></script><script src="c'
    'hrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/content/captcha/yan'
    'dex/interceptor.js"></script><script src="chrome-extension://kjmkgkdkp'
    'edkejedfhmfcenooemhbpbo/content/captcha/lemin/interceptor.js"></script'
    '><script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/cont'
    'ent/captcha/arkoselabs/hunter.js"></script><script src="chrome-extensi'
    'on://kjmkgkdkpedkejedfhmfcenooemhbpbo/content/captcha/arkoselabs/inter'
    'ceptor.js"></script><script src="chrome-extension://kjmkgkdkpedkejedfh'
    'mfcenooemhbpbo/content/captcha/recaptcha/interceptor.js"></script><scr'
    'ipt src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/content/c'
    'aptcha/recaptcha/hunter.js"></script><script src="chrome-extension://k'
    'jmkgkdkpedkejedfhmfcenooemhbpbo/content/captcha/keycaptcha/hunter.js">'
    '</script><script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhb'
    'pbo/content/captcha/geetest_v4/interceptor.js"></script><script src="c'
    'hrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/content/captcha/gee'
    'test/interceptor.js"></script><script src="chrome-extension://kjmkgkdk'
    'pedkejedfhmfcenooemhbpbo/content/communication_helpers.js"></script><s'
    'cript src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/content'
    '/core_helpers.js"></script><captcha-widgets></captcha-widgets>')


class _NullContext:
    """Stands in for a driver's lifetime while the browser is stubbed."""

    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def check(name, condition, detail=""):
    global PASSED
    if condition:
        PASSED += 1
        if VERBOSE:
            print("  ok   %s" % name)
    else:
        FAILURES.append("%s%s" % (name, (" — " + detail) if detail else ""))
        print("  FAIL %s%s" % (name, (" — " + detail) if detail else ""))


def equal(name, got, want):
    check(name, got == want, "got %r, want %r" % (got, want))


def skip(group, reason):
    SKIPS.append("%s: %s" % (group, reason))
    print("  SKIP %s — %s" % (group, reason))


def _argparse_flags(module_name):
    """Every --flag a module's parser defines, without running the CLI."""
    path = os.path.join(HERE, module_name + ".py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    # Only calls on the argparse parser itself. A browser's option object
    # also has `add_argument`, and counting Chrome's own switches
    # (`--no-sandbox`, `--window-size=…`) as CLI flags made this check
    # compare nonsense.
    parsers = {"p"}
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr in ("add_argument_group",
                                             "add_mutually_exclusive_group")):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    parsers.add(target.id)
    flags = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in parsers):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str) \
                        and arg.value.startswith("--"):
                    flags.add(arg.value)
    return flags


def _import_engine(name):
    try:
        return __import__(name)
    except ImportError as e:
        skip(name, "engine library absent (%s)" % e)
        return None


def _import_graph(entrypoint):
    """Every local module an entrypoint reaches, transitively."""
    local = {f[:-3] for f in os.listdir(HERE) if f.endswith(".py")}
    seen, queue = set(), [entrypoint]
    while queue:
        name = queue.pop()
        if name in seen or name not in local:
            continue
        seen.add(name)
        tree = ast.parse(open(os.path.join(HERE, name + ".py"),
                              encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                queue.extend(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                queue.append(node.module.split(".")[0])
    return seen


def _tree_state():
    result = subprocess.run(["git", "status", "--porcelain"], cwd=HERE,
                            capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return sorted(line for line in result.stdout.splitlines()
                  if not line.endswith(".pyc"))


def _fault_args(engine, **overrides):
    """Arguments for a fault-injection run, with no network in them."""
    base = dict(url="nasa", mode="profile", pages=1, locale="en-US", delay=0,
                retries=0, retry_delay=0, solve_captcha="never",
                dump_html=False, out="unused", concurrency=1,
                cdp_endpoint=None, proxy=None, proxy_file=None,
                proxy_rotate="per-run", fingerprint=False, twocaptcha_key=None,
                fp_tags=None, fp_country=None, headless=True, captcha_api="v2",
                min_score=0.3, category=None, allow_empty=False, format="json",
                proxy_block_retries=1, transport="browser")
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _with_stubs(engine, fetch, body):
    """Run `body` with the engine's transport and session stubbed out."""
    class FakeSession:
        client_version = ""
        proxy_url = None

        def close(self):
            pass

    original = (engine._open_session, engine._prime_session,
                engine._fetch_with_policy)
    engine._open_session = lambda pw, args, pool: FakeSession()
    engine._prime_session = lambda session, args, url: 200
    engine._fetch_with_policy = fetch
    try:
        return body(FakeSession)
    finally:
        (engine._open_session, engine._prime_session,
         engine._fetch_with_policy) = original


# ---------------------------------------------------------------------------
# What this site is, pinned by VALUE on real fixtures (CLAUDE.md §10)
# ---------------------------------------------------------------------------


def check_profile_values_on_real_accounts():
    """A column can be 100% populated and entirely wrong, so VALUES.

    Every figure below is what the capture of that account said on
    2026-09-24, read by hand out of the raw page before this check was
    written — not what the parser returned, which would make the check
    circular.
    """
    measured = [
        # name, business profile id, subscribers, entity, created
        ("nasa", "cb983b26-c537-474b-9699-065309205ea2", 757_800,
         "Organization", "2019-05-22T17:38:27Z"),
        ("mrbeast", "fe63dec1-4fa4-476c-9c73-19b2bc2e8589", 1_466_200,
         "Person", "2019-05-16T14:46:37Z"),
        ("kyliejenner", "e9bcdb52-a5cc-4dea-88ac-8baa91a78345", 29_262_900,
         None, None),
    ]
    for name, bpid, subs, entity, created in measured:
        r = row(name)
        equal("%s: sku is the business profile id" % name, r.sku, bpid)
        equal("%s: subscribers" % name, r.subscriber_count, subs)
        equal("%s: entity type from the JSON-LD" % name, r.entity_type, entity)
        equal("%s: created_at from the JSON-LD" % name, r.created_at, created)
        equal("%s: the url is the canonical profile" % name, r.url,
              "https://www.snapchat.com/@" + name)
        check("%s: public_profile is True" % name, r.public_profile is True)
        # Rounded by the SITE — there is no exact figure on the page.
        check("%s: the count is a multiple of 100, as Snapchat publishes it"
              % name, subs % 100 == 0)
    r = row("nasa")
    equal("nasa: page-bounded counts", (r.spotlight_count, r.highlight_count,
                                        r.story_snap_count, r.lens_count),
          (7, 10, 0, 3))
    equal("nasa: website", r.website_url, "https://www.nasa.gov")
    equal("nasa: category passed through as Snapchat's own id", r.category,
          "public-profile-category-v3-business-group")
    equal("nasa: badge passed through, not renamed", r.badge, 1)


def check_a_hidden_subscriber_count_is_null_not_zero():
    """@khaby00: `subscriberCount: "0"` on a public profile with 7 videos.

    The page's own JSON-LD carries an EMPTY `interactionStatistic` list
    rather than a counter of 0 — the site saying "not shown". Written
    through, that zero drags every average a consumer computes
    (CLAUDE.md §21).
    """
    raw = props("khaby00")["userProfile"]["publicProfileInfo"]
    equal("the fixture really does say 0", raw["subscriberCount"], "0")
    ld = PROFILES["khaby00"]["payload"]["profile_ld"]
    equal("and its JSON-LD states no counter at all",
          ld["mainEntity"]["interactionStatistic"], [])
    equal("so the row says None", row("khaby00").subscriber_count, None)
    equal("the free-text address is carried as typed",
          row("khaby00").address, "Subscribe for a Cookie")
    # And a REAL counter still reads, including one that is genuinely 0.
    equal("a FollowAction counter is read", snapchat_payload.ld_follow_count(
        {"mainEntity": {"interactionStatistic": [
            {"interactionType": {"@type": "FollowAction"},
             "userInteractionCount": 0}]}}), 0)
    equal("no block at all is None", snapchat_payload.ld_follow_count(None), None)


def check_an_ordinary_account_is_an_answer_not_a_failure():
    """@espn: HTTP 200, `userInfo` only — a username and a Snapcode.

    The account EXISTS, so `profile` emits a row saying so, and the other
    two modes emit nothing WITH A REASON: Snapchat shows a logged-out
    visitor no story or Spotlight of an ordinary account.
    """
    html = page("espn")
    equal("it is content", product_parser.detect_page_state(html, 200),
          product_parser.STATE_CONTENT)
    r = row("espn")
    equal("public_profile is False", r.public_profile, False)
    equal("its key is the username, since it publishes no id", r.sku, "espn")
    check("its Snapcode is carried", bool(r.snapcode_url))
    for mode in ("spotlight", "story"):
        equal("%s mode: no rows" % mode, rows("espn", mode), [])
        equal("%s mode: and the reason" % mode,
              diag("espn", mode).get("empty_reason"), "no_public_profile")


def check_no_account_is_not_a_block_even_without_a_status():
    """The not-found page, classified the same by every engine.

    The first live Selenium run classified this exact page `parse_error`,
    retried it twice and reported a PARTIAL run (exit 6) for a username
    that does not exist — because Selenium has no status to give, and the
    classifier leaned on the 404. The payload's own `pageType: "NOT_FOUND"`
    is what every engine can see.
    """
    html = page("not_found")
    for status in (404, None, 200):
        equal("not-found with status %r" % status,
              product_parser.detect_page_state(html, status),
              product_parser.STATE_USER_UNAVAILABLE)
    state = product_parser.STATE_USER_UNAVAILABLE
    check("which the policy does not call blocked",
          not page_flow.counts_as_blocked(state))
    check("and does not retry", not page_flow.should_retry(state))
    check("and does not pay a solver", not page_flow.should_solve(state))
    try:
        product_parser.parse_page(html, FIXTURE_URL, SCRAPED_AT, Profile)
        check("parsing it raises rather than inventing a row", False)
    except snapchat_payload.PayloadError:
        check("parsing it raises rather than inventing a row", True)


def check_empty_spotlight_slots_are_not_videos_with_no_views():
    """@kyliejenner: 14 of 25 slots are placeholders, at the same indices in
    both parallel arrays, carrying `viewCount: "0"` and no id."""
    metas = props("kyliejenner")["spotlightStoryMetadata"]
    placeholders = [m for m in metas if not m.get("engagementStats")]
    equal("the fixture really does hold 14 placeholders", len(placeholders), 14)
    check("and they really do say zero views",
          all(m["videoMetadata"]["viewCount"] == "0" for m in placeholders))
    got = rows("kyliejenner", "spotlight")
    equal("only the 11 real videos become rows", len(got), 11)
    equal("positions count the EMITTED rows, 1..11 with no holes",
          [r.position for r in got], list(range(1, 12)))
    check("no row carries a placeholder's zero",
          all(r.view_count and r.view_count > 0 for r in got))
    equal("the sidecar counts the holes",
          diag("kyliejenner", "spotlight")["spotlight_empty_slots"], 14)
    equal("and the profile row counts only real videos",
          row("kyliejenner").spotlight_count, 11)


def check_spotlight_values_on_a_real_video():
    """Pinned by value: @mrbeast's first Spotlight, as its capture says."""
    r = rows("mrbeast", "spotlight")[0]
    equal("id", r.sku, "W7_EDlXWTBiXAEEniNoMPwAAYbHZzZmtrYm1jAZhMOQmTAZhMOQfqAAAAAQ")
    equal("url", r.url, "https://www.snapchat.com/spotlight/" + r.sku)
    equal("views", r.view_count, 3_841_923)
    equal("shares", r.share_count, 881)
    equal("comments", r.comment_count, 2_209)
    equal("boosts", r.boost_count, 65_582)
    equal("recommends", r.recommend_count, 4_727)
    equal("duration from durationMs", r.duration_s, 34.53)
    equal("size", (r.width, r.height), (540, 960))
    equal("uploaded_at from uploadDateMs", r.uploaded_at, "2025-07-27T14:11:02Z")
    equal("sound card", (r.sound_title, r.sound_artist),
          ("Original Sound", "mrbeast"))
    equal("the creator's own description", r.description,
          "Slippery vs Sticky Stairs")
    check("title is Snapchat's generated one, not the literal 'Spotlight Snap'",
          r.title and r.title != "Spotlight Snap", "got %r" % r.title)
    check("video_url is the MP4 on Snapchat's CDN",
          (r.video_url or "").startswith("https://cf-st.sc-cdn.net/"))


def check_snapchats_filler_text_is_not_data():
    """`name: "Spotlight Snap"` and `description: "Another Spotlight Snap
    brought to you by Snapchat"` are the site's defaults, not the creator's
    words. A column of identical boilerplate reads as data."""
    for name in ("nasa", "mrbeast", "kyliejenner", "khaby00"):
        for r in rows(name, "spotlight"):
            check("%s %s: no boilerplate title" % (name, r.sku[-8:]),
                  r.title not in product_parser._BOILERPLATE)
            check("%s %s: no boilerplate description" % (name, r.sku[-8:]),
                  r.description not in product_parser._BOILERPLATE)
    metas = props("nasa")["spotlightStoryMetadata"]
    check("the fixture really does carry the boilerplate",
          any(m["videoMetadata"]["description"] in product_parser._BOILERPLATE
              for m in metas))


def check_every_snap_has_a_unique_key():
    """A HIGHLIGHT snap's own `snapId` is `{"value": ""}` — 645 of 645 on
    @kyliejenner — and the same content can be saved into several
    highlights. So the key falls back to highlight/content id, then to
    highlight#index, and must be unique per row."""
    for name in ("kyliejenner", "nasa", "mrbeast"):
        got = rows(name, "story")
        keys = [r.sku for r in got]
        check("%s: every snap row has a key (%d rows)" % (name, len(keys)),
              all(keys))
        equal("%s: and no two share one" % name, len(set(keys)), len(keys))
    live = [r for r in rows("kyliejenner", "story") if r.collection == "story"]
    equal("the live story has 16 snaps", len(live), 16)
    check("a live-story snap is keyed on its own snapId",
          all("/" not in r.sku and "#" not in r.sku for r in live))
    equal("and a live-story snap has no highlight title", {r.title for r in live},
          {None})
    no_media = [r for r in rows("nasa", "story") if "#" in r.sku]
    equal("@nasa: 9 snaps with no media fall back to position", len(no_media), 9)
    equal("an empty snapId is not a key",
          product_parser._snap_key({"snapId": {"value": ""}}, None), None)


def check_media_type_and_timestamps_are_read_not_guessed():
    got = rows("kyliejenner", "story")
    kinds = {r.media_type for r in got}
    equal("media types are the two Snapchat publishes", kinds, {"image", "video"})
    first = got[0]
    equal("a live-story snap's posted_at", first.posted_at, "2026-09-19T13:38:56Z")
    equal("zero is 'not set', not 1970",
          product_parser._epoch_to_iso("0", 1000), None)
    equal("an Int64 arrives as a STRING and still reads",
          snapchat_payload.to_int({"value": "1789825136"}), 1789825136)
    equal("an abbreviated magnitude is not a count",
          snapchat_payload.to_int("1.5m"), None)


def check_json_ld_is_secondary_and_may_be_absent():
    """@kyliejenner's page carries ZERO JSON-LD blocks. Everything but the
    two dates must still read, and the dates must be None rather than
    borrowed from anywhere else."""
    equal("the fixture really has no JSON-LD",
          PROFILES["kyliejenner"]["payload"]["profile_ld"], None)
    r = row("kyliejenner")
    equal("subscribers still read from the payload", r.subscriber_count, 29_262_900)
    equal("created_at is None without a ProfilePage block", r.created_at, None)
    equal("entity_type is None likewise", r.entity_type, None)


def check_a_page_says_when_it_is_not_the_whole_account():
    """The cursors are recorded, not followed — and a non-empty one is how a
    reader knows the page's slice is not the account's history
    (CLAUDE.md §21: complete is not exhaustive)."""
    equal("@kyliejenner has more Spotlight than its page lists",
          diag("kyliejenner")["more_spotlight"], True)
    equal("@nasa has more highlights than its page lists",
          diag("nasa")["more_highlights"], True)
    equal("@khaby00 has neither", (diag("khaby00")["more_spotlight"],
                                   diag("khaby00")["more_highlights"]),
          (False, False))
    for module in ENGINES:
        src = open(os.path.join(HERE, module + ".py"), encoding="utf-8").read()
        check("%s records it in the sidecar" % module,
              "handles_with_more_than_page" in src)


def check_no_marker_matches_a_page_snapchat_serves():
    """CLAUDE.md §18, counted rather than asserted.

    Counted on 26 served captures when the set was chosen, including the
    word `captcha` itself: zero. Re-counted here on every fixture.
    """
    for name, html in _SERVED_PAGES() + [("not_found", page("not_found"))]:
        hits = snapchat_payload.challenge_markers_present(html)
        check("no challenge marker on %s" % name, not hits, "fired: %s" % hits)
    for word in ("captcha", "recaptcha", "hcaptcha", "turnstile",
                 "cf-turnstile"):
        check("%r is deliberately not a marker" % word,
              word not in snapchat_payload.BOT_CHALLENGE_MARKERS,
              "a bare vendor word is injected by the Scraping Browser's "
              "extension into every page it loads (CLAUDE.md §24)")
    for marker in snapchat_payload.BOT_CHALLENGE_MARKERS:
        check("marker %r is not a bare word" % marker,
              any(c in marker for c in "-_/."),
              "a marker a bio could contain will eventually refuse a good page")
    fake = "<html><script src='https://www.google.com/recaptcha/api.js'></script>"
    equal("a real loader IS a challenge", product_parser.detect_page_state(fake, 200),
          product_parser.STATE_CHALLENGE)
    check("which the policy treats as blocked and worth a solve",
          page_flow.counts_as_blocked(product_parser.STATE_CHALLENGE)
          and page_flow.should_solve(product_parser.STATE_CHALLENGE))


def check_somebody_elses_page_is_not_a_parse_error():
    """Positive-asset detection (CLAUDE.md §8, §18): a page with no payload
    that is built from Snapchat's own static host is OUR failure to read
    it; one that is not — Chromium's network-error page carrying the site's
    hostname in its title, a proxy's own 502 — is somebody else's."""
    chromium = ("<html><head><title>www.snapchat.com</title></head><body>"
                "<div>ERR_PROXY_CONNECTION_FAILED</div></body></html>")
    equal("Chromium's own error page is unknown, not ours",
          product_parser.detect_page_state(chromium, None),
          product_parser.STATE_UNKNOWN)
    shell = ('<html><script src="https://static.snapchat.com/a.js"></script>'
             '<link href="https://static.snapchat.com/b.css"></html>')
    equal("a Snapchat shell with no payload is a parse error",
          product_parser.detect_page_state(shell, 200),
          product_parser.STATE_PARSE_ERROR)
    equal("a gateway's 502 with no payload is an error",
          product_parser.detect_page_state("<html>Bad gateway</html>", 502),
          product_parser.STATE_ERROR)
    other = page("nasa").replace('"/at/[username]"', '"/spotlight/[id]"')
    equal("a Snapchat page of another kind is unknown, not content",
          product_parser.detect_page_state(other, 200),
          product_parser.STATE_UNKNOWN)


def check_the_state_policy_is_read_and_not_hardcoded():
    """Every state has a policy, and the engines consult it (CLAUDE.md §17)."""
    states = [v for k, v in vars(product_parser).items()
              if k.startswith("STATE_") and isinstance(v, str)]
    for state in states:
        check("%s has a policy entry" % state, state in page_flow.STATE_POLICY,
              "an unlisted state silently falls back to `unknown`")
    for state, policy in page_flow.STATE_POLICY.items():
        equal("%s policy has all four keys" % state,
              sorted(policy), ["blocked", "parse", "retry", "solve"])
    for module in ENGINES:
        src = open(os.path.join(HERE, module + ".py"), encoding="utf-8").read()
        check("%s calls should_parse rather than testing the string"
              % module, "page_flow.should_parse(" in src)
        check("%s does not compare a state to a literal" % module,
              'state == "content"' not in src and "state == 'content'" not in src)


def check_a_profile_has_exactly_one_page():
    """`--pages` above 1 is refused, not silently ignored."""
    url = "https://www.snapchat.com/@nasa"
    equal("page 1 is the URL itself", product_parser.page_url(url, 1), url)
    try:
        product_parser.page_url(url, 2)
        check("page 2 is refused", False, "it returned a URL instead")
    except product_parser.NotAProfileUrl as exc:
        check("page 2 is refused with the reason",
              "exactly one page" in str(exc), "got %r" % str(exc))
    for module in ENGINES:
        src = open(os.path.join(HERE, module + ".py"), encoding="utf-8").read()
        check("%s refuses --pages above 1" % module,
              "PAGES_PER_PROFILE" in src and "is refused" in src)


def check_urls_are_refused_with_the_reason():
    """CLAUDE.md §5: a false refusal sends the reader hunting for a typo."""
    cases = [
        ("https://www.snapchat.com/spotlight/W7_abc", "creator"),
        ("https://www.snapchat.com/discover", "discover"),
        ("https://www.snapchat.com/lens/abc", "lens"),
        ("https://www.snapchat.com/explore/cats", "topic"),
        ("https://www.snapchat.com/", "home page"),
        ("https://map.snapchat.com/@1,2,3z", "snap map"),
        ("https://ads.snapchat.com/", "ads manager"),
        ("https://example.com/@nasa", "not on a snapchat host"),
    ]
    for url, want in cases:
        try:
            product_parser.handle_from_url(url)
            check("%s is refused" % url, False, "it parsed as a username")
        except product_parser.NotAProfileUrl as exc:
            check("%s is refused with a reason naming %r" % (url, want),
                  want.lower() in str(exc).lower(), "got %r" % str(exc))
    try:
        product_parser.handle_from_url("https://map.snapchat.com/x")
    except product_parser.NotAProfileUrl as exc:
        check("a Snapchat host is not called a non-Snapchat host",
              "not on a snapchat host" not in str(exc).lower())


def check_usernames_are_accepted_in_every_form_a_user_types():
    for raw in ("nasa", "@nasa", "NASA", "https://www.snapchat.com/@nasa",
                "https://www.snapchat.com/add/nasa", "snapchat.com/add/nasa",
                "https://story.snapchat.com/@nasa", "www.snapchat.com/@nasa/",
                "https://www.snapchat.com/@nasa?locale=en-US",
                "https://www.snapchat.com/@nasa/spotlight/W7_abc"):
        equal("%r -> nasa" % raw, product_parser.normalise_handle(raw), "nasa")
    equal("dots, dashes and underscores survive",
          product_parser.normalise_handle("@a.b-c_d"), "a.b-c_d")
    equal("a one-character username is real (@a answers HTTP 200)",
          product_parser.normalise_handle("a"), "a")
    for bad in ("", "a" * 33, "nas a", "nas/a", "-nasa"):
        try:
            product_parser.normalise_handle(bad)
            check("%r is refused" % bad, False, "it was accepted")
        except product_parser.NotAProfileUrl:
            check("%r is refused" % bad, True)
    equal("the locale rides on the canonical URL",
          product_parser.profile_url("NASA", "en-US"),
          "https://www.snapchat.com/@nasa?locale=en-US")


def check_a_locale_moves_the_chrome_and_not_the_data():
    """Measured 2026-09-24: en-US, fi-FI and ja-JP returned byte-identical
    account and Spotlight data. Say which half a locale flag changes."""
    for mode in product_parser.MODES:
        en = [asdict(r) for r in rows("nasa", mode)]
        ja = [asdict(r) for r in rows("nasa_ja", mode)]
        equal("?locale=ja-JP leaves every %s row alone" % mode, ja, en)
    equal("while the page itself IS localised", props("nasa_ja")["locale"], "ja-JP")
    readme = open(os.path.join(HERE, "README.md"), encoding="utf-8").read()
    check("the README says a locale does not localise the data",
          "chrome" in readme.lower() and "locale" in readme.lower())


def check_every_mode_shares_the_family_prefix():
    for cls in (Profile, Spotlight, Snap):
        names = [f.name for f in fields(cls)]
        equal("%s: family prefix first and in order" % cls.__name__, names[:5],
              ["source", "scraped_at", "url", "sku", "title"])
        equal("%s: source names the site" % cls.__name__, cls().source,
              "snapchat.com")
        check("%s: ends with page, position" % cls.__name__,
              names[-2:] == ["page", "position"])
    check("Product is bound to the profile row class",
          output_writer.Product is Profile)
    equal("every mode has a row class", sorted(output_writer.ROW_CLASS_BY_MODE),
          sorted(product_parser.MODES))
    # A column null on every row of every fixture should not exist (§9).
    for mode, cls in output_writer.ROW_CLASS_BY_MODE.items():
        got = [r for n in PROFILES if n != "not_found" for r in rows(n, mode)]
        always_null = [f.name for f in fields(cls)
                       if all(getattr(r, f.name) in (None, []) for r in got)]
        check("%s: no column is null on every fixture row (%d rows)"
              % (mode, len(got)), not always_null,
              "always null: %s" % always_null)


def check_page_and_position_are_unique_across_a_multi_account_run():
    """CLAUDE.md §18: `page` was 1 on every row of a two-page run in a
    sibling, so 60 of 119 rows claimed a position another row had. Here a
    "page" is which ACCOUNT of the run a row came from. Driven through the
    real `_run_profile` with the fetch stubbed."""
    engine = _import_engine("playwright_scraper")
    if engine is None:
        return
    pages = {"nasa": page("nasa"), "mrbeast": page("mrbeast")}

    def fetch(box, pw, args, pool, url, label):
        name = label.lstrip("@")
        return 200, pages[name], product_parser.STATE_CONTENT, False

    def body(FakeSession):
        args = _fault_args(engine, url="nasa,mrbeast", mode="spotlight")
        box = {"session": FakeSession(), "prime_url": "u"}
        return engine._run_profile(box, None, args, None)

    got, meta = _with_stubs(engine, fetch, body)
    pairs = [(r.page, r.position) for r in got]
    equal("11 Spotlight rows across two accounts", len(got), 11)
    equal("page+position is unique", len(set(pairs)), len(pairs))
    equal("page names the account's place in --url",
          sorted({r.page for r in got}), [1, 2])
    equal("the run is complete", meta["stop_reason"], "completed")


def check_hcaptcha_is_detected_solved_and_injected_as_itself():
    """hCaptcha is detected as itself, tasked and injected as itself.

    It carries a `g-recaptcha-response` field and a `data-sitekey` for
    compatibility, which the reCAPTCHA probe must not take for its own.
    """
    import captcha_solver as C
    key = "00000000-1111-2222-3333-444444444444"
    widget = ('<form><div class="h-captcha" data-sitekey="%s" '
              'data-callback="onDone"></div>'
              '<textarea name="g-recaptcha-response"></textarea>'
              '<textarea name="h-captcha-response"></textarea></form>'
              '<script src="https://hcaptcha.com/1/api.js"></script>' % key)
    ch = C.detect_hcaptcha(widget, "https://example.org/form")
    check("the container's sitekey is read", ch is not None and ch.sitekey == key)
    equal("it is hCaptcha", ch and ch.kind, "hcaptcha")
    equal("and its callback travels with it", ch and ch.action, "onDone")
    frame = ('<iframe src="https://newassets.hcaptcha.com/captcha/v1/x/static/'
             'hcaptcha.html#frame=checkbox&id=0&sitekey=%s"></iframe>' % key)
    equal("a rendered widget is found from its iframe alone",
          (C.detect_hcaptcha(frame) or C.CaptchaChallenge("x", "")).sitekey, key)
    equal("a loader with no widget is NOT a challenge — Snapchat's CSP names "
          "hcaptcha.com on every page", C.detect_hcaptcha(
              '<script src="https://hcaptcha.com/1/api.js"></script>'), None)
    equal("the reCAPTCHA detector declines an hCaptcha page",
          C.detect_recaptcha_v3(widget, "https://example.org/form"), None)

    probe = {"found": True, "sitekey": key, "size": None, "render": None}
    equal("the runtime reCAPTCHA probe declines a UUID sitekey",
          C.detect_recaptcha_in_page(lambda js, *a: probe, "u"), None)

    task = C._v2_task_for(ch, 0.3)
    equal("v2 task", task, {"type": "HCaptchaTaskProxyless",
                            "websiteURL": "https://example.org/form",
                            "websiteKey": key})
    sent = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"status": 1, "request": "TOKEN"}

    real_post, real_get, real_sleep = (C.requests.post, C.requests.get,
                                       C.time.sleep)
    C.requests.post = lambda url, data=None, **kw: (sent.update(data or {}), _Resp())[1]
    C.requests.get = lambda url, params=None, **kw: _Resp()
    C.time.sleep = lambda s: None
    try:
        token = C.solve_recaptcha(ch, "k" * 8, api_version="v1")
    finally:
        C.requests.post, C.requests.get, C.time.sleep = real_post, real_get, real_sleep
    equal("v1 uses method=hcaptcha with `sitekey`",
          (sent.get("method"), sent.get("sitekey"), "googlekey" in sent),
          ("hcaptcha", key, False))
    equal("and returns the token", token, "TOKEN")

    script, arg = C.injection_for(ch, "T")
    check("the token goes to h-captcha-response",
          script is C.INJECT_HCAPTCHA_JS and "h-captcha-response" in script)
    equal("with the page's callback", arg, {"token": "T", "callback": "onDone"})
    check("reCAPTCHA keeps its own injection",
          C.injection_for(C.CaptchaChallenge("recaptcha_v2", "6Lx"), "T")
          == (C.INJECT_TOKEN_JS, "T"))

    for module in ENGINES:
        src = open(os.path.join(HERE, module + ".py"), encoding="utf-8").read()
        body = src[src.index("def _handle_captcha_in_browser"):]
        body = body[:body.index("\n\n\n")]
        check("%s looks for hCaptcha BEFORE reCAPTCHA" % module,
              body.find("detect_hcaptcha(") != -1
              and body.find("detect_hcaptcha(") < body.find("reconcile_detections("))
        check("%s injects through injection_for" % module,
              "injection_for(challenge, token)" in body)
    for name, html in _SERVED_PAGES():
        equal("no hCaptcha on served page %s" % name, C.detect_hcaptcha(html), None)
    # A solve that finishes after the client stopped waiting is paid for
    # and thrown away.
    check("an hCaptcha solve is waited for long enough",
          C.HCAPTCHA_MAX_WAIT >= 240)
    for fn in (C._solve_with_2captcha_v1, C._solve_with_2captcha_v2):
        check("%s raises its wait for hCaptcha" % fn.__name__,
              "HCAPTCHA_MAX_WAIT" in inspect.getsource(fn))


def check_the_modes_cannot_disagree_about_an_account():
    """All three modes read one page, so their counts must agree."""
    for name in ("nasa", "mrbeast", "kyliejenner", "khaby00"):
        p = row(name)
        equal("%s: spotlight_count == spotlight rows" % name,
              p.spotlight_count, len(rows(name, "spotlight")))
        story = rows(name, "story")
        equal("%s: story_snap_count == live-story rows" % name,
              p.story_snap_count,
              len([r for r in story if r.collection == "story"]))


# ---------------------------------------------------------------------------
# Checks shared with the family, lifted verbatim so the two copies stay
# comparable (CLAUDE.md §22: renaming inside a shared check is how two
# repos stop being able to answer the same question).
# ---------------------------------------------------------------------------


def check_policy_constants_have_a_consumer():
    """§17: a policy constant nothing reads is the same defect as dead code.

    `RETRY_ON_BLOCKED` carried a paragraph of justification in a sibling repo
    and no engine consulted it, so setting it False changed nothing.
    """
    import page_flow
    sources = []
    for name in ("playwright_scraper.py", "selenium_scraper.py",
                 "puppeteer_scraper.py", "scraper_api_client.py"):
        path = os.path.join(HERE, name)
        if os.path.exists(path):
            sources.append(open(path, encoding="utf-8").read())
    joined = "\n".join(sources)
    for constant in ("RETRY_ON_BLOCKED", "BLOCK_RETRIES_WITHOUT_POOL",
                     "SOLVES_PER_PAGE"):
        check("page_flow.%s is CONSULTED by an engine" % constant,
              constant in joined,
              "defined in page_flow and read by nothing")
    for fn in ("pages_to_plan", "ready_selector", "min_matches",
               "content_timeout_ms", "wait_for_count", "classify",
               "should_retry", "should_solve", "counts_as_blocked",
               "should_parse", "concurrency_limit",
               "pagination_is_addressable"):
        check("page_flow.%s has a caller outside its own module" % fn,
              fn in joined, "unused policy")


def check_csv_and_json_writers():
    """JSON and CSV carry the same columns in the same order, and an empty
    CSV still carries its header (CLAUDE.md §9)."""
    from output_writer import write_csv, write_json
    rows = [row("nasa"), row("mrbeast")]
    expected = [f.name for f in fields(Profile)]
    with tempfile.TemporaryDirectory() as tmp:
        csv_path = os.path.join(tmp, "out.csv")
        write_csv(rows, csv_path, row_cls=Profile)
        with open(csv_path, encoding="utf-8") as f:
            reader = list(csv.reader(f))
        equal("csv header is the dataclass, in order", reader[0], expected)
        equal("csv has one line per row", len(reader), len(rows) + 1)
        equal("and the id column is the account's business profile id",
              reader[1][expected.index("sku")], rows[0].sku)

        json_path = os.path.join(tmp, "out.json")
        write_json(rows, json_path)
        data = json.load(open(json_path, encoding="utf-8"))
        equal("json columns match the dataclass, in order",
              list(data[0].keys()), expected)
        equal("json and csv agree on the subscriber count",
              str(data[0]["subscriber_count"]),
              reader[1][expected.index("subscriber_count")])

        # An empty CSV still carries its header, so a consumer reads a
        # table with no rows rather than failing on a zero-byte file.
        empty_path = os.path.join(tmp, "empty.csv")
        write_csv([], empty_path, row_cls=Profile)
        with open(empty_path, encoding="utf-8") as f:
            empty = list(csv.reader(f))
        equal("an empty CSV still has its header", empty, [expected])


def check_exit_codes():
    import output_writer as O
    equal("0 ok / 1 crash / 2 usage / 3 blocked / 4 empty / 5 api / 6 partial",
          (O.EXIT_BLOCKED, O.EXIT_NO_PRODUCTS, O.EXIT_API_ERROR, O.EXIT_PARTIAL),
          (3, 4, 5, 6))
    check("page_cap_reached is a COMPLETE stop reason",
          "page_cap_reached" in O.COMPLETE_STOP_REASONS)
    # Carried for the family's shared vocabulary: no engine in this repo
    # emits it today, but a route served at ONE address holding its whole
    # result set is complete after one fetch, and must stay so.
    check("single_page_route is complete by construction AND by measurement",
          "single_page_route" in O.COMPLETE_STOP_REASONS)
    # Carried for the family's shared vocabulary and unreachable here: this
    # site cannot clamp an out-of-range page back, having only one.
    check("page_echo_mismatch is complete",
          "page_echo_mismatch" in O.COMPLETE_STOP_REASONS)
    check("...and an enumeration that yielded nothing is NOT complete",
          "enumeration_empty" not in O.COMPLETE_STOP_REASONS)
    check("no_new_products is complete",
          "no_new_products" in O.COMPLETE_STOP_REASONS)


def check_a_run_that_finds_nothing_writes_nothing():
    """Never replace last night's good output with []."""
    from output_writer import save
    with tempfile.TemporaryDirectory() as tmp:
        prefix = os.path.join(tmp, "out")
        with open(prefix + ".json", "w", encoding="utf-8") as f:
            f.write('[{"sku": "yesterday"}]')
        code = save([], prefix, "json", allow_empty=False)
        equal("an empty run exits 4", code, 4)
        equal("...and leaves the previous good file alone",
              open(prefix + ".json", encoding="utf-8").read(),
              '[{"sku": "yesterday"}]')
        code = save([], prefix, "json", allow_empty=True)
        equal("--allow-empty WRITES the empty file...", 
              json.load(open(prefix + ".json", encoding="utf-8")), [])
        # ...and still reports exit 4. Pinned deliberately (§10: pin a known
        # behaviour rather than half-guarding it): "zero rows" is true
        # whether or not the file was written, and a caller that wanted the
        # file still wants to know the result was empty.
        equal("...and still reports exit 4, because it IS empty", code, 4)


def check_sidecar_shape():
    from output_writer import run_meta
    meta = run_meta(status="complete", stop_reason="single_page_route",
                    pages_requested=1, pages_completed=1, pages_failed=[],
                    products=3, mode="profile", source="snapchat.com",
                    start_url="https://www.snapchat.com/@nasa",
                    final_url="https://www.snapchat.com/@nasa",
                    extra={"pages_available": 1, "route_is_paginated": False})
    for key in ("status", "stop_reason", "pages_requested", "pages_completed",
                "pages_failed", "mode", "source"):
        check("the sidecar records %r" % key, key in meta)
    equal("the sidecar carries run facts that are about no single row",
          meta["pages_available"], 1)
    equal("...and whether this route is addressable page by page",
          meta["route_is_paginated"], False)
    equal("pages_failed is a LIST of numbers, not a count",
          isinstance(meta["pages_failed"], list), True)


def check_engines_import_their_driver_at_module_level():
    """For the guarded imports above to MEAN anything.

    A sibling repo imported `launch`/`connect` inside the launch path, so the
    module imported cleanly with no pyppeteer installed: the group never
    skipped, and the CI job that exists to fail on unexpected skips could not
    have caught a broken import. It also let CI run against a stub version
    for a while without anything noticing. This drifts back silently, so it
    is asserted with an `ast` walk rather than trusted.
    """
    for module, driver in DRIVER_IMPORTS.items():
        path = os.path.join(HERE, module + ".py")
        if not os.path.exists(path):
            check("%s exists" % module, False)
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        top_level = set()
        for node in tree.body:          # module level ONLY
            if isinstance(node, ast.Import):
                top_level.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level.add(node.module.split(".")[0])
        check("%s imports %s at MODULE level" % (module, driver),
              driver in top_level,
              "top-level imports: %s" % sorted(top_level))


def check_shared_calls_bind_against_the_real_signature():
    """§17's check #1, and the one that earns its keep.

    A sibling repo shipped `classify(html, url=…)` in two of three engines
    against a callee taking `status` second, and BOTH crashed on their first
    fetch — invisible to import, --help, compileall, the undefined-name walk
    and 400+ green assertions, because none of those calls a function the way
    a live run does.

    This walks every engine's AST for calls into the shared modules and binds
    each one against the callee's real signature.
    """
    # EVERY shared module, not the three that are easy. A sibling repo
    # widened this after a key arrived and five calls into functions that
    # never existed came out of the credential-gated paths — the ones
    # nobody runs, for the obvious reason (CLAUDE.md §16). Two of the
    # modules below are reachable only with a key.
    import captcha_solver
    import diff_runs
    import env_config
    import fingerprint_client
    import output_writer
    import page_flow
    import product_parser
    import proxy_pool
    targets = {"page_flow": page_flow, "product_parser": product_parser,
               "output_writer": output_writer, "proxy_pool": proxy_pool,
               "fingerprint_client": fingerprint_client,
               "captcha_solver": captcha_solver, "env_config": env_config,
               "diff_runs": diff_runs}
    bound = 0
    for module in ENGINES + ("scraper_api_client",):
        path = os.path.join(HERE, module + ".py")
        if not os.path.exists(path):
            continue
        source = open(path, encoding="utf-8").read()
        tree = ast.parse(source)
        # Which shared names this file imported directly (`from x import y`).
        direct = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in targets:
                for alias in node.names:
                    direct[alias.asname or alias.name] = (
                        targets[node.module], alias.name)

        # A name bound ANYWHERE in this file shadows a same-named module
        # (CLAUDE.md §22). An engine that takes `proxy_pool` as a parameter
        # is calling a method on an object, not a module attribute, and
        # without this rule that reported twenty-one false positives on a
        # clean repo in a sibling. Parameters count whether or not they
        # carry a type annotation — an annotation is not what makes a name
        # a local.
        shadowed = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                spec = node.args
                for arg in (list(spec.args) + list(spec.posonlyargs)
                            + list(spec.kwonlyargs)
                            + [a for a in (spec.vararg, spec.kwarg) if a]):
                    shadowed.add(arg.arg)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                shadowed.add(node.id)
        local_targets = {name: mod for name, mod in targets.items()
                         if name not in shadowed}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            owner = attr = None
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                if func.value.id in local_targets:
                    owner, attr = local_targets[func.value.id], func.attr
            elif isinstance(func, ast.Name) and func.id in direct:
                owner, attr = direct[func.id]
            if owner is None:
                continue
            # A name that is NOT THERE is the loudest possible failure and
            # this check used to swallow it: `getattr(..., None)` returned
            # None, `not callable(None)` was true, and the call was skipped.
            # Three calls into a page_flow API that does not exist in this
            # repo -- comparable(), next_page_selector(),
            # next_page_candidates(), all of them Tokopedia's, all arriving
            # with copied code -- sat in two engines under a green run of
            # this very function. Absent is not "nothing to bind".
            if not hasattr(owner, attr):
                check("%s.%s exists (called from %s:%d)"
                      % (getattr(owner, "__name__", owner), attr,
                         module + ".py", node.lineno),
                      False,
                      "the engine calls a name the shared module does not "
                      "define; a live run reaches this as AttributeError")
                continue
            callee = getattr(owner, attr)
            if not callable(callee):
                continue
            if inspect.isclass(callee):
                # A CONSTRUCTOR is a call like any other, and skipping it
                # is how `SolveBudget(limit=…)` or `ProxyPool(rotate=…)`
                # with a wrong keyword reaches a live run untested. Bind
                # against `__init__` with `self` already supplied.
                try:
                    signature = inspect.signature(callee.__init__)
                    signature = signature.replace(
                        parameters=list(signature.parameters.values())[1:])
                except (TypeError, ValueError):
                    continue
            else:
                try:
                    signature = inspect.signature(callee)
                except (TypeError, ValueError):
                    continue
            positional = [inspect.Parameter.empty] * len(node.args)
            keywords = {}
            for kw in node.keywords:
                if kw.arg is None:
                    break
                keywords[kw.arg] = inspect.Parameter.empty
            else:
                try:
                    signature.bind(*positional, **keywords)
                    bound += 1
                except TypeError as exc:
                    check("%s:%d %s.%s(...) binds against its real signature"
                          % (module, node.lineno,
                             getattr(owner, "__name__", owner), attr),
                          False,
                          "%s; signature is %s" % (exc, signature))
    check("every shared-module call in every engine binds (%d checked)" % bound,
          bound > 40, "only %d calls were checked — is the walk finding them?"
          % bound)


def check_engine_flag_sets():
    """§17's check #2: against the contract AND against each other, both ways.

    A missing flag fails; so does closing a difference the README documents.
    """
    sets = {}
    for module in ENGINES:
        if not os.path.exists(os.path.join(HERE, module + ".py")):
            continue
        sets[module] = _argparse_flags(module)
    for module, flags in sets.items():
        missing = (CONTRACT_FLAGS | SITE_FLAGS) - flags
        check("%s defines every contract flag" % module, not missing,
              "missing %s" % sorted(missing))
    # NO documented differences on this site, and that is a stronger
    # statement than a list: the three engines were generated from one
    # file with only their driver layer swapped, so their flag sets are
    # identical by construction. Growing a difference — in either
    # direction — has to be a decision, and this empty map is what makes
    # it fail the build until someone writes down why.
    DOCUMENTED_DIFFERENCES = {}
    names = sorted(sets)
    for i in range(len(names) - 1):
        a, b = names[i], names[i + 1]
        only_a = sets[a] - sets[b] - DOCUMENTED_DIFFERENCES.get(a, set())
        only_b = sets[b] - sets[a] - DOCUMENTED_DIFFERENCES.get(b, set())
        check("%s and %s define the same flags" % (a, b),
              not only_a and not only_b,
              "only in %s: %s; only in %s: %s"
              % (a, sorted(only_a), b, sorted(only_b)))


def check_banned_and_removed_flags():
    """Scoped to the ENGINES.

    `--country` is banned on the engines and there is no exception here:
    a country flag on a scraper could only contradict what the URL (or,
    on the Ad Library, `--region`) already says, and where a request
    EXITS is the proxy's business, not the scraper's. On `fingerprint_client.py` the
    name `--country` is legitimate — there it picks a fingerprint locale,
    not a target — which is why this check is scoped to the engines rather
    than to the tree (CLAUDE.md §10).

    What the rule is really about is an option that can disagree with
    reality, and the three that CAN on this site are all refused with
    their reason rather than silently absorbed:

      * a URL that is not a profile — a Spotlight video, Discover or a
        Lens page has no account object on it, and "not a Snapchat URL"
        would be a lie about a Snapchat URL (CLAUDE.md §5);
      * `--pages` above 1, because a profile has exactly one page and a
        silent cap would report a complete run of duplicates;
      * `--proxy` with `--cdp-endpoint`, because the Scraping Browser
        already proxies and stacking two exits is not better cover;
      * more workers than there are accounts to fetch, which would leave
        idle threads and a log that overstates what the run did.
    """
    for module in ENGINES:
        path = os.path.join(HERE, module + ".py")
        if not os.path.exists(path):
            continue
        source = open(path, encoding="utf-8").read()
        for flag in BANNED_FLAGS:
            check("%s does not define %s" % (module, flag),
                  '"%s"' % flag not in source)
        check("%s refuses a URL that is not a profile" % module,
              "NotAProfileUrl" in source and "p.error" in source,
              "a hashtag feed or a video URL would otherwise be fetched "
              "and parse to nothing")
        check("%s caps --pages with the reason" % module,
              "PAGES_PER_PROFILE" in source and "is refused" in source,
              "a silent cap would report a complete run of duplicates")
        check("%s refuses --proxy with --cdp-endpoint" % module,
              "already proxies" in source,
              "the Scraping Browser already proxies; stacking two exits is "
              "not better cover")
        check("%s lowers --concurrency to the number of accounts, and says "
              "so" % module,
              "lowered to" in source,
              "starting five workers for two accounts leaves three idle "
              "threads and a log that overstates what the run did")


def check_undefined_names_in_every_module():
    """§10: compileall proves a file PARSES, not that its names RESOLVE.

    A live run of a sibling repo's pyppeteer engine died with NameError on a
    line reached only while fetching, after an import had been removed — the
    module imported cleanly, --help worked, compileall passed and CI was
    green. Kept COARSE (pooled bindings, no scope tracking) so it
    under-reports rather than inventing problems.
    """
    import builtins
    modules = [f for f in sorted(os.listdir(HERE))
               if f.endswith(".py") and f != "smoke_test.py"]
    for filename in modules:
        tree = ast.parse(open(os.path.join(HERE, filename), encoding="utf-8").read())
        # Module-level dunders exist without being assigned anywhere.
        defined = set(dir(builtins)) | {"__file__", "__name__", "__doc__",
                                        "__package__", "__spec__"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    defined.add((alias.asname or alias.name).split(".")[0])
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                defined.add(node.id)
            elif isinstance(node, ast.arg):
                defined.add(node.arg)
            elif isinstance(node, ast.ExceptHandler) and node.name:
                defined.add(node.name)
            elif isinstance(node, ast.alias) and node.asname:
                defined.add(node.asname)
        used = {n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        unresolved = sorted(used - defined)
        check("%s: every name resolves" % filename, not unresolved,
              "%s" % unresolved)


def check_dockerfile_copies_everything_the_entrypoint_imports():
    """§10: all three repos in this family shipped an image that died with
    ModuleNotFoundError on every invocation, --help included, because
    proxy_pool.py was missing from the COPY list. CI never built the image;
    this check needs no Docker."""
    path = os.path.join(HERE, "Dockerfile")
    if not os.path.exists(path):
        check("Dockerfile exists", False)
        return
    dockerfile = open(path, encoding="utf-8").read()
    # Only the COPY instructions, continuations included — a comment above
    # them naming a file is not a file the image carries.
    copy_lines, joining = [], False
    for line in dockerfile.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if joining or stripped.upper().startswith("COPY "):
            copy_lines.append(stripped)
            joining = stripped.endswith("\\")
    copied = set(re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\.py", " ".join(copy_lines)))
    entry = re.search(r'(?:CMD|ENTRYPOINT)\s*\[?\s*"?(?:python3?"?,\s*"?)?'
                      r'([A-Za-z_][A-Za-z0-9_]*)\.py', dockerfile)
    entrypoint = entry.group(1) if entry else "playwright_scraper"
    needed = _import_graph(entrypoint)
    missing = sorted(needed - copied)
    check("the Dockerfile COPYs every module %s.py imports" % entrypoint,
          not missing, "missing %s" % missing)
    for unwanted in ("smoke_test", "test_smoke"):
        check("the image does not carry %s.py" % unwanted,
              unwanted not in copied)


def check_env_example_documents_exactly_what_the_loader_reads():
    import env_config
    path = os.path.join(HERE, ".env.example")
    if not os.path.exists(path):
        check(".env.example exists", False)
        return
    documented = set(re.findall(r"^\s*#?\s*([A-Z][A-Z0-9_]+)\s*=", 
                                open(path, encoding="utf-8").read(), re.M))
    read = set(env_config.ENV_KEYS)
    check("every variable the loader reads is documented",
          not (read - documented), "undocumented: %s" % sorted(read - documented))
    check("every documented variable is actually read",
          not (documented - read), "unread: %s" % sorted(documented - read))


def check_a_copied_env_example_reads_as_UNSET():
    """§17: `cp .env.example .env` followed by a run must not connect.

    The placeholder check was a literal set in a sibling repo, and the two
    credentialled URLs are documented the way the vendor documents them —
    `ws://{login}-zone-…:{password}@cb.2captcha.com:9222` — so neither
    literal matched, the run connected with the string `{login}-zone-…` as
    its username, and got a 401 a long way from its cause.
    """
    import env_config
    example = os.path.join(HERE, ".env.example")
    if not os.path.exists(example):
        check(".env.example exists", False)
        return
    text = open(example, encoding="utf-8").read()
    values = dict(re.findall(r"^([A-Z][A-Z0-9_]+)=(.*)$", text, re.M))
    check("the example actually sets every variable",
          set(values) == set(env_config.ENV_KEYS),
          "example has %s, loader reads %s"
          % (sorted(values), sorted(env_config.ENV_KEYS)))
    # Every CREDENTIAL must read as unset. The default TARGET must not: it is
    # a real, usable URL, and blanking it would remove the one setting this
    # file exists to make convenient (§17's check #3 says exactly this — the
    # credentials unset, the non-credential default still usable).
    CREDENTIALS = {"TWOCAPTCHA_KEY", "SNAPCHAT_CDP_ENDPOINT", "SNAPCHAT_PROXY"}
    before = dict(os.environ)
    try:
        for name, raw in values.items():
            os.environ[name] = raw
            got = env_config.env_value(name)
            if name in CREDENTIALS:
                check("a copied .env.example leaves %s unset" % name,
                      got is None, "got %r" % got)
            else:
                check("...while %s stays a usable default" % name,
                      got == raw.strip(), "got %r" % got)
    finally:
        os.environ.clear()
        os.environ.update(before)
    # And the counter-check: a real credential must still come through, or
    # the placeholder rule would have made the loader useless. Deliberately
    # NOT 32 hex characters — that is the shape of a real 2captcha key, and
    # this repo's own credential scan (rightly) fails on one.
    try:
        os.environ["TWOCAPTCHA_KEY"] = "not-a-real-key-but-a-real-value"
        equal("a real value is still read",
              env_config.env_value("TWOCAPTCHA_KEY"),
              "not-a-real-key-but-a-real-value")
    finally:
        os.environ.clear()
        os.environ.update(before)


def check_credential_scan_is_one_implementation_invoked_from_both():
    """§17: two sources of truth, one dead and one holed.

    `.github/ci_checks.py` sat in three repos invoked by NOTHING, while
    tests.yml carried an inline grep doing a narrower version of the same job
    — one that matched only ws:// and wss://, so an http://user:pass@
    credential would have sailed past CI.
    """
    script = os.path.join(HERE, ".github", "ci_checks.py")
    check("the credential scan exists as a script", os.path.exists(script))
    if not os.path.exists(script):
        return
    workflow_dir = os.path.join(HERE, ".github", "workflows")
    workflow = os.path.join(workflow_dir, "tests.yml")
    # Triggered on the whole .github directory being absent, never on this one
    # file being missing: two suites in this family run INSIDE the Docker
    # image, which deliberately COPYs no .github/, and a check that quietly
    # starts passing once its input disappears is the same failure this
    # function is about (CLAUDE.md §22).
    if not os.path.isdir(workflow_dir):
        skip("ci-wiring", "no .github/ in this tree (the Docker image)")
    elif os.path.exists(workflow):
        text = open(workflow, encoding="utf-8").read()
        check("CI INVOKES the script rather than reimplementing it",
              "ci_checks.py" in text)
        # ...and does not ALSO reimplement it. The original version of this
        # check asserted only the first half, and the workflow carried inline
        # `python - <<EOF` copies of the --help and sample checks alongside
        # the call — justified in a comment as keeping the two from drifting
        # apart. They drifted: the inline sample copy still imported the row
        # dataclass under a name this repo renamed, and it failed on the
        # repo's FIRST push while the script it duplicated passed.
        #
        # Scoped to the OFFLINE job, because the docker job legitimately
        # names `sample_output.json` for a different purpose — asserting the
        # image does NOT contain it. A guard that fired there would be wrong,
        # and a guard people have to argue with is one they learn to
        # suppress.
        offline = text.split("  engine-smoke:", 1)[0]
        for marker, what in (("from output_writer import", "the row schema"),
                             ("sample_output.json", "the sample output"),
                             ("subprocess.run([sys.executable", "the --help contract")):
            check("the offline job does not reimplement the check for %s" % what,
                  marker not in offline,
                  "tests.yml's offline job mentions %r — one implementation, "
                  "in ci_checks.py, invoked from both" % marker)
        # And the guard must have had something to read, or it passed for the
        # wrong reason (CLAUDE.md §22).
        check("...and the offline job was actually found to scan",
              "ci_checks.py" in offline, "no offline job in tests.yml")
    result = subprocess.run([sys.executable, script, "--all"], cwd=HERE,
                            capture_output=True, text=True)
    check("the credential scan passes on this repo's own tree",
          result.returncode == 0,
          (result.stdout + result.stderr)[-600:])


def check_the_credential_scan_survives_a_venv_in_the_tree():
    """A guard people have to argue with is one they learn to suppress.

    Found by cloning this repo the way a stranger does and following the
    README: `python3 -m venv` puts a virtualenv in the working tree, and the
    credential scan walked into pip's vendored code and flagged a 32-hex
    string in `_elffile.py` as key-shaped. Correct about the string, wrong
    about the file, and the first thing a new user would have seen.

    The fix is structural rather than a longer list of names — a directory
    holding `pyvenv.cfg` is a virtualenv whatever it is called — and this
    pins BOTH halves, because narrowing a credential scan is exactly how one
    stops catching things. CLAUDE.md §22 records a sibling repo whose scan
    caught an UNTRACKED `.env.bak` holding a live key, so scanning must not
    be reduced to tracked files.
    """
    import importlib.util
    script = os.path.join(HERE, ".github", "ci_checks.py")
    if not os.path.exists(script):
        skip("credential-scan", "no .github/ in this tree (the Docker image)")
        return
    spec = importlib.util.spec_from_file_location("_ci_checks", script)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    check("the scan knows a virtualenv structurally, not by name",
          hasattr(mod, "_is_virtualenv"))
    if not hasattr(mod, "_is_virtualenv"):
        return

    with tempfile.TemporaryDirectory() as tmp:
        odd = os.path.join(tmp, "whatever-i-called-it")
        os.makedirs(os.path.join(odd, "lib"))
        open(os.path.join(odd, "pyvenv.cfg"), "w").write("home = /usr\n")
        check("...so a venv under any name is recognised",
              mod._is_virtualenv(pathlib.Path(odd)))
        plain = os.path.join(tmp, "src")
        os.makedirs(plain)
        check("...and an ordinary directory is not",
              not mod._is_virtualenv(pathlib.Path(plain)))

    # The other half: it must still walk files git does not track, because a
    # key pasted into a scratch file is the case this scan exists for.
    scanned = [str(p) for p in mod.scanned_files()]
    check("the scan still reads this repo's own files", len(scanned) > 20,
          "%d file(s)" % len(scanned))
    check("...and is not limited to git's index",
          "git ls-files" not in open(script, encoding="utf-8").read())

    # And the skip set applies INSIDE the repo only. A clone that lives
    # under a directory called `tmp` (or `build`, `run`, ...) once scanned
    # zero files and reported the empty set as clean.
    with tempfile.TemporaryDirectory() as tmp:
        clone = pathlib.Path(tmp) / "tmp" / "build" / "a-clone"
        clone.mkdir(parents=True)
        (clone / "planted.py").write_text("x = 1\n")
        saved = mod.REPO
        mod.REPO = clone
        try:
            found = [p.name for p in mod.scanned_files()]
        finally:
            mod.REPO = saved
        check("...wherever the clone happens to live on disk",
              found == ["planted.py"], repr(found))


def check_banned_wording():
    """§12: enforced by this test rather than by review."""
    for root, dirs, files in os.walk(HERE):
        dirs[:] = [d for d in dirs if d not in
                   (".git", "__pycache__", ".pytest_cache", "node_modules")]
        for filename in files:
            if not filename.endswith((".py", ".md", ".yml", ".yaml", ".txt",
                                      ".toml", ".html", ".example")):
                continue
            path = os.path.join(root, filename)
            text = open(path, encoding="utf-8", errors="replace").read().lower()
            for phrase in BANNED_WORDING:
                if phrase.lower() in text and filename != "smoke_test.py":
                    check("%s contains no %r" % (
                        os.path.relpath(path, HERE), phrase), False)
    check("banned-wording scan ran", True)


def check_worker_pools_start_on_different_exits():
    engine = _import_engine("playwright_scraper")
    if engine is None:
        return
    from proxy_pool import ProxyPool
    pool = ProxyPool(["http://a:1", "http://b:2", "http://c:3"], rotate="per-run")
    firsts = [engine._worker_pool(pool, i).current for i in range(3)]
    equal("three workers start on three different exits",
          len(set(firsts)), 3)
    equal("a missing pool stays missing", engine._worker_pool(None, 0), None)


def check_fingerprint_kwargs_are_ones_the_driver_accepts():
    """§10: an unknown key in new_context(**kwargs) is a TypeError at launch,
    on the PAID path, at runtime."""
    engine = _import_engine("playwright_scraper")
    if engine is None:
        return
    try:
        from fingerprint_client import playwright_context_kwargs
    except ImportError as e:
        skip("fingerprint", str(e))
        return
    sample = {"id": "x", "country": "US",
              "userAgent": "Mozilla/5.0 Chrome/140.0.0.0",
              "screen": {"width": 1920, "height": 1080},
              "timezone": "America/New_York", "language": "en-US",
              "devicePixelRatio": 2}
    kwargs = playwright_context_kwargs(sample)
    from playwright.sync_api import sync_playwright  # noqa: F401
    import playwright.sync_api as pw_api
    signature = inspect.signature(pw_api.Browser.new_context)
    unknown = [k for k in kwargs if k not in signature.parameters]
    check("every fingerprint kwarg is one new_context accepts", not unknown,
          "unknown: %s" % unknown)


def check_every_engine_exposes_the_same_public_surface():
    """The three engines are one file with three driver layers.

    Read from the SOURCE rather than from imported modules, and that is
    the fix rather than a style choice. The first version compared the
    engines that happened to import, so in a single-engine virtualenv —
    the only configuration this repo's README supports (§6: install
    exactly one) — it compared ONE engine against nothing and reported
    itself passed. Measured: 0 pairs compared, suite green.

    A third-party audit found the divergence it was supposed to find, by
    installing all three engines in one environment: a configuration the
    README tells people not to create. A check that only runs in an
    unsupported setup is a check nobody runs.

    An `ast` walk needs no driver installed, so the comparison now happens
    in every environment including one with no engine at all.
    """
    surfaces = {}
    for module in ENGINES:
        path = os.path.join(HERE, module + ".py")
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        names = set()
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        names.add(target.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target,
                                                                ast.Name):
                names.add(node.target.id)
        surfaces[module] = names

    equal("all three engines are present to compare", len(surfaces), 3)

    # What each engine legitimately holds that its twins do not: the names
    # its own driver layer needs. Everything else must match.
    DRIVER_LOCAL = {
        "playwright_scraper": {"sync_playwright", "PWError", "PWTimeout"},
        "puppeteer_scraper": {"launch", "connect", "asyncio", "concurrent",
                              "PyppeteerError", "NetworkError", "PPTimeout",
                              "_Loop", "_FETCH_JS", "RemoteBrowserError",
                              "CDP_CONNECT_TIMEOUT"},
        "selenium_scraper": {"webdriver", "WebDriverException", "SETimeout",
                             "ChromeOptions", "By", "_FETCH_JS",
                             "RemoteBrowserError", "_apply_fingerprint"},
    }
    names = sorted(surfaces)
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            a, b = names[i], names[j]
            only_a = surfaces[a] - surfaces[b] - DRIVER_LOCAL.get(a, set())
            only_b = surfaces[b] - surfaces[a] - DRIVER_LOCAL.get(b, set())
            check("%s and %s expose the same names" % (a, b),
                  not only_a and not only_b,
                  "only in %s: %s; only in %s: %s"
                  % (a, sorted(only_a), b, sorted(only_b)))

    # And the shared half must really be shared: the names the runner
    # needs, in every engine, whatever its driver.
    for module, names_in in surfaces.items():
        for name in ("scrape", "parse_args", "PageOutcome", "MODES",
                     "_handles", "_fetch_one_profile",
                     "_fetch_profiles_concurrently", "_worker_pool",
                     "_run_profile", "_open_session", "_prime_session",
                     "_fetch_with_policy", "handle_captcha_if_present",
                     "_proxy_failure", "_mask_credentials", "_driver_context",
                     "_rotate_if_per_page", "_open_http", "_call", "_dump",
                     "PAGES_PER_PROFILE"):
            check("%s defines %s" % (module, name), name in names_in)

    # The runtime shape of PageOutcome, for whichever engines DO import.
    for module in ENGINES:
        engine = _import_engine(module)
        if engine is None:
            continue
        outcome = engine.PageOutcome(number=1)
        for field_name in ("number", "url", "rows", "state", "status",
                           "blocked", "error", "diagnostics", "attempted"):
            check("%s.PageOutcome carries %r" % (module, field_name),
                  hasattr(outcome, field_name))
        equal("%s names the same modes" % module, tuple(engine.MODES),
              tuple(product_parser.MODES))


def check_every_solve_is_counted_against_the_budget():
    """`SOLVES_PER_PAGE` is a MONEY limit, so every call that can buy must
    be counted — CLAUDE.md §23.

    `handle_captcha_if_present` is called TWICE per attempt in every engine
    in this family: once before the response is classified (so a challenge
    is cleared before anything is judged) and once after, for the state
    that says the page really is gated. In every sibling repo only the
    SECOND was counted, so the first bought a solve on every block attempt,
    for free and silently. Measured in a sibling on 2026-09-17 from an
    address where a real Cloudflare challenge rendered on every fetch: one
    page bought THREE Turnstile solves with the cap set to 1.

    This repo fixes it by construction rather than by discipline. The
    budget is an OBJECT created once per page and handed to both call
    sites, and it counts the spend inside itself — so a caller cannot
    forget to, which is the failure mode the sibling had. What this check
    pins is that the object is still the only way to spend.

    Inherited rather than measured here: this site has refused nothing, so
    no run of this repo has ever bought a solve.
    """
    for module in ENGINES:
        path = os.path.join(HERE, module + ".py")
        if not os.path.exists(path):
            continue
        source = open(path, encoding="utf-8").read()
        calls = source.count("handle_captcha_if_present(session, args, budget)")
        check("%s routes the solver through one entry point" % module,
              source.count("def _handle_captcha_in_browser") == 1,
              "the HTTP transport has no page to inject a token into, so "
              "the split is what keeps that answer in one place")
        check("%s calls the solver from two places, as designed" % module,
              calls == 2, "found %d call site(s)" % calls)
        check("%s creates exactly one budget per attempt loop" % module,
              source.count("budget = SolveBudget()") == 1,
              "%d budget object(s)" % source.count("budget = SolveBudget()"))
        check("%s never counts a spend by hand" % module,
              "solves_bought" not in source,
              "a hand-rolled counter is the thing SolveBudget replaces")

    # The object itself: the spend is counted inside, and the second
    # attempt is refused.
    budget = page_flow.SolveBudget()
    equal("the first spend is allowed", budget.spend(), True)
    equal("the second is not", budget.spend(), False)
    equal("and the count is kept by the object", budget.spent, 1)
    check("may_spend agrees with spend", not budget.may_spend())
    equal("a zero budget buys nothing", page_flow.SolveBudget(0).spend(), False)

    # And a solver call must sit behind it, not beside it.
    for module in ENGINES:
        path = os.path.join(HERE, module + ".py")
        if not os.path.exists(path):
            continue
        source = open(path, encoding="utf-8").read()
        # The browser handler, not the router. `handle_captcha_if_present`
        # now decides which transport it is on and returns early for the
        # HTTP one, so the budget lives one function further in — and this
        # check must follow it rather than reading a function that no
        # longer spends anything.
        handler = source[source.index("def _handle_captcha_in_browser"):]
        handler = handler[:handler.index("\n\n\n")]
        check("%s checks the budget before paying" % module,
              handler.index("budget.spend()") < handler.index("solve_recaptcha("),
              "the solver is reached before the budget is charged")

    equal("at most one purchase per page", page_flow.SOLVES_PER_PAGE, 1)


def check_a_dead_proxy_is_reported_as_a_proxy_failure():
    """CLAUDE.md §8: a proxy failure is not a timeout, and the two want
    opposite responses — another try at the same exit versus a different one.

    The engines all compute the reason (`_proxy_failure`) and, WITH a pool,
    log it on rotation. Without a pool — a single `--proxy`, which is the
    common case — an earlier version dropped it and reported only "gave up
    loading", so a refused proxy read exactly like a slow site. Found by
    running it rather than by reading it: `--proxy http://127.0.0.1:9`
    printed the generic message while `_proxy_failure()` had already
    identified ERR_PROXY_CONNECTION_FAILED.

    Asserted on the SOURCE rather than by launching a browser, because the
    branch only runs when a navigation fails and the suite must pass with no
    engine library installed at all.
    """
    for module in ENGINES:
        path = os.path.join(HERE, module + ".py")
        if not os.path.exists(path):
            continue
        source = open(path, encoding="utf-8").read()
        # Anchored on the FUNCTION that carries the decision, not on the
        # first `except _TransportError` in the file — which is the
        # client-version fallback in `_prime_session` and comes earlier.
        # The first version of this check read that block in all three
        # engines and failed on code that was correct. Reading the wrong
        # block is how a check like this passes, or fails, for the wrong
        # reason (CLAUDE.md §22).
        anchor = "def _fetch_with_policy("
        if anchor not in source:
            check("%s has a give-up branch to check" % module, False)
            continue
        start = source.index(anchor)
        end = source.find("\ndef ", start + 1)
        branch = source[start:end if end > 0 else len(source)]
        check("%s handles a transport failure at all" % module,
              "except (_TransportError, TransportError) as exc:" in branch,
              "both transports raise, and both must be caught: the browser "
              "sessions raise _TransportError and the HTTP one raises "
              "http_transport.TransportError")
        check("%s names the proxy when the proxy was the fault" % module,
              "exit_failed = _proxy_failure(exc)" in branch
              and "if exit_failed:" in branch,
              "the failure branch does not distinguish a dead exit")
        check("%s ROTATES on a dead exit rather than retrying it" % module,
              "pool.advance(exit_failed)" in branch,
              "a retry through the same dead exit is repetition, not a "
              "second attempt")
        check("%s rebuilds the browser when it rotates" % module,
              "_open_session(pw, args, pool)" in branch,
              "a rotation is a fresh browser, never a proxy swapped under "
              "a live session")
        check("%s still has a plain message for a non-proxy failure" % module,
              "failed after %d attempt(s)" in branch,
              "the non-proxy branch was lost")
        check("%s masks the exit it names" % module,
              "mask(session.proxy_url)" in branch,
              "host and port are the point of the log; the password is not")
    # ...and the detector the branch depends on must actually match the
    # string Chromium produces. Measured on a SIBLING repo 2026-09-17
    # against a dead
    # local port: `net::ERR_PROXY_CONNECTION_FAILED`.
    engine = _import_engine("playwright_scraper")
    if engine is None:
        skip("proxy-failure", "playwright_scraper not importable here")
    else:
        class _E(Exception):
            pass
        got = engine._proxy_failure(
            _E("Page.goto: net::ERR_PROXY_CONNECTION_FAILED at https://x/"))
        equal("the marker list matches what Chromium really raises", got,
              "ERR_PROXY_CONNECTION_FAILED")
        equal("...and a plain timeout is NOT read as a proxy failure",
              engine._proxy_failure(_E("Page.goto: Timeout 25000ms exceeded.")),
              "")


def check_engines_do_not_evaluate_a_string_in_the_browser():
    """§18: a site whose CSP omits `unsafe-eval` kills wait_for_function with
    an EvalError and takes the run down with exit 1, on the site's most
    obvious URL. Snapchat's is such a site: measured 2026-09-24, its
    `script-src` carries `'wasm-unsafe-eval'` and NOT `'unsafe-eval'`, so
    an evaluated string would die here rather than merely be bad habit."""
    for module in ENGINES:
        path = os.path.join(HERE, module + ".py")
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        called = {node.func.attr for node in ast.walk(tree)
                  if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute)}
        for banned in ("wait_for_function", "waitForFunction", "waitFor"):
            check("%s never CALLS %s" % (module, banned), banned not in called,
                  "poll through page_flow.wait_for_count instead")


def check_credentials_never_reach_a_log():
    """§8: an EXCEPTION MESSAGE is a log, and the masker must be GLOBAL.

    A Playwright connection error repeats the endpoint five times (the
    message plus a four-line call log), so a masker handling only the first
    occurrence prints the password four times and looks like it is working.
    """
    for module in ENGINES:
        engine = _import_engine(module)
        if engine is None:
            continue
        masked = engine._mask_credentials(
            "tried ws://u:supersecret@h1:9222 and ws://u:supersecret@h2:9222 "
            "and again ws://u:supersecret@h1:9222")
        check("%s masks EVERY occurrence" % module,
              "supersecret" not in masked, masked)
        check("%s keeps the host and port, which are the useful half" % module,
              "h1:9222" in masked and "h2:9222" in masked, masked)
    from proxy_pool import mask
    masked = mask("http://user:secret@exit.example.com:2334")
    check("proxy_pool.mask hides the password", "secret" not in masked)
    check("proxy_pool.mask keeps the exit", "exit.example.com:2334" in masked)


def check_sample_output_matches_the_schema():
    from output_writer import Profile as RowClass
    expected = [f.name for f in fields(RowClass)]
    json_path = os.path.join(HERE, "sample_output.json")
    csv_path = os.path.join(HERE, "sample_output.csv")
    if not os.path.exists(json_path):
        check("sample_output.json exists", False)
        return
    rows = json.load(open(json_path, encoding="utf-8"))
    check("sample_output.json holds rows", bool(rows))
    equal("sample_output.json keys match the schema, in order",
          list(rows[0].keys()), expected)
    check("sample_output.json is from a real run (snapchat.com rows)",
          all(r["source"] == "snapchat.com" for r in rows))
    check("...and carries no fabrication markers",
          not any("lorem" in (r.get("title") or "").lower() or
                  "example.com" in (r.get("url") or "").lower()
                  for r in rows))
    # The sample is cut from a real run and NOT anonymised. CLAUDE.md §10's
    # rule is about a private individual's words; a row here is a public
    # profile's catalogue entry — the name, bio and counts Snapchat shows
    # any logged-out visitor. What the checks pin is that it is REAL,
    # because a fabricated sample is the failure this file exists to catch.
    check("the sample's accounts are real Snapchat profiles",
          all(str(r.get("url", "")).startswith("https://www.snapchat.com/@")
              for r in rows),
          "a sample that does not point at the site is not a sample")
    public = [r for r in rows if r.get("public_profile")]
    check("...at least one of them public", bool(public))
    check("...whose ids are Snapchat's own UUIDs",
          all(re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
                           r"[0-9a-f]{12}", str(r.get("sku", ""))) for r in public),
          "a placeholder id would mean the sample was not cut from a run")
    check("...and a plausible, site-rounded subscriber count where shown",
          all(r.get("subscriber_count") is None
              or (isinstance(r["subscriber_count"], int)
                  and r["subscriber_count"] > 0
                  and r["subscriber_count"] % 100 == 0) for r in public))
    if os.path.exists(csv_path):
        header = next(csv.reader(open(csv_path, encoding="utf-8")))
        equal("sample_output.csv header matches the schema", header, expected)


def check_a_failed_page_outranks_a_complete_stop_reason():
    """`finish_run` decides completeness from the reason AND the evidence.

    A named list of stop reasons cannot cover a failure recorded anywhere
    else, which is the same hole the exit-code unification closed one
    level up. Pinned in BOTH directions: a clean run must still be
    complete, or this rule would make every run partial and someone would
    turn it off.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        for label, failed, want_status, want_rc in (
                ("with a failed page", [2], "partial", 6),
                ("with none", [], "complete", 0)):
            out = os.path.join(tmp, label.replace(" ", "_"))
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = output_writer.finish_run(
                    [Profile(sku="a"), Profile(sku="b")], out, "json", False,
                    blocked=False, stop_reason="page_cap_reached",
                    pages_requested=2, pages_completed=2, pages_failed=failed,
                    start_url="u", final_url="u", mode="profile")
            meta = json.load(open(out + ".meta.json"))
            equal("%s: status" % label, meta["status"], want_status)
            equal("%s: exit code" % label, rc, want_rc)


def check_per_page_rotation_actually_rotates_per_page():
    """The mode is named for what it does, which in this family it did not.

    `pool.advance()` used to be reached only from a dead exit or a refusal,
    so a run whose fetches all succeeded stayed on one address for its whole
    life — a flag that reads like a traffic-spreading control and was a
    recovery control.

    Counted rather than asserted qualitatively, because the obvious fix
    rotates once too often: taking a new exit after the LAST account tears
    down a browser for a request that never comes.
    """
    engine = _import_engine("playwright_scraper")
    if engine is None:
        skip("playwright_scraper", "engine library absent")
        return

    served = page("nasa")

    def fetch(box, pw, args, pool, url, label):
        return 200, served, product_parser.STATE_CONTENT, False

    def run(rotate, handles):
        # Assembled from pieces, not written out: the credential scan reads
        # this file, and a literal `user:pass@host` here would make the scan
        # fail on its own test data (CLAUDE.md §22).
        exits = ["http:" + "//" + "u" + ":" + "p" + "@one.example:1",
                 "http:" + "//" + "u" + ":" + "p" + "@two.example:2"]
        pool = proxy_pool.ProxyPool(exits, rotate=rotate)

        def body(FakeSession):
            args = _fault_args(engine, url=",".join(handles),
                               proxy_rotate=rotate)
            box = {"session": FakeSession(), "prime_url": "u"}
            engine._run_profile(box, None, args, pool)
            return pool.rotations

        return _with_stubs(engine, fetch, body)

    for handles in (["nasa"], ["nasa", "mrbeast", "khaby00"]):
        equal("per-run takes no exit over %d account(s)" % len(handles),
              run("per-run", handles), 0)
        equal("per-page takes one exit BETWEEN each of %d account(s)"
              % len(handles), run("per-page", handles), len(handles) - 1)

    check("a single-exit pool does not thrash",
          engine._rotate_if_per_page(
              {"session": None, "prime_url": "u"}, None,
              _fault_args(engine, proxy_rotate="per-page"),
              proxy_pool.ProxyPool(
                  ["http:" + "//" + "u" + ":" + "p" + "@only.example:1"],
                  rotate="per-page"), "why") is False,
          "rebuilding a browser to arrive at the same address buys nothing")


def check_a_site_that_answered_is_not_a_run_that_failed():
    """Exit 4 and exit 5 answer different questions.

    `user_unavailable` is the site ANSWERING: the handle has no account behind it. The honest code for a zero-row run is
    4 — "we asked, and the answer was nothing" — and not 5, which means
    the content was never obtained at all and sends a reader to check a
    proxy that is working fine.

    A sibling repo (youtube-scraper) had two such states regress to 5 when
    the family unified its exit codes: that rule keys on "did the run
    complete" rather than on a list of failure names, which is the right
    shape and stays. What was wrong there was the COMPLETE set, which
    enumerated only the ways a pagination LOOP can end and not the ways a
    SITE can answer.

    Pinned in both directions, because a rule that made everything
    complete would pass the first half of this check and be worse than the
    bug.
    """
    import tempfile
    answered = ("user_unavailable", "no_new_products")
    failed = ("page_load_timeout", "page_challenge", "page_error",
              "parse_error")

    for reason in answered:
        check("%r is a COMPLETE stop reason — the site answered" % reason,
              reason in output_writer.COMPLETE_STOP_REASONS,
              "a zero-row run reports 5 otherwise, which claims we never "
              "reached the site")
    for reason in failed:
        check("%r is NOT complete — we never got the content" % reason,
              reason not in output_writer.COMPLETE_STOP_REASONS)

    with tempfile.TemporaryDirectory() as tmp:
        for reason, want in ([(r, 4) for r in answered]
                             + [(r, 5) for r in failed]):
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = output_writer.finish_run(
                    [], os.path.join(tmp, reason), "json", False,
                    blocked=False, stop_reason=reason, pages_requested=1,
                    pages_completed=0, start_url="u", final_url="u",
                    mode="comments")
            equal("zero rows + %s -> exit %d" % (reason, want), rc, want)

        # And a blocked run outranks both: something stood between the run
        # and the content, which is neither "no answer" nor "no content".
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = output_writer.finish_run(
                [], os.path.join(tmp, "blocked"), "json", False,
                blocked=True, stop_reason="page_challenge", pages_requested=1,
                pages_completed=0, start_url="u", final_url="u",
                mode="comments")
        equal("a blocked run reports 3 whatever it stopped for", rc, 3)

    # The states themselves must still classify the way the parser says,
    # or the stop reasons above would never be reached.
    equal("a no-account page still classifies as such",
          product_parser.detect_page_state(page("not_found"), 404),
          product_parser.STATE_USER_UNAVAILABLE)
    equal("and an ordinary account is content, not a missing one",
          product_parser.detect_page_state(page("espn"), 200),
          product_parser.STATE_CONTENT)


def check_a_fingerprint_is_applied_whole_or_not_at_all():
    """CLAUDE.md §24: a HALF identity is measured worse than none.

    Four defects lived on this path, every one of them a SILENT success —
    the call was accepted, the log said nothing, and the page disagreed
    with the fingerprint. All four were found by reading the values back
    out of a live page on 2026-09-21, which is what §24 tells you to do
    and which no amount of reading this code would have produced:

      1. `navigator.languages` reported `["en-US"]` against the
         fingerprint's `["en-US", "en"]`, because Playwright's `locale=`
         sets the PRIMARY language only.
      2. `navigator.userAgentData.brands` reported `HeadlessChrome/153`
         while the user agent claimed `Chrome/150` — the client hints are
         the half a `user_agent=` option leaves behind.
      3. Detaching the CDP session REVERTED the override, and the protocol
         reported success either way.
      4. In pyppeteer the init script never ran: `evaluateOnNewDocument`
         wraps its argument as a function expression, and
         `Page.addScriptToEvaluateOnNewDocument` silently does nothing
         until `Page.enable` has been sent — it answers
         `{"identifier": "1"}` regardless.

    These are asserted on the SOURCE and on the pure functions, because
    the branch needs a live browser and a paid key, and the suite must
    pass with neither.
    """
    import fingerprint_client as F

    # A fingerprint shaped like the ones the live API returns.
    fp = {
        "userAgent": {
            "value": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                     "AppleWebKit/537.36 (KHTML, like Gecko) "
                     "Chrome/150.0.0.0 Safari/537.36",
            "brandVersionList": [{"brand": "Not;A=Brand", "version": "8"},
                                 {"brand": "Chromium", "version": "150"},
                                 {"brand": "Google Chrome", "version": "150"}],
            "brandFullVersionList": [{"brand": "Chromium",
                                      "version": "150.0.0.0"}],
            "platform": "Windows", "platformVersion": "19.0.0",
            "architecture": "x86", "bitness": "64", "model": "",
            "mobile": False, "fullVersion": "150.0.0.0",
        },
        "navigator": {"platform": "Win32", "hardwareConcurrency": 32,
                      "deviceMemory": 32},
        "intl": {"languages": ["en-US", "en"], "contentLocale": "en-US",
                 "timeZone": "America/New_York"},
        "screen": {"width": 2560, "height": 1440, "deviceScaleFactor": 1.5},
        "webgl": {"vendor": "Google Inc.", "renderer": "ANGLE (NVIDIA)"},
    }

    metadata = F.user_agent_metadata(fp)
    check("a complete fingerprint yields client hints", bool(metadata))
    equal("the brands come from the fingerprint, not the browser",
          [b["brand"] for b in metadata["brands"]],
          ["Not;A=Brand", "Chromium", "Google Chrome"])
    equal("platform", metadata["platform"], "Windows")
    equal("platformVersion", metadata["platformVersion"], "19.0.0")
    equal("bitness", metadata["bitness"], "64")
    equal("mobile is a real bool", metadata["mobile"], False)

    # The refusal half: no brand list means no metadata, so the caller
    # leaves the hints alone rather than applying a fragment of one.
    equal("an incomplete fingerprint yields NO metadata",
          F.user_agent_metadata({"userAgent": {"platform": "Windows"}}), None)
    equal("...and neither does an empty one",
          F.user_agent_metadata({}), None)

    # Accept-Language carries no q-values, and the reason is written down.
    equal("Accept-Language is built without q-values",
          F.accept_language(fp), "en-US,en")
    check("...and why is recorded beside it",
          "q-value" in inspect.getdoc(F.accept_language),
          "Chromium derives navigator.languages from this string and keeps "
          "the qualifier, which no real browser reports")
    equal("no languages, no header", F.accept_language({}), None)

    # The init script carries the language list, which `locale=` cannot.
    script = F.playwright_init_script(fp)
    check("the init script carries navigator.languages",
          "'languages'" in script and "en-US" in script)
    check("...and the platform and WebGL strings with it",
          "'platform'" in script and "37446" in script)

    # And every engine applies the two TOGETHER.
    for module in ENGINES:
        path = os.path.join(HERE, module + ".py")
        if not os.path.exists(path):
            continue
        source = open(path, encoding="utf-8").read()
        check("%s applies the client hints beside the user agent" % module,
              '"userAgent": user_agent, "userAgentMetadata": metadata'
              in source,
              "a bare override is the thing §24 measured being refused")
        check("%s refuses to apply a partial identity" % module,
              "HALF identity is worse than" in source,
              "no brand list must mean no override at all")
        check("%s never detaches the session that carries it" % module,
              ".detach()" not in source,
              "detaching reverts the override, and the call succeeds anyway")
        # The timezone reaches the browser by a DIFFERENT route in each
        # engine, and that is legitimate rather than drift: Playwright
        # takes `timezone_id` as a context option, which is what
        # `playwright_context_kwargs` sets; the other two have no such
        # option and send `Emulation.setTimezoneOverride`. Naming the
        # route per engine is what keeps a missing one visible — all three
        # were verified against a live page reporting America/New_York on
        # 2026-09-21.
        route = ("playwright_context_kwargs" if module == "playwright_scraper"
                 else "Emulation.setTimezoneOverride")
        check("%s applies the fingerprint's timezone (via %s)"
              % (module, route), route in source,
              "a browser reporting UTC under a New York fingerprint "
              "contradicts itself on an axis any script reads")

    puppeteer = os.path.join(HERE, "puppeteer_scraper.py")
    if os.path.exists(puppeteer):
        source = open(puppeteer, encoding="utf-8").read()
        check("pyppeteer enables the Page domain before adding the script",
              'send("Page.enable"' in source,
              "without it the protocol answers success and runs nothing")
        check("...and does not use the wrapper that mangles the source",
              "evaluateOnNewDocument(\n" not in source
              and "page.evaluateOnNewDocument(" not in source,
              "that wrapper emits `(<source>)()` and Chromium drops the "
              "syntax error in silence")
        check("...and installs it through the raw protocol command instead",
              "Page.addScriptToEvaluateOnNewDocument" in source)


def check_the_credential_scan_covers_the_files_it_most_needs_to():
    """The repo's own guard was blind to its biggest files, twice over.

    Both were found by PLANTING a real-shaped key and running the scan
    rather than by reading it (CLAUDE.md §23), and both reported
    "nothing credential-shaped" over a file that held one:

      1. `.json` and `.csv` were not in `SCANNED_SUFFIXES` at all, so
         `fixtures_generated.json` — 500-odd KB of captured page payload,
         which is precisely where a front-end key or a session token
         arrives — was never opened.
      2. With the suffixes added it STILL passed, because the allowlists
         were applied per LINE and that fixture is a single line. It
         contains "sha" 69 times and "hash" 36 times, so one allowlisted
         token anywhere in it exempted every match in the whole file. A
         line-scoped allowlist becomes a FILE-scoped one the moment a file
         is one line.

    Added with no new allowlist entries, which is the point: the real
    fixtures and sample carry zero 32-hex strings and zero credentialled
    URLs, so the strictest rule now covers the largest files instead of
    acquiring an exception a real key could hide behind (CLAUDE.md §24).
    """
    sys.path.insert(0, os.path.join(HERE, ".github"))
    import ci_checks

    for suffix in (".json", ".csv"):
        check("the scan opens %s files" % suffix,
              suffix in ci_checks.SCANNED_SUFFIXES,
              "the generated fixtures and the committed sample are these")

    scanned = {str(p.relative_to(ci_checks.REPO))
               for p in ci_checks.scanned_files()}
    for name in ("fixtures_generated.json", "sample_output.json",
                 "sample_output.csv"):
        check("the scan reaches %s" % name, name in scanned,
              "it is committed, and it is captured payload")

    # The window, and that it is narrower than a one-line fixture.
    check("the allowlist is scoped to a window, not to a line",
          hasattr(ci_checks, "_allowed_near"),
          "a per-line allowlist exempts a whole one-line file")
    equal("a token beside the match still excuses it",
          ci_checks._allowed_near("md5 " + "a" * 32, 4, 36,
                                  ci_checks.HEX32_ALLOWED, lower=True), True)
    far = "md5" + " " * 400 + "b" * 32
    equal("a token 400 characters away does not",
          ci_checks._allowed_near(far, len(far) - 32, len(far),
                                  ci_checks.HEX32_ALLOWED, lower=True), False)
    check("the window is narrower than the fixture is long",
          ci_checks.ALLOWLIST_WINDOW * 2 <
          len(json.dumps(FIX, ensure_ascii=False)),
          "otherwise the fixture is one window and nothing is scoped")

    # And the escaped-quote half: a fixture stored as JSON escapes every
    # quote inside it, so a pattern with plain quotes matches nothing.
    #
    # The sample is ASSEMBLED from pieces rather than written out, and that
    # is not fussiness — the scan now reads this file, and a literal
    # credentialled URL here would make the check fail on its own test
    # data. CLAUDE.md §22: a note about a banned string is a use of it, and
    # assembling is what lets the scan cover the suite instead of
    # exempting the one file most likely to acquire a pasted secret.
    sample = "ws:" + "//" + "acct7" + ":" + "s3cr3tpw" + "@" + "host:9222"
    check("the credentialled-URL pattern tolerates an escaped quote",
          ci_checks.CREDENTIALLED_URL.search('{"e": \\"%s\\"}' % sample)
          is not None)
    check("...and the bare form too",
          ci_checks.CREDENTIALLED_URL.search('{"e": "%s"}' % sample)
          is not None)
    # A documented placeholder must still be allowed, or the check becomes
    # one people switch off.
    placeholder = "ws:" + "//" + "user" + ":" + "pass" + "@" + "host:9222"
    check("a documented placeholder is still allowed",
          ci_checks._allowed_near(placeholder, 0, len(placeholder),
                                  ci_checks.CREDENTIAL_ALLOWED))


def check_ci_greps_for_a_sentinel_this_suite_can_actually_emit():
    """The `engine-smoke` job must be ABLE to fail.

    That job exists for one reason (CLAUDE.md §10): "skipped, engine absent"
    reads identically to a real import error, so CI installs each engine and
    fails if THAT engine's group still reports a skip. It works by grepping
    the suite's own output for a sentinel.

    The inherited version grepped for `"<engine>_scraper could not be
    imported"` — a string no suite in this family emits. Checked 2026-09-18,
    seven sibling repos carry the same dead grep, so in none of them could
    the job ever have failed. It passed for the wrong reason, which is
    §22's "a check that swallows the loudest failure it could report".

    This check is the guard against that coming back: whatever sentinel the
    workflow looks for, this suite has to be capable of printing it. It
    verifies the sentinel against `skip()`'s real output format rather than
    against a copy of the string, so changing either one without the other
    fails here.
    """
    workflow = os.path.join(HERE, ".github", "workflows", "tests.yml")
    if not os.path.isdir(os.path.join(HERE, ".github")):
        # §22: trigger on the WHOLE .github directory being absent — which
        # is the Docker image, where it is deliberately not COPYed — and
        # never on a file inside it going missing, because a check that
        # quietly starts passing once its input disappears is the failure
        # mode this whole function is about.
        skip("ci-sentinel", "no .github/ in this tree (the Docker image)")
        return
    check("tests.yml exists", os.path.exists(workflow))
    if not os.path.exists(workflow):
        return
    text = open(workflow, encoding="utf-8").read()

    # What `skip()` actually prints, derived rather than quoted.
    import io as _io, contextlib as _contextlib
    buf = _io.StringIO()
    before = len(SKIPS)
    with _contextlib.redirect_stdout(buf):
        skip("playwright_scraper", "engine library absent (probe)")
    del SKIPS[before:]          # leave the run's real skip list untouched
    printed = buf.getvalue()
    check("skip() prints a line naming the engine", "playwright_scraper" in printed,
          repr(printed))

    # The sentinel the workflow greps for, with the matrix placeholder
    # resolved the way Actions would resolve it.
    greps = re.findall(r'grep -q(?:E)? "([^"]*matrix\.engine[^"]*)"', text)
    check("the engine-smoke step greps for something", bool(greps),
          "no grep against ${{ matrix.engine }} found in tests.yml")
    for pattern in greps:
        resolved = pattern.replace("${{ matrix.engine }}", "playwright")
        check("the CI sentinel %r is a string this suite can emit" % resolved,
              resolved in printed,
              "the workflow greps for %r but skip() prints %r — the job "
              "cannot fail" % (resolved, printed.strip()))

    # And the other half: the step must confirm the suite RAN, or a crash on
    # line one sails past a grep for an absent string.
    check("the engine-smoke step also asserts the suite ran to completion",
          "checks passed" in text,
          "nothing in tests.yml checks for the suite's summary line")


def main():
    global VERBOSE
    parser = argparse.ArgumentParser(description="snapchat-scraper offline suite")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()
    VERBOSE = args.verbose

    global _TREE_BEFORE
    _TREE_BEFORE = _tree_state()

    for fn in CHECKS:
        if VERBOSE:
            print("\n== %s" % fn.__name__)
        try:
            fn()
        except Exception as e:  # noqa: BLE001 — a broken check is a failure
            import traceback
            FAILURES.append("%s raised %s: %s" % (fn.__name__, type(e).__name__, e))
            print("  ERROR %s raised %s: %s" % (fn.__name__, type(e).__name__, e))
            if VERBOSE:
                traceback.print_exc()

    print("\n%d checks passed, %d failed, %d group(s) skipped."
          % (PASSED, len(FAILURES), len(SKIPS)))
    for line in SKIPS:
        print("  skipped: %s" % line)
    if FAILURES:
        print("\nFailures:")
        for line in FAILURES:
            print("  - %s" % line)
        return 1
    return 0


def check_no_module_defines_a_name_twice():
    """A second `def` of the same name silently replaces the first.

    Found in this family by the offline suite rather than by reading: an
    assembled engine carried TWO copies of its whole shared layer —
    `_open_http`, `_prime_session`, `handle_captcha_if_present`, `_call`,
    `_fetch_with_policy`, `_rotate_if_per_page` and `_worker_pool`, ten
    functions, four hundred lines. Python bound the second copy and threw
    the first away.

    Nothing else could see it. The module imported, `--help` worked,
    `compileall` passed, the undefined-name walk was clean, every engine
    produced correct rows, and all three agreed with each other — because
    the two copies were IDENTICAL. What gave it away was a check counting
    solver call sites, which found four where the design has two.

    That is the dangerous shape: a duplicate that is currently harmless
    and becomes a silent wrong answer the moment somebody edits one copy.
    CLAUDE.md §22's statement-after-return check is the same family of
    defect — something the parser accepts and a reader never sees.
    """
    import collections
    for name in ENGINES + ("product_parser", "page_flow", "output_writer",
                           "proxy_pool", "env_config", "diff_runs",
                           "captcha_solver", "fingerprint_client",
                           "http_transport", "scraper_api_client",
                           "snapchat_payload", "make_fixtures",
                           "smoke_test"):
        path = os.path.join(HERE, name + ".py")
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        seen = collections.Counter(
            node.name for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)))
        duplicates = {k: v for k, v in seen.items() if v > 1}
        check("%s.py defines no name twice" % name, not duplicates,
              "%r — the later definition silently replaces the earlier, and "
              "nothing else in this repo can see that" % duplicates)


def check_no_statement_follows_a_return_in_the_same_block():
    """CLAUDE.md §22: found the same fifteen dead lines in six repos.

    A function whose `def` line had been lost, leaving its docstring and
    body absorbed into the end of the function above it. It parses, it
    imports, `--help` works, `compileall` passes — and the
    undefined-name walk cannot see it and SHOULD not, because it pools
    every binding in a file rather than tracking scopes.

    Measured across eighteen repos when it was written: 6 hits, 0 false
    positives.
    """
    terminators = (ast.Return, ast.Raise, ast.Break, ast.Continue)
    hits = []
    for path in sorted(pathlib.Path(HERE).glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            for field in ("body", "orelse", "finalbody"):
                block = getattr(node, field, None)
                if not isinstance(block, list):
                    continue
                for i, stmt in enumerate(block[:-1]):
                    if isinstance(stmt, terminators):
                        nxt = block[i + 1]
                        hits.append("%s:%d (after %s on line %d)"
                                    % (path.name, nxt.lineno,
                                       type(stmt).__name__.lower(),
                                       stmt.lineno))
    check("no statement follows a return/raise/break/continue", not hits,
          "; ".join(hits[:6]))


def check_the_scraping_browsers_own_extension_does_not_read_as_a_challenge():
    """The marker check, against a page fetched the way a PAID run fetches.

    CLAUDE.md §21 records a guard that passed for the WRONG REASON: it ran
    only against captures taken with a plain HTTP client, which carry no
    extension injection at all. Every other fixture in this repo is such a
    capture. This one is the material a real `--cdp-endpoint` fetch adds.

    It was a recorded SKIP until a live Scraping Browser profile turned up
    on 2026-09-22 — and the reason it was a skip, rather than an omission,
    is that "not known to be injected" is not "measured not to be".
    """
    served = _SERVED_PAGES()
    check("there are served fixtures to test against", bool(served))

    # The marker set must score zero on a served page WITH the injection
    # spliced into it — which is what a paid run actually receives.
    for name, html in served:
        with_injection = html.replace("<body>", "<body>" + CDP_INJECTED)
        hits = product_parser.challenge_markers_present(with_injection)
        check("no marker fires on %s fetched over --cdp-endpoint" % name,
              not hits,
              "fired: %s — the Scraping Browser's own extension is being "
              "read as the site's challenge" % hits)
        equal("...and it still classifies as content, on %s" % name,
              product_parser.detect_page_state(with_injection, 200),
              product_parser.STATE_CONTENT)

    # And the set must score zero WITHOUT an extension strip, or the strip
    # becomes load-bearing and the next marker added inherits the hole
    # (CLAUDE.md §24).
    for marker in snapchat_payload.BOT_CHALLENGE_MARKERS:
        check("marker %r is absent from the injected material" % marker,
              marker not in CDP_INJECTED,
              "a marker the extension injects would report every paid run "
              "as blocked")

    # The specific names this family has been burned by, pinned as
    # PRESENT in the injection — so that if a future edit adds one to the
    # marker set, the check above fails loudly rather than the repo
    # shipping a scraper that reports exit 3 on a full catalogue.
    for burned in ("cf-turnstile", "captcha-widgets", "hunter.js",
                   "data-ts-input"):
        check("the extension really does inject %r" % burned,
              burned in CDP_INJECTED,
              "if this stops being true the fixture is stale — recapture "
              "it over --cdp-endpoint")


def check_diff_runs_watches_this_repos_columns():
    """Every column diff_runs.py watches exists on this repo's row class.

    Its TRACKED_FIELDS once named a sibling's columns, so the diff
    compared almost nothing and
    reported "0 changed" on runs where a watched value had changed. Pinned
    by name AND by behaviour: a changed tracked column must be reported.
    """
    import copy
    import dataclasses
    import diff_runs
    import output_writer
    names = {f.name for f in dataclasses.fields(output_writer.Profile)}
    for mode, cls in output_writer.ROW_CLASS_BY_MODE.items():
        own = {f.name for f in dataclasses.fields(cls)}
        stray = [n for n in diff_runs.TRACKED_FIELDS_BY_MODE[mode] if n not in own]
        check("every column diff_runs watches in %s exists on %s"
              % (mode, cls.__name__), not stray, "not on the class: %r" % stray)
    check("diff_runs tracks at least one column",
          len(diff_runs.TRACKED_FIELDS) > 0)
    missing = [n for n in diff_runs.TRACKED_FIELDS + diff_runs.SOURCE_ONLY_FIELDS
               if n not in names]
    check("every column diff_runs watches exists on the row class",
          not missing, "not on the row class: %r" % missing)
    check("SOURCE_ONLY_FIELDS is a subset of TRACKED_FIELDS",
          set(diff_runs.SOURCE_ONLY_FIELDS) <= set(diff_runs.TRACKED_FIELDS))
    equal("source_changed is inert here: one payload, no second source",
          (diff_runs.SOURCE_COLUMN, diff_runs.SOURCE_ONLY_FIELDS), (None, ()))
    equal("every mode has tracked fields", sorted(diff_runs.TRACKED_FIELDS_BY_MODE),
          sorted(output_writer.ROW_CLASS_BY_MODE))
    check("SOURCE_COLUMN is a real column or None",
          diff_runs.SOURCE_COLUMN is None or diff_runs.SOURCE_COLUMN in names)

    def bumped(value):
        if isinstance(value, bool):
            return not value
        if isinstance(value, (int, float)):
            return value + 1
        if isinstance(value, list):
            return value + ["changed"]
        return str(value) + " (changed)"

    with open(os.path.join(HERE, "sample_output.json"), encoding="utf-8") as fh:
        base = json.load(fh)[0]
    plain = [f for f in diff_runs.TRACKED_FIELDS
             if f not in diff_runs.SOURCE_ONLY_FIELDS and base.get(f) is not None]
    check("the sample row has a tracked column to change", bool(plain))
    if plain:
        after = copy.deepcopy(base)
        after[plain[0]] = bumped(base[plain[0]])
        result = diff_runs.diff_products([base], [after])
        equal("a changed %s is reported as changed" % plain[0],
              [list(c["changes"]) for c in result["changed"]], [[plain[0]]])
    split = [f for f in diff_runs.SOURCE_ONLY_FIELDS if base.get(f) is not None]
    if diff_runs.SOURCE_COLUMN and split:
        after = copy.deepcopy(base)
        after[split[0]] = bumped(base[split[0]])
        after[diff_runs.SOURCE_COLUMN] = str(base.get(diff_runs.SOURCE_COLUMN)) + "-other"
        result = diff_runs.diff_products([base], [after])
        check("a %s difference with a %s difference is source_changed"
              % (split[0], diff_runs.SOURCE_COLUMN),
              len(result["source_changed"]) == 1 and not result["changed"],
              "got %r" % result)


def check_scraper_api_waitfor_is_object_and_status_is_http_code():
    """Measured 2026-09-23 against the live Scraper API: a `waitFor` sent as
    a JSON-encoded STRING is answered HTTP 422 ("params.waitFor must be an
    object") and still billed; an object is answered 200. And the target's
    status is `http_code` -- `status` is the API's own "success", which must
    never be what reaches the page classifier. Driven through the real
    fetch_html with requests.post stubbed, so no network and no key."""
    import argparse
    import scraper_api_client as sac
    captured = {}

    class _Resp:
        status_code = 200
        headers = {}
        text = ""

        def json(self):
            return {"status": "success", "http_code": 403, "body": "<html></html>"}

    def _post(url, headers=None, json=None, timeout=None, **_kw):  # noqa: A002
        captured["payload"] = json
        return _Resp()

    real_post = sac.requests.post
    sac.requests.post = _post
    try:
        args = argparse.Namespace(url='https://www.snapchat.com/@nasa', key="k", timeout=60,
                                  cdp_url=None, wait_text='Snapchat',
                                  wait_element=None, wait_state=None)
        result = sac.fetch_html(args)
    finally:
        sac.requests.post = real_post
    status = result[1] if isinstance(result, tuple) else None
    wf = (captured.get("payload") or {}).get("waitFor")
    check(f"Scraper API: --wait-text must send waitFor as an OBJECT (a string "
          f"is HTTP 422 and still billed), got {wf!r}",
          isinstance(wf, dict) and wf.get("text") == 'Snapchat')
    check(f"Scraper API: the status handed onward must be the target's "
          f"http_code 403 (int), not the API's own verdict, got {status!r}",
          status == 403 and isinstance(status, int))


CHECKS = [v for k, v in sorted(globals().items()) if k.startswith("check_")
          and callable(v) and k != "check"]

if __name__ == "__main__":
    sys.exit(main())
