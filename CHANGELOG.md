# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/) as
closely as a CLI toolkit can. A patch release means **fixes** — it does not
promise that every flag's default is frozen, and where a default does change
in one, the note leads with it.

## [0.2.0] — 2026-10-05

Changes from a third-party audit, each checked against a live run before it
was accepted. Two columns are added to the profile row, which is why this
is a minor release; no column was removed or renamed.

### Added

- **`has_more_spotlight` and `has_more_highlights` on the profile row**,
  from the page's own cursors. The page lists about 25 Spotlight videos
  and a slice of highlights; these say when the account has more, on the
  row itself, so a row read without its sidecar still says its counts are
  a slice.
- **`payload_keys_missing` in the sidecar.** The parser now checks that
  every `__NEXT_DATA__` key it reads is present, and names the ones that
  are not, per account. A page that renders but has lost a field used to
  produce silent nulls. Zero false positives on 20 raw captures and a live
  page; the daily canary fails on a non-empty value.
- **`transport` records what actually fetched the pages** (`http`,
  `browser`, `cdp`), with `transport_requested` beside it. It used to
  record the flag, so the default run said `auto` and never which one.
- **CSV formula neutralisation**, lifted from rakuten-scraper: a string cell
  beginning `=`, `+`, `-`, `@` or a control character is prefixed with `'`
  in CSV only, and counted in `csv_cells_escaped`. It does not fire on
  today's data (0 of 10,554 string cells, measured); it is here because
  the text is the account owner's.
- **A check that the three engines' shared code is identical**, so the
  three copies cannot drift apart unnoticed.
- CI tests the newest end of the supported range on Python 3.14 (was 3.12).

### Fixed

- **Output files were created owner-only (0600).** The atomic writer's
  temporary file is 0600 and the rename kept it: nine of nine files on a
  live run under umask 022. New files now get the umask's mode (0644
  there). A file that already exists keeps its mode, so outputs written by
  0.1.0 stay 0600 until you delete them once — the writer cannot tell a
  mode someone chose from one the bug left.

## [0.1.0] — 2026-09-24

First release.

### Added

- **Three modes over one request per account.** `--mode profile` (one row
  per account: subscribers, bio, category, website, ids, what the page
  carries), `--mode spotlight` (one row per Spotlight video, with view,
  share, comment, boost and recommend counts), `--mode story` (one row per
  snap in the live story and every curated highlight). All three read the
  same server-rendered `__NEXT_DATA__`, so they cannot disagree about an
  account.
- **Plain HTTPS by default, a browser as fallback.** Measured 2026-09-24
  from a datacentre address: every profile page served to plain curl, 30
  of 30 in a row with no delay; 1.29 s over HTTP against 3.14 s through a
  browser for three accounts, identical rows. `--transport auto` starts a
  browser only if the site refuses.
- **Playwright, Selenium and Puppeteer engines, plus the 2Captcha Scraper
  API client**, all run live on 2026-09-24 and producing identical rows.
  The Scraper API cost $0.0005 per page.
- **The traps this site sets, each handled and pinned by a check on a real
  capture:** a hidden subscriber count published as `"0"` (written as
  `null`, told apart by the page's own JSON-LD); empty Spotlight slots
  carrying `viewCount: "0"` (not rows; counted in the sidecar);
  highlight snaps with no id of their own (keyed on the CDN content id);
  an ordinary account served as a username and nothing else (a row with
  `public_profile: false`, not a failure); Snapchat's own boilerplate
  titles and descriptions (dropped); and the not-found page, which must
  classify the same with no HTTP status (Selenium has none).
- **A sidecar that says when the page is not the whole account** —
  `handles_with_more_than_page` — because a profile lists about 25
  Spotlight videos and this repo does not follow the cursor beyond them.
- **`diff_runs.py` with tracked columns per mode.**
- **A daily canary with no secrets**, green from a bare GitHub runner on its
  first dispatch.

### Not implemented

- Discover, Lenses, Snap Map, topic pages, Spotlight comments, and the
  cursor pages beyond a profile's first ~25 Spotlight videos.

### Fixed in the core this repo was built from

Found while porting the family core, and fixed here:

- **The Scraper API client recorded every run as `mode: "video"`** and
  carried a sibling's unused `ytInitialData` reader and `/player` column
  list. It now records the mode actually run, which is what lets
  `diff_runs.py` compare a Scraper API run with an engine run.
- **The credential scan's site exemption is gone.** The core forgave
  32-hex strings inside its previous site's CDN URLs; nothing here needs
  that, the fixtures carry none, and the strictest rule now covers the
  largest files.
