#!/usr/bin/env python3
"""make_fixtures.py — build `fixtures_generated.json` from real captures.

Why fixtures are TRIMMED rather than whole pages
================================================
A Snapchat profile page is 120 KB to 1.8 MB, and most of it is not under
test. Counted on the @nasa capture (672 KB):

    encodedSpotlightComments   159 KB   other people's comments, with their
                                        user ids — personal data, not read
    JSON-LD VideoObjects       133 KB   descriptions the parser does not read
    messages                    20 KB   the page's translation table
    serverSideConfigs          0.3 KB   an A/B config cache key (32 hex) and
                                        a per-visit `webClientId`

None of it is a credential, and the session-shaped part was anonymous and
per-visit. It still does not belong in a public repo, and CLAUDE.md §10 is
explicit that the checks need the STRUCTURE of a thing, not the session
that fetched it or other people's words. So a fixture keeps only the
`__NEXT_DATA__` keys the parser reads, only the fields inside them the
parser reads, and only the ProfilePage JSON-LD block. The rest is absent as
a consequence of keeping what is under test, rather than as a redaction
someone has to remember — the version of scrubbing that cannot rot.

What is kept is a public account's own catalogue entry: its name, bio,
counts, media URLs and Spotlight titles. Those are what Snapchat shows any
logged-out visitor, and the checks test real values.

Verify a trimmed fixture parses identically to its untrimmed original
=====================================================================
`--verify` re-parses both in ALL THREE modes and compares every row field
by field, plus the page state. CLAUDE.md §15 asks for this before
committing any trimmed fixture, and it is one command rather than a
promise — which matters more here than in most repos, because this trim
drops FIELDS inside the objects under test, not just whole objects beside
them.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from typing import Any, Dict, Optional

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from output_writer import ROW_CLASS_BY_MODE                 # noqa: E402
from product_parser import (MODES, detect_page_state,       # noqa: E402
                            parse_page)
from snapchat_payload import PayloadError, next_data, profile_ld  # noqa: E402

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                   "fixtures_generated.json")

# The captures these fixtures are cut from, each here for a reason no other
# covers (CLAUDE.md §15: one dump teaches you one shape).
#   filename -> (fixture name, HTTP status the capture came with, why)
WANTED = {
    "prof_nasa.html": ("nasa", 200,
                       "a public ORGANISATION: JSON-LD present, 10 highlights, "
                       "9 highlight snaps with no media URL, a highlights cursor"),
    "prof_mrbeast.html": ("mrbeast", 200,
                          "a public PERSON; every Spotlight slot filled; "
                          "JSON-LD lists 3 videos against the payload's 4"),
    "prof_kyliejenner.html": ("kyliejenner", 200,
                              "a LIVE story (16 snaps); 14 of 25 Spotlight "
                              "slots empty; NO JSON-LD at all"),
    "prof_khaby00.html": ("khaby00", 200,
                          "subscriberCount 0 with an EMPTY JSON-LD "
                          "interactionStatistic — hidden, not zero"),
    "prof_espn.html": ("espn", 200,
                       "an ordinary account: userInfo only, no public profile"),
    "prof_not_found.html": ("not_found", 404,
                            "no such account: HTTP 404, pageType NOT_FOUND"),
    "prof_nasa_ja.html": ("nasa_ja", 200,
                          "?locale=ja-JP — same data, localised chrome"),
}

# What the parser reads, and therefore all a fixture keeps. Each list is
# checked by `--verify`: drop a key the parser needs and the rows differ.
_PROPS_KEPT = ("userProfile", "story", "curatedHighlights",
               "spotlightHighlights", "spotlightStoryMetadata", "lenses",
               "spotlightHighlightsCursor", "curatedHighlightsCursor",
               "viewerInfo", "pageMetadata", "locale")
_SNAP_KEPT = ("snapIndex", "snapId", "snapMediaType", "timestampInSec")
_SNAP_URLS_KEPT = ("mediaUrl", "mediaPreviewUrl")
_COLLECTION_KEPT = ("storyId", "storyTitle", "highlightId", "thumbnailUrl")
_META_KEPT = ("videoMetadata", "hashtags", "engagementStats", "description",
              "llmTitle", "llmDescription")
_CARD_KEPT = ("contextType", "title", "subtitle")


def _pick(d: Any, keys) -> Any:
    if not isinstance(d, dict):
        return d
    return {k: d[k] for k in keys if k in d}


def _snap(s: Any) -> Any:
    if not isinstance(s, dict):
        return s
    out = _pick(s, _SNAP_KEPT)
    out["snapUrls"] = _pick(s.get("snapUrls") or {}, _SNAP_URLS_KEPT)
    return out


def _collection(c: Any) -> Any:
    if not isinstance(c, dict):
        return c
    out = _pick(c, _COLLECTION_KEPT)
    out["snapList"] = [_snap(s) for s in c.get("snapList") or []]
    return out


def _meta(m: Any) -> Any:
    if not isinstance(m, dict):
        return m
    out = _pick(m, _META_KEPT)
    out["contextCards"] = [_pick(c, _CARD_KEPT) for c in m.get("contextCards") or []]
    return out


def trim(html: str) -> Dict[str, Any]:
    """A whole page down to what the parser reads."""
    data = next_data(html)
    props = (data.get("props") or {}).get("pageProps") or {}
    if isinstance(props.get("pageProps"), dict):
        # The not-found page nests its props one level deeper
        # (`{"status": 2, "pageProps": {...}}`); keep its shape.
        inner = props["pageProps"]
        kept_props = {"status": props.get("status"),
                      "pageProps": _pick(inner, ("pageMetadata", "viewerInfo"))}
    else:
        kept_props = _pick(props, _PROPS_KEPT)
        if isinstance(kept_props.get("story"), dict):
            kept_props["story"] = _collection(kept_props["story"])
        for key in ("curatedHighlights", "spotlightHighlights"):
            if isinstance(kept_props.get(key), list):
                kept_props[key] = [_collection(c) for c in kept_props[key]]
        if isinstance(kept_props.get("spotlightStoryMetadata"), list):
            kept_props["spotlightStoryMetadata"] = [
                _meta(m) for m in kept_props["spotlightStoryMetadata"]]
        if isinstance(kept_props.get("lenses"), list):
            kept_props["lenses"] = [_pick(x, ("lensName",))
                                    for x in kept_props["lenses"]]
    return {
        "next_data": {"page": data.get("page"), "props": {"pageProps": kept_props}},
        "profile_ld": profile_ld(html),
    }


def as_page(payload: Dict[str, Any]) -> str:
    """A trimmed fixture back into the minimal page the parser accepts.

    Deliberately small and NOT dressed up as a real page: a fixture that
    mimicked the shell would invite someone to test asset counting against
    it, and asset counting is exactly what a trimmed fixture cannot
    honestly exercise.
    """
    parts = ['<!DOCTYPE html><html><head><script id="__NEXT_DATA__" '
             'type="application/json">'
             + json.dumps(payload["next_data"], ensure_ascii=False)
             + "</script>"]
    if payload.get("profile_ld"):
        parts.append('<script type="application/ld+json">'
                     + json.dumps(payload["profile_ld"], ensure_ascii=False)
                     + "</script>")
    parts.append("</head><body></body></html>")
    return "".join(parts)


def build(capture_dir: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {"_readme": (
        "Generated by make_fixtures.py from real captures taken 2026-09-24. "
        "Each entry is one real Snapchat profile page cut down to the "
        "__NEXT_DATA__ fields the parser reads and its ProfilePage JSON-LD. "
        "Other people's comments, the translation table and the per-visit "
        "config ids are absent rather than redacted. Run `python3 "
        "make_fixtures.py --verify` to re-check that a trimmed fixture "
        "parses identically to its untrimmed original in every mode."
    ), "profiles": {}}
    missing = []
    for filename, (name, status, why) in WANTED.items():
        path = os.path.join(capture_dir, filename)
        if not os.path.exists(path):
            missing.append(filename)
            continue
        html = open(path, encoding="utf-8", errors="replace").read()
        out["profiles"][name] = {"why": why, "status": status,
                                 "payload": trim(html)}
    if missing:
        print(f"[!] {len(missing)} capture(s) not found: {missing}",
              file=sys.stderr)
    return out


def _rows(html: str, mode: str) -> Optional[list]:
    try:
        rows, _ = parse_page(html, "https://www.snapchat.com/@x", "T",
                             ROW_CLASS_BY_MODE[mode], mode)
    except PayloadError:
        return None
    return [asdict(r) for r in rows]


def verify(capture_dir: str) -> int:
    """Trimmed vs untrimmed, every mode, field by field. Exit 1 on a diff."""
    data = json.load(open(OUT, encoding="utf-8"))
    bad = 0
    for filename, (name, status, _why) in WANTED.items():
        path = os.path.join(capture_dir, filename)
        entry = data["profiles"].get(name)
        if entry is None or not os.path.exists(path):
            print(f"  {name:12} SKIP (no capture or no fixture)")
            continue
        original = open(path, encoding="utf-8", errors="replace").read()
        trimmed = as_page(entry["payload"])
        state_o = detect_page_state(original, status, "")
        state_t = detect_page_state(trimmed, status, "")
        counts = []
        same = state_o == state_t
        for mode in MODES:
            ro, rt = _rows(original, mode), _rows(trimmed, mode)
            same = same and ro == rt
            counts.append(f"{mode} {len(ro) if ro is not None else '-'}")
        print(f"  {name:12} {'OK' if same else 'DIFFERS'}  state {state_o}, "
              f"{', '.join(counts)}")
        if not same:
            bad += 1
    return 1 if bad else 0


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--captures", default=os.path.expanduser(
                       "~/2scraper/captures/snapchat"),
                   help="Directory holding the raw page captures. Not in the "
                        "repo: captures are large and are not committed.")
    p.add_argument("--verify", action="store_true",
                   help="Re-parse each fixture against its untrimmed original "
                        "in every mode and compare, rather than rebuilding.")
    args = p.parse_args()
    if args.verify:
        sys.exit(verify(args.captures))
    data = build(args.captures)
    with open(OUT, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=1, sort_keys=True)
    size = os.path.getsize(OUT)
    print(f"[+] {len(data['profiles'])} fixture(s) -> {OUT} ({size:,} bytes)")
