"""
output_writer.py
-----------------
Shared row model + JSON/CSV writers used by all three engines and the
HTTP path.

Three modes, three row shapes, ONE fetch
----------------------------------------
    --mode profile     one row per account: subscribers, bio, category,
                       website, ids, and what the account publishes
    --mode spotlight   one row per Spotlight video on the profile, with
                       its views, shares, comments, boosts and sound
    --mode story       one row per snap: the live story and every
                       curated highlight, with media URLs and timestamps

All three read the same page — `https://www.snapchat.com/@handle` — so a
mode is a choice of which part of one server-rendered payload to emit, not
a different request. Every class keeps the family prefix
`source, scraped_at, url, sku, title` byte-identical and first
(CLAUDE.md §9), `mode` is recorded in the sidecar because the repo no
longer implies it, and `diff_runs.py` refuses a pair whose modes differ.

`sku` is the id in every schema, so one column name works across the
family:

    profile     the account's business-profile id (a UUID) when the account
                has a PUBLIC profile, else the username. See `Profile`.
    spotlight   the Spotlight id — the path segment of
                https://www.snapchat.com/spotlight/{id}
    story       the snap id

Two traps that shape the columns
--------------------------------
Measured 2026-09-24 on real captures:

* **Zero is "hidden", not zero.** `subscriberCount` is `"0"` on accounts
  that hide it (@khaby00 — 8 Spotlight videos, a public profile, and a
  JSON-LD `interactionStatistic` that is an EMPTY list rather than a
  counter of 0). Written through, that zero drags every average a consumer
  computes. It is null here, and the page's own JSON-LD is what tells the
  two apart (CLAUDE.md §21: where the site states it, use its signal).
* **A Spotlight slot can be EMPTY.** A profile page lists its Spotlight
  videos as two parallel arrays, and on @kyliejenner 14 of the 25 slots in
  both were placeholders — `viewCount: "0"`, `uploadDateMs: "0"`, no id —
  at the same indices, stable over three refetches. Read naively, that is
  fourteen videos with no views. They are not emitted, `position` counts
  the rows that ARE (CLAUDE.md §24's rule from a sibling whose payload
  index shifted every position), and the sidecar counts the holes.

Everything below the dataclasses is row-class-agnostic: pass `row_cls` so
an empty CSV still gets the right header for the mode that produced it.
"""

import contextlib
import csv
import json
import os
import tempfile
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime, timezone
from typing import Optional, List, Set, Sequence, Any, Type


# The site a row came from. `snapchat.com/add/{user}` answers HTTP 308
# into `www.snapchat.com/@{user}`, so every accepted input form lands on
# one canonical path; this column names the SITE.
SOURCE_DEFAULT = "snapchat.com"


def utc_now() -> str:
    """The run's timestamp, as a UTC ISO-8601 string with a `Z`.

    One helper so every row in a run can be given the SAME stamp by the
    caller rather than each row calling the clock. Rows from one page that
    disagree in `scraped_at` by a few milliseconds make a diff noisier for
    no information.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass
class Profile:
    """One Snapchat account.

    The family prefix — `source`, `scraped_at`, `url`, `sku`, `title` — is
    byte-identical and in this order across every repo in the family
    (CLAUDE.md §9).

    `sku` is the account's `businessProfileId` when it has a PUBLIC profile.
    Snapchat lets a user change their username, so a diff joined on the
    username would report one renamed account as a deletion plus an
    arrival. An account WITHOUT a public profile publishes no id at all —
    the page carries its username and a Snapcode and nothing else — so its
    `sku` is the lower-cased username, and `public_profile` says which of
    the two a row's key is.

    `title` is the display name, which is the account's human label.
    """

    source: str = SOURCE_DEFAULT
    scraped_at: str = ""
    url: Optional[str] = None
    sku: Optional[str] = None
    title: Optional[str] = None

    # ---- identity ---------------------------------------------------------
    username: Optional[str] = None
    # True: a Public Profile (creator, brand, public figure) — every column
    # below may be populated. False: an ordinary account, which Snapchat
    # serves as a username and a Snapcode and nothing more, measured on
    # @djkhaled and @espn on 2026-09-24. A False row is still a real
    # answer — the account EXISTS — which is exactly what a nonexistent
    # handle's HTTP 404 is not.
    public_profile: Optional[bool] = None
    business_profile_id: Optional[str] = None
    # A second UUID Snapchat publishes beside the first. Carried because
    # it is the one Snapchat's own share and messaging links use.
    host_user_id: Optional[str] = None
    # "Person" | "Organization", from the page's own JSON-LD mainEntity.
    entity_type: Optional[str] = None

    # ---- counts -----------------------------------------------------------
    # Rounded BY THE SITE to the nearest hundred on every public account
    # measured (757,800 / 1,466,200 / 29,262,900), in both the payload
    # and the JSON-LD — there is no exact figure anywhere on the page to
    # read instead. NULL when the account hides it; see the module note.
    subscriber_count: Optional[int] = None
    # Counts of what THIS PAGE carries, not lifetime totals. A profile page
    # lists at most 25 Spotlight videos and 18 highlights in what was
    # measured; the rest are behind a cursor this repo does not follow.
    spotlight_count: Optional[int] = None
    highlight_count: Optional[int] = None
    story_snap_count: Optional[int] = None
    lens_count: Optional[int] = None
    # The site's own flags, carried because the counts above are bounded
    # by one page and these are not.
    has_story: Optional[bool] = None
    has_curated_highlights: Optional[bool] = None
    has_spotlight_highlights: Optional[bool] = None

    # ---- what the account says about itself --------------------------------
    bio: Optional[str] = None
    website_url: Optional[str] = None
    # Free text the account types in the "address" slot. Often a real
    # address on a business; on @khaby00 it reads "Subscribe for a Cookie".
    address: Optional[str] = None
    # Snapchat's own category ids (`public-profile-category-v3-people`),
    # passed through rather than mapped to words this repo would invent.
    category: Optional[str] = None
    subcategory: Optional[str] = None
    # Snapchat's badge value, passed through: 1 on every verified-looking
    # account measured and 0 on @khaby00, but Snapchat does not document
    # the enum, so it is not renamed to `verified` (CLAUDE.md §8).
    badge: Optional[int] = None
    profile_picture_url: Optional[str] = None
    hero_image_url: Optional[str] = None
    snapcode_url: Optional[str] = None

    # ---- when -------------------------------------------------------------
    # From the page's JSON-LD ProfilePage, which is ABSENT on some profile
    # pages (0 blocks on @kyliejenner, whose page opens on a live story) —
    # so these are sparse for a structural reason, not a parsing one.
    created_at: Optional[str] = None
    modified_at: Optional[str] = None

    page: Optional[int] = None
    position: Optional[int] = None


@dataclass
class Spotlight:
    """One Spotlight video, as listed on its creator's profile page."""

    source: str = SOURCE_DEFAULT
    scraped_at: str = ""
    url: Optional[str] = None
    # The Spotlight id.
    sku: Optional[str] = None
    # Snapchat's own AI-generated title where it publishes one
    # (`llmTitle`), else the video's `name`. The `name` is the literal
    # "Spotlight Snap" on most videos, which is why it is not preferred.
    title: Optional[str] = None

    username: Optional[str] = None
    creator_name: Optional[str] = None

    # ---- engagement, exact integers ---------------------------------------
    view_count: Optional[int] = None
    share_count: Optional[int] = None
    comment_count: Optional[int] = None
    # "Boosts" — Snapchat's own engagement counter on a Spotlight.
    boost_count: Optional[int] = None
    recommend_count: Optional[int] = None

    # ---- the video --------------------------------------------------------
    description: Optional[str] = None
    # Snapchat's AI-generated description (`llmDescription`), labelled as
    # such: it is the SITE's text, not the creator's.
    generated_description: Optional[str] = None
    caption: Optional[str] = None
    hashtags: List[str] = field(default_factory=list)
    #
    # `keywords` was here and is GONE: `videoMetadata.keywords` was an empty
    # list on all 36 Spotlight rows across the fixtures measured 2026-09-24.
    # CLAUDE.md §9 — a column empty on every row should not exist, and the
    # measurement is written down so it can come back with a better one.
    duration_s: Optional[float] = None
    width: Optional[int] = None
    height: Optional[int] = None
    uploaded_at: Optional[str] = None
    # The MP4. Unsigned — measured 2026-09-24, the URL answers identically
    # with its query string removed — so it does not expire by signature.
    video_url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    # The "Original Sound" / track card, where the video carries one.
    sound_title: Optional[str] = None
    sound_artist: Optional[str] = None

    page: Optional[int] = None
    position: Optional[int] = None


@dataclass
class Snap:
    """One snap from an account's live story or one of its highlights."""

    source: str = SOURCE_DEFAULT
    scraped_at: str = ""
    url: Optional[str] = None
    # The snap id.
    sku: Optional[str] = None
    # The highlight's title, or null for a live-story snap (a story has none).
    title: Optional[str] = None

    username: Optional[str] = None
    # "story" — the live story, which expires | "highlight" — a curated,
    # saved collection.
    collection: Optional[str] = None
    highlight_id: Optional[str] = None
    # Order inside the collection, from Snapchat's own `snapIndex`.
    snap_index: Optional[int] = None
    # "image" | "video", from Snapchat's `snapMediaType` (0 / 1). Checked on
    # real captures: every 0 was a JPEG and every 1 an MP4.
    media_type: Optional[str] = None
    media_url: Optional[str] = None
    preview_url: Optional[str] = None
    posted_at: Optional[str] = None

    page: Optional[int] = None
    position: Optional[int] = None


# The family name. CLAUDE.md §9 makes `Product` the schema every repo in
# this family exports, and several shared checks import it by that name;
# keeping the alias means the descriptive name can be used in this repo's
# own code without reddening a check that was written against the family.
Product = Profile


# Row classes by --mode, so an engine maps its mode to a schema in one
# place.
ROW_CLASS_BY_MODE = {"profile": Profile, "spotlight": Spotlight,
                     "story": Snap}

# Modes whose rows are one-per-sku, and therefore safe to dedupe on `sku`
# and to hand to diff_runs.py. All three: an account, a Spotlight video
# and a snap each have their own id, and a video two accounts both list is
# ONE video.
UNIQUE_BY_SKU_MODES = ("profile", "spotlight", "story")


def dedupe_by_key(rows: Sequence[Any], seen: Set[str], key: str = "sku") -> List[Any]:
    """Drop rows whose key already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread the same set across pages —
    a repeated page then re-parses without duplicating its rows into the
    final output.

    The function stays regardless of whether it fires — it is the backstop
    that keeps the output clean, and "should never fire" is a poor reason to
    remove a guard that costs one pass over a list.

    A row with no key is always kept: there is nothing to check a duplicate
    against, and dropping it would be a silent data loss rather than a
    duplicate removal.
    """
    fresh = []
    for r in rows:
        val = getattr(r, key, None)
        if val is None or val not in seen:
            if val is not None:
                seen.add(val)
            fresh.append(r)
    return fresh


# Kept under its old name: the engines and smoke tests in this family all
# call it, and a listing run does dedupe by sku.
def dedupe_by_sku(rows: Sequence[Any], seen: Set[str]) -> List[Any]:
    return dedupe_by_key(rows, seen, key="sku")


# CSV cannot hold a list. Joining with " | " keeps the cell readable in a
# spreadsheet and round-trippable by splitting on the same separator; the
# JSON output keeps the real list, so nothing is lost for a consumer that
# wants structure. `repr()` of a Python list (the default if this is not
# handled) is neither readable nor parseable by anything but Python.
LIST_CSV_SEPARATOR = " | "


def _csv_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return LIST_CSV_SEPARATOR.join(str(x) for x in v)
    return v


@contextlib.contextmanager
def _atomic(path: str, newline: Optional[str] = None):
    """Write to a temporary file beside `path`, then rename over it.

    Every write here replaces a file a previous run may have left, and the
    invariant this module exists to protect is that a bad run never
    destroys last night's good data (`save` refuses to overwrite with an
    empty result for the same reason). Writing in place gives that up at
    the worst moment: a kill, a full disk or a crash halfway through
    `json.dump` leaves a TRUNCATED file where a complete one was, and the
    sidecar beside it still describes the old, good run.

    `os.replace` is atomic on POSIX and on Windows, so a reader sees
    either the whole previous file or the whole new one and never half of
    either. The temporary file is created in the SAME directory, because a
    rename across filesystems is not atomic and would silently degrade to
    a copy.

    `fsync` before the rename is what makes that true after a power loss
    rather than only after a crash — without it the rename can reach the
    disk before the bytes do.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    handle = tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline=newline, dir=directory,
        prefix=os.path.basename(path) + ".", suffix=".tmp", delete=False)
    try:
        with handle:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        # Leave the destination untouched. A failed write must not be
        # visible at all, which is the whole point of writing aside.
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def write_json(rows: Sequence[Any], path: str) -> None:
    with _atomic(path) as f:
        json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)


def write_csv(rows: Sequence[Any], path: str, row_cls: Type = Profile) -> None:
    # An empty result still gets the header row. A zero-byte file makes a
    # consumer fail on read (no columns to parse) instead of reading a valid
    # table with zero rows — and "an empty result is still a well-formed
    # result" is the same principle as `save` refusing to overwrite good data.
    #
    # The header comes from `row_cls`, not from the first row, so an empty
    # run still writes the columns of the mode that produced it.
    fieldnames = [f.name for f in fields(row_cls)]
    with _atomic(path, newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: _csv_value(v) for k, v in asdict(r).items()})


# Exit code used when a run completes but produced nothing. Distinct from 1
# (crash) so a caller can tell "ran, found nothing" from "blew up".
EXIT_NO_PRODUCTS = 4

# Exit code for a run blocked by a bot-check/challenge page before parsing
# even started — distinct from EXIT_NO_PRODUCTS so a caller can tell "the
# site genuinely had nothing" from "something stood between us and the
# content".
#
# On Snapchat this code does NOT cover the states that look like it and
# are not: a handle with no account (HTTP 404, the site's own not-found
# page) and an ordinary account with no public profile (HTTP 200, a
# username and nothing else). Both are real answers. Reporting one as
# blocked sends a user hunting for a proxy problem that does not exist.
#
# What would be blocked here has not been OBSERVED: measured 2026-09-24,
# every profile page asked for — from a datacentre address, to plain curl
# and to a Chrome User-Agent, thirty in a row with no delay — was served.
# The code exists because a scraper that cannot report a block reports
# success instead, not because this site has been seen to refuse.
EXIT_BLOCKED = 3

# Exit code for a run that gathered SOME rows and then stopped early — a
# page-load timeout, a 503 throttle, or a challenge on page 3 of 10. The
# output file is still written (throwing away three good pages would be
# worse), but it is not a complete picture, and a consumer that cannot tell
# the difference will read the pages that were never fetched as products that
# disappeared from the catalogue. See write_run_meta.
# A REMOTE service failed — the Scraping Browser refusing the connection
# (`profile_locked` is the common one: a profile allows a single live
# connection), or the Scraper API answering an error. Distinct from 1 (a
# crash in this code) and from 2 (bad usage) because it means "try again, or
# use a different profile", not "there is a bug here". Defined once, here,
# because the browser engines and scraper_api_client.py both return it and
# two definitions of the same code is exactly how a family's exit contract
# drifts.
EXIT_API_ERROR = 5

EXIT_PARTIAL = 6


# Exit code for a run that never GOT its pages: a navigation timeout, a dead
# or unauthenticated proxy, a DNS failure, or an edge answering with
# something that is not the page that was asked for.
#
# Distinct from EXIT_NO_PRODUCTS because those are opposite facts. Exit 4 is
# a statement about the CATALOGUE — "we asked, and the answer was nothing" —
# so handing it to a run that never reached the site tells a pipeline the
# listing is empty when nothing was read at all.
#
# 5 rather than a new number, and 5 rather than EXIT_PARTIAL:
#
#   * this family's contract already reserves 5 for a transport failure
#     (scraper_api_client has used it for a remote API error since it was
#     written), so this needs no new code and no per-repo table for a caller
#     driving more than one of these scrapers;
#   * EXIT_PARTIAL (6) means "some rows were gathered and the output is
#     incomplete". A run holding nothing writes no output at all, so a
#     consumer that reads the file on a 6 finds either nothing or the
#     PREVIOUS run's good data, which `save` deliberately does not
#     overwrite. Exit 5 promises no file.
#
# Deliberately NOT applied when rows WERE gathered: a timeout on page 7 of
# 10 is a partial run (exit 6, output written), which is already right. This
# decides only what a run holding nothing reports.
EXIT_FETCH_FAILED = 5


def write_run_meta(out_prefix: str, meta: dict) -> str:
    """Write a run-metadata sidecar next to the output, return its path.

    Deliberately a separate `<out>.meta.json` rather than columns on every
    row: this describes the RUN, not the product, and repeating it across
    every row would both bloat the output and change the schema every
    consumer of this project already parses.

    diff_runs.py reads it to refuse a comparison between runs that are not
    both complete, and between runs of different `mode`.
    """
    path = f"{out_prefix}.meta.json"
    # Atomic for the same reason the row files are, and one reason more:
    # this file is what a consumer branches on, so a truncated sidecar is
    # worse than none at all — it parses as far as it parses and then
    # raises, next to data that is perfectly fine.
    with _atomic(path) as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


def run_meta(status: str, stop_reason: str, pages_requested: int,
             pages_completed: int, start_url: str, final_url: str,
             products: int, pages_failed: Optional[List[int]] = None,
             mode: str = "profile", source: str = SOURCE_DEFAULT,
             extra: Optional[dict] = None) -> dict:
    """Build the metadata dict for a finished run.

    `status` is the field a consumer branches on:
      complete — every requested page was fetched, or the site's own
                 pagination genuinely ran out (nothing more existed to get)
      partial  — rows were gathered, then the run stopped early
      failed   — nothing was gathered at all

    `mode` and `source` are recorded because a consumer holding files from
    several repos in this family needs to tell them apart without knowing which
    repo wrote which. `diff_runs.py` refuses a pair whose modes differ.

    `extra` carries facts about the run that are not about any single row —
    most importantly, where the route makes it possible, how small a sample
    the run is of what the site holds.
    """
    meta = {
        "source": source,
        "mode": mode,
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        # Named "products" even though these rows are accounts, and kept that
        # way deliberately: every repo in this family writes this key, and a
        # consumer reading several of them reads one sidecar shape.
        # quora-scraper made the same call for answers. The row TYPE is
        # `mode` plus `source`, which are right beside it.
        "products": products,
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        # Merged rather than nested under a key, so a consumer reads
        # `handles_requested` at the top level beside `products`. Run fields win a
        # name collision: a caller cannot accidentally overwrite `status`.
        meta.update({k: v for k, v in extra.items() if k not in meta})
    return meta


def save(rows: Sequence[Any], out_prefix: str, fmt: str,
         allow_empty: bool = False, row_cls: Type = Profile) -> int:
    """Write JSON/CSV and return a process exit code.

    Returns 0 when rows were written, EXIT_NO_PRODUCTS when there were none.
    Callers are expected to exit with it.

    On zero rows, nothing is written at all unless `allow_empty`. Two reasons,
    and a live run demonstrated both. A page-load timeout produced
    `Saved 0 rows -> out.json` and exit 0: a two-byte `[]` that a
    consuming pipeline reads as a successful run with no stock. Worse, if the
    file already held a good result from an earlier run, that result is now
    gone — the failure destroyed the last known good data. So an empty result
    leaves the previous file intact and says why.

    `allow_empty=True` is for the legitimate case: a filter that genuinely
    matches nothing, where an empty file is the answer.
    """
    if not rows and not allow_empty:
        print(f"[!] 0 rows — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected answer.")
        return EXIT_NO_PRODUCTS

    if fmt in ("json", "both"):
        write_json(rows, f"{out_prefix}.json")
        print(f"[+] Saved {len(rows)} rows -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        write_csv(rows, f"{out_prefix}.csv", row_cls=row_cls)
        print(f"[+] Saved {len(rows)} rows -> {out_prefix}.csv")
    return 0 if rows else EXIT_NO_PRODUCTS


# Stop reasons that mean the run saw everything there was to see. Anything
# else ended the page loop early, so the result is only a partial view.
#
# "no_new_products" belongs here and "pagination_exhausted" is kept for the
# engines that still stop on a missing next-link: the first is a property of
# the DATA (a page contributed nothing not already seen, so the listing is
# over), while the second is a property of a CSS SELECTOR and is therefore
# the weaker signal — a renamed attribute looks identical to a short
# catalogue.
#
# On Snapchat there is no listing to walk: one profile page per account,
# and the Spotlight/highlight cursors the page carries are followed by the
# site's own protobuf API, which this repo does not implement. So a run is
# a list of accounts, and "complete" means every account named was
# answered.

COMPLETE_STOP_REASONS = ("completed", "pagination_exhausted", "no_new_products",
                         "page_cap_reached", "page_echo_mismatch",
                         "single_page_route", "user_unavailable")


def finish_run(rows: Sequence[Any], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[int]] = None,
               mode: str = "profile", source: str = SOURCE_DEFAULT,
               extra: Optional[dict] = None) -> int:
    """Write output + the run-metadata sidecar; return the exit code.

    Shared by all three browser engines so the status/exit-code mapping
    cannot drift between them.

    The metadata sidecar is written ONLY when the row file was written.
    Otherwise a failed run would leave a "status": "failed" sidecar next to
    the previous run's still-intact good output (which `save` deliberately
    does not overwrite) — the two files would contradict each other, and
    diff_runs.py would refuse to compare data that is in fact fine.
    """
    # Completeness is decided by the reason AND by the evidence, and the
    # second half is the fix. `stop_reason` is a NAMED LIST, and a named
    # list cannot cover a failure that was recorded somewhere else — which
    # is exactly what happened: a run whose top-level pages all arrived
    # stops for `page_cap_reached`, a COMPLETE reason, while
    # `pages_failed` names pages that did not. Measured before
    # the fix: `finish_run(rows, stop_reason="page_cap_reached",
    # pages_failed=[3, 7])` returned exit 0 with `status: complete` and a
    # two-entry `pages_failed` in the same sidecar — a file that
    # contradicts itself, and a pipeline that branches on `status` reading
    # a short run as a whole one.
    #
    # Same shape as the exit-code unification this file already carries: a
    # rule keyed on a list of names has a hole for every name nobody added
    # to it, so key on the thing that is actually true instead. If a page
    # failed, the run is not complete, whatever it stopped for.
    complete = stop_reason in COMPLETE_STOP_REASONS and not pages_failed
    row_cls = ROW_CLASS_BY_MODE.get(mode, Profile)
    rc = save(rows, out_prefix, fmt, allow_empty=allow_empty, row_cls=row_cls)
    wrote_output = bool(rows) or allow_empty

    if wrote_output:
        status = "complete" if (rows and complete) else (
            "partial" if rows else "failed")
        write_run_meta(out_prefix, run_meta(
            status=status, stop_reason=stop_reason,
            pages_requested=pages_requested, pages_completed=pages_completed,
            pages_failed=pages_failed, mode=mode, source=source,
            start_url=start_url, final_url=final_url, products=len(rows),
            extra=extra))

    if not rows:
        # Nothing gathered at all, and WHY decides the code. The three
        # outcomes are different facts and a pipeline branches on them
        # (blocked is not empty is not "never reached"):
        #
        #   blocked            something stood between the run and the content
        #   did not complete   we never got the pages — a dead proxy, a load
        #                      timeout, an edge serving something else
        #   completed          we asked, and the answer was nothing
        #
        # Keyed on `not complete` rather than on a list of stop reasons, on
        # purpose: a list cannot cover a reason nobody has added to it yet,
        # so a new one falls silently through to "the catalogue is empty" —
        # which is the defect this branch exists to prevent.
        if blocked:
            return EXIT_BLOCKED
        if not complete:
            print(f"[!] Nothing was gathered and the run did not finish "
                  f"({stop_reason}) — exit {EXIT_FETCH_FAILED}, NOT an empty "
                  f"result (exit {EXIT_NO_PRODUCTS}). Nothing can be "
                  f"concluded about the catalogue from this run.")
            return EXIT_FETCH_FAILED
        return rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view — see "
              f"{out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
