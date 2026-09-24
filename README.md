# snapchat-scraper

[![release](https://img.shields.io/github/v/release/2scraper/snapchat-scraper?sort=semver)](https://github.com/2scraper/snapchat-scraper/releases)
[![tests](https://github.com/2scraper/snapchat-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/snapchat-scraper/actions/workflows/tests.yml)
[![canary](https://github.com/2scraper/snapchat-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/snapchat-scraper/actions/workflows/canary.yml)
[![python](https://img.shields.io/badge/python-3.9%20%7C%203.12-blue)](pyproject.toml)
[![licence](https://img.shields.io/badge/licence-MIT-green)](LICENSE)
[![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20pyppeteer%20%7C%20CDP-informational)](#engines-and-what-each-one-costs-you)
[![runs without an account](https://img.shields.io/badge/runs%20without-an%20account-brightgreen)](#you-do-not-need-a-key-a-proxy-or-an-account)

Scrape public Snapchat profiles — subscriber count, bio, category, website,
ids — their **Spotlight** videos with view, share, comment and boost
counts, and their **story and highlight** snaps with media URLs and
timestamps.

Three modes, one request per account. Playwright, Selenium or Puppeteer,
or no browser at all. Captcha solving, proxies and fingerprints are wired
in and, on this site, unnecessary — see below, with the measurement.

---

## You do not need a key, a proxy, or an account

A Snapchat profile page is server-rendered and served to anyone. Measured
**2026-09-24** from a bare datacentre address, with no credentials of any
kind:

| request | result |
|---|---|
| `curl https://www.snapchat.com/@nasa` (curl's own User-Agent) | HTTP 200, the whole account in `__NEXT_DATA__` |
| the same with a Chrome User-Agent | HTTP 200, same data |
| thirty requests in a row, no delay | 30 of 30 HTTP 200 |
| headless and headful Chromium | HTTP 200, identical rows |

The site is served by Google Cloud with no bot-management vendor in front
of it on any capture. So the default transport here is plain HTTPS, and a
browser is a fallback, not the engine. Three accounts end to end, median of
three runs on the same machine:

```
--transport http      1.29 s
--transport browser   3.14 s      identical rows
```

**What the paid products buy here**, stated plainly because the honest
answer is "not access": volume from many addresses, a specific exit
country, and no infrastructure of your own. The 2Captcha Scraper API path
was run live on 2026-09-24 and returned rows identical to the engines' at
**$0.0005 per page**.

### No captcha has been met

On 26 served captures the word `captcha` does not appear once. Captcha
solving through 2Captcha is wired into every engine for the day that
changes, and `--solve-captcha when-blocked` (the default) pays only for a
page that is actually gated.

---

## Quick start

```bash
git clone https://github.com/2scraper/snapchat-scraper
cd snapchat-scraper
python3 -m venv venv
./venv/bin/pip install -r requirements.txt -r requirements-playwright.txt
./venv/bin/python playwright_scraper.py --url nasa
```

That is the whole setup. No key, no `.env`, no browser download — the
default transport needs none of them. Install a browser only if you want
`--transport browser`:

```bash
./venv/bin/python -m playwright install chromium
```

`--url` takes a username, an `@username`, a profile URL
(`https://www.snapchat.com/@nasa`, `.../add/nasa`,
`story.snapchat.com/@nasa`), or a comma-separated list of any mixture:

```bash
./venv/bin/python playwright_scraper.py --url nasa,mrbeast,kyliejenner \
    --mode spotlight --concurrency 3 --format both
```

---

## Three modes, one request

| `--mode` | one row per | the columns that matter |
|---|---|---|
| `profile` (default) | account | `subscriber_count`, `bio`, `website_url`, `category`, `business_profile_id`, `public_profile`, `created_at`, counts of what the page carries |
| `spotlight` | Spotlight video on the profile | `view_count`, `share_count`, `comment_count`, `boost_count`, `recommend_count`, `title`, `description`, `hashtags`, `duration_s`, `uploaded_at`, `video_url`, `sound_title` |
| `story` | snap in the live story or a highlight | `collection` (`story` / `highlight`), `title` (the highlight's name), `media_type`, `media_url`, `preview_url`, `posted_at` |

All three are views of the **same page**, so a mode never costs an extra
request, and the modes cannot disagree about an account: the offline
suite asserts that `spotlight_count` on a profile row equals the number of
rows `--mode spotlight` emits for it.

Every row starts with the family prefix `source, scraped_at, url, sku,
title`. `sku` is a public profile's `businessProfileId` (a UUID — Snapchat
lets a user change their username, so the username is not the key), a
Spotlight's id, or a snap's id. `page` is which account of the run a row
came from and `position` its place within that account, so the pair is
unique across a run.

Measured on 2026-09-24: @nasa — 757,800 subscribers, 7 Spotlight videos,
10 highlights, 56 highlight snaps; @kyliejenner — a 16-snap live story and
645 highlight snaps; @mrbeast — a Spotlight at 3,841,923 views.

---

## Things that look like bugs, and are not

* **`subscriber_count` is `null` for some public accounts.** Snapchat
  publishes a hidden count as `"0"`. The page's own JSON-LD tells the two
  apart — a real count is a `FollowAction` counter, a hidden one is an
  empty list — and a hidden count is written as `null`, because a zero
  drags every average a consumer computes. Measured on @khaby00.
* **Every subscriber count ends in `00`.** Snapchat rounds it to the
  nearest hundred, in the payload and the JSON-LD alike. There is no exact
  figure anywhere on the page.
* **Fewer Spotlight rows than the page's 25 slots.** A slot can be an
  empty placeholder — `viewCount: "0"`, no id — and 14 of 25 were on
  @kyliejenner, stable over three refetches. They are not videos with no
  views, so they are not rows; the sidecar counts them in
  `spotlight_empty_slots`.
* **At most ~25 Spotlight videos per account.** That is what a profile
  page lists. The page carries a cursor for the rest, consumed by
  Snapchat's own protobuf API, which **this repo does not implement**. The
  sidecar names every account whose page said there was more, in
  `handles_with_more_than_page`, so "complete" is never misread as "the
  account's whole history".
* **An account with one row and nothing in it.** An ordinary account —
  not a Public Profile — is served as a username and a Snapcode and
  nothing else (@espn, measured). `--mode profile` emits a row with
  `public_profile: false`, because "this account exists" is a real answer;
  `--mode spotlight` and `--mode story` emit nothing for it, and say why.
* **A highlight snap's `sku` has a slash in it.** Snapchat gives highlight
  snaps no id of their own (`snapId` is empty on every one measured), so
  the key is `{highlight_id}/{content id from the CDN path}` — stable
  across refetches, and unique even when one snap is saved into several
  highlights. Snaps with no media URL at all fall back to
  `{highlight_id}#{index}`.
* **A Spotlight `title` reads like a headline.** It is Snapchat's own
  AI-generated title (`llmTitle`), because the video's own `name` is the
  literal `"Spotlight Snap"` unless the creator set one. Snapchat's
  AI-generated description is its own column, `generated_description`, so
  it is never mistaken for the creator's words. Snapchat's boilerplate
  description ("Another Spotlight Snap brought to you by Snapchat") is
  dropped.
* **`--locale` does not translate anything in the rows.** It moves the
  page's CHROME, not its DATA: `?locale=en-US`, `fi-FI` and `ja-JP`
  returned byte-identical account and Spotlight data. It is always sent
  because, without it, the page follows the exit's country.
* **A nonexistent username is exit 4, not exit 3.** Snapchat answers with
  its own not-found page; that is an answer, not a block, and no retry or
  exit rotation will change it. Selenium, which has no HTTP status to give,
  reads the page's own `pageType: "NOT_FOUND"` like the other engines.

---

## What this repo does not implement

Named so nobody concludes a column is broken: **Discover**, **Lenses**,
**Snap Map**, topic pages (`/explore/…`), a single Spotlight by its own
URL (pass the creator's username with `--mode spotlight` instead),
Spotlight **comments**, and the cursor pages beyond a profile's first
~25 Spotlight videos. Those with a URL of their own are refused with that
reason if you pass one, rather than fetched and parsed to nothing.

---

## Engines, and what each one costs you

All four paths produce identical rows. Verified on 2026-09-24: the same
two accounts in `--mode spotlight` through Playwright (headless and
headful), Puppeteer and Selenium, each over both transports — six runs,
one hash — and the Scraper API path identical to them for @nasa.

```bash
./venv/bin/python playwright_scraper.py --url nasa      # primary
./venv/bin/python selenium_scraper.py   --url nasa
./venv/bin/python puppeteer_scraper.py  --url nasa
./venv/bin/python scraper_api_client.py --url nasa      # 2Captcha Scraper API
```

**Install exactly one engine.** The three declare mutually unsatisfiable
pins (`pyee` <12 vs >=13 for playwright/pyppeteer; `urllib3` <2.0 vs >=2.6
for pyppeteer/selenium). They do run side by side in practice, but
`pip check` reports the conflict and pip may resolve it by downgrading
something you wanted. Use a virtualenv per engine.

Two engine limits worth knowing before you hit them:

* **Selenium cannot use an authenticated remote CDP endpoint.** Playwright
  and Puppeteer take a full `ws://user:pass@host:port`; chromedriver's
  `debuggerAddress` takes a bare `host:port` with nowhere to put a
  password.
* **Selenium's `--proxy-server` cannot authenticate at all.** Credentials
  are stripped and a warning is printed rather than letting you believe a
  `user:pass` URL is doing something.

`--fingerprint` (the 2Captcha Fingerprint API) was run live on 2026-09-24
over the browser transport and produced the same rows. It is ignored on
the HTTP transport, which cannot carry client hints and would wear half an
identity.

---

## Exit codes

| code | meaning |
|---|---|
| 0 | ok |
| 1 | crash |
| 2 | bad usage |
| 3 | blocked |
| 4 | zero rows — including "no account for any username asked" and "no Spotlight / no story on these accounts" |
| 5 | the content was never obtained (a navigation timeout, a dead proxy, a remote API error) |
| 6 | partial — some accounts fetched, some failed |

**A run that finds nothing writes nothing.** Last night's good output is
never replaced with `[]`. `--allow-empty` is the opt-out.

Every run writes `<out>.meta.json` beside its output: the status, the stop
reason, **which** accounts failed by number, `handles_unavailable`,
`handles_without_public_profile`, `handles_with_more_than_page`,
`spotlight_empty_slots`, and `viewer_countries` — the country Snapchat
says the request came from, which is how you check that a proxy's or a
Scraping Browser's `country-` segment did what you asked.

---

## Watching accounts over time

```bash
./venv/bin/python playwright_scraper.py --url nasa,mrbeast --mode spotlight \
    --out "spotlight_$(date +%F)"
./venv/bin/python diff_runs.py --old spotlight_2026-09-23.json \
    --new spotlight_2026-09-24.json
```

`diff_runs.py` keys on `sku`, tracks a different set of columns per mode,
and refuses to compare two runs of different modes or a run that was not
complete. Spotlight counters grow while a video is being watched, so two
runs an hour apart legitimately differ on almost every `spotlight` row;
in `story` mode, `removed` is ordinary, because a live story expires.

---

## Configuration

Credentials live in `.env` beside the scripts, never on a command line — a
secret in `argv` is readable by anything that can run `ps`.

```bash
cp .env.example .env
python3 env_config.py      # prints what was picked up, WITHOUT printing secrets
```

Precedence, highest first: **an explicit flag -> an exported environment
variable -> `.env` -> the default.** A value still carrying a
`{placeholder}` is treated as unset, so a copied example is never sent to
an API as if it were a key.

Variables: `TWOCAPTCHA_KEY`, `SNAPCHAT_CDP_ENDPOINT`, `SNAPCHAT_PROXY`,
`SNAPCHAT_URL`.

---

## The daily canary

`.github/workflows/canary.yml` runs real scrapes against live Snapchat
every morning **from a bare GitHub runner with no secrets** — four profiles
covering a public organisation, a public person, an ordinary account and a
username that does not exist, plus a Spotlight run — and is expected to be
green.

That is not a convenience. The central claim of this README is "you need no
key, no proxy and no account", and an ungated scheduled canary is that
sentence under test every morning. If Snapchat ever puts its profile pages
behind a challenge, the badge goes red the next day and the claim is
retested without anyone having to remember to.

---

## Contributing, and what the checks are for

```bash
python3 smoke_test.py       # the offline suite — no network, no browser needed
python3 -m pytest           # the same checks, through pytest
python3 make_fixtures.py --verify   # trimmed fixtures vs. the raw captures
```

The suite is one file of plain functions with fixtures cut from real
captures and pinned by VALUE — a column can be fully populated and
entirely wrong. It passes with no engine library installed at all, and CI
fails if an engine group reports an *unexpected* skip.

It also holds the material the 2Captcha Scraping Browser's auto-solve
extension injects into every page it loads — 16 `chrome-extension://`
tags, `cf-turnstile` among them — spliced into every served fixture, so
that no challenge marker in this repo ever reports a good page fetched
over a paid connection as blocked.

See [CONTRIBUTING.md](CONTRIBUTING.md) for what each check pins and why.

---

## Licence

MIT. See `LICENSE`.

Captcha solving, the Scraping Browser API, proxies and fingerprints are
four separately-billed [2Captcha](https://2captcha.com) products behind one
key. This repo needs none of them for its own pages, and says so above with
the measurement.
