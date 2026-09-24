# Contributing

Bug reports, site-change reports and pull requests are all welcome. This file
covers the few things specific to a scraper, which are not the usual ones.

## Before you open anything

Run the offline suite. It needs no network, no browser and no API key:

```bash
pip install -r requirements.txt
python3 smoke_test.py
```

It prints its own check count and lists any group it skipped because an engine
library is absent.

**The suite must pass with no engine installed at all.** CI installs only the
core requirements, so any import of `playwright_scraper`, `puppeteer_scraper`
or `selenium_scraper` in a check sits inside `try/except ImportError` with the
skip recorded. If the suite fails on a clean clone, that is itself the bug —
say so.

## Never commit a credential

`.env` and every `.env.*` variant except `.env.example` are in `.gitignore`.
Keep them there.

The engines mask `user:pass@` in their own log lines, but three things are
**not** masked: raw page dumps (`--dump-html`), the Scraper API's `x-debug`
response header, and your shell history. Before pasting output into an issue
or a PR, replace keys, proxy passwords and full `ws://user:pass@host:9222`
endpoints with `***`.

CI fails the build if something credential-shaped is committed. That is a
backstop, not a review.

## Reporting a site change

This repo reads Snapchat profile pages — `www.snapchat.com/@username` — which
are served to a bare HTTP client from a datacentre address: no key, no proxy,
no account.

The parser reads one structured source and never the rendered DOM:

    `<script id="__NEXT_DATA__">` → `props.pageProps` (`userProfile`, `story`,
    `curatedHighlights`, `spotlightHighlights`, `spotlightStoryMetadata`)

plus the page's `ProfilePage` JSON-LD for two dates. So a site change almost
always shows up as that source moving or its shape changing, and the most
useful thing a report can carry is the source itself from a `--dump-html`
capture — there is an issue template for exactly that.

## What the checks pin, and why

Each of these cost real time when it was found, and the offline suite pins it
so a PR that undoes one fails rather than silently regressing:

- **A hidden subscriber count is published as `"0"`.** @khaby00 has a public
  profile, seven Spotlight videos and `subscriberCount: "0"`; its JSON-LD
  carries an EMPTY `interactionStatistic` rather than a counter. The row says
  `null`. A PR that writes the zero through fails a check pinned on that
  account.

- **Spotlight slots can be empty.** 14 of 25 on @kyliejenner, placeholders
  with `viewCount: "0"` and no id, at the same indices in both parallel arrays.
  They are not rows, `position` counts the rows that ARE, and the sidecar
  counts the holes.

- **A highlight snap has no id of its own.** `snapId` is `{"value": ""}` on
  every highlight snap measured; the key is `highlight_id/content_id` from the
  CDN path, and the suite asserts it is unique per row.

- **The not-found page must classify without a status.** Selenium has no HTTP
  status to give, and the first live Selenium run reported a PARTIAL run for a
  username that simply does not exist. The page's own `pageType: "NOT_FOUND"`
  is what every engine reads.

- **`?locale=` changes the chrome, not the data.** en-US, fi-FI and ja-JP
  returned byte-identical rows; the suite pins it on a Japanese capture.

Before adding a challenge marker, count it on a page you **know** was served.
Snapchat's served pages carry zero occurrences of even the word `captcha`, but
a page fetched over the Scraping Browser carries twenty — injected by its own
extension. A marker that matches every page is worse than no marker.

## Before a release

```bash
python3 smoke_test.py
python3 .github/ci_checks.py --history-check
```

The second applies the credential rules CI enforces to **every blob that has
ever existed**, not just the working tree. A commit on top cannot reach what a
published tag already holds.

The canary is **not** gated on a secret: a profile page needs none, so it runs a real scrape daily from a bare GitHub runner and is expected GREEN. If Snapchat ever puts the page behind a challenge, the badge goes red the next morning.

## Pull requests

Add a check for the behaviour you are changing. `smoke_test.py` is a single
file of plain functions; copy the nearest existing check and edit it. Keep the
three engines identical above their driver layer — a check compares their
public surfaces and flag sets in both directions.
