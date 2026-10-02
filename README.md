# Saffitt collector

Reads the dance and ballet schedules of every website in the Saffitt `sources` table, puts the performances into the staging table, then publishes them with a duplicate check in between.

```
sources ──> collect (12 jobs in parallel) ──> import_staging_performances ──> publish (review.py) ──> performances
            headless browser + Claude                                          matching, duplicate check,   + performance_sources
            reads pages                                                        Claude reviews unsure cases    (one row per site)
```

## One performance, many websites

The same show is often listed by the host theatre, a festival and the touring company. It is published once, and every
website that lists it is kept as a **listing** in `performance_sources`. What the site shows is chosen by precedence:

1. the host venue's own website, when it is a tier 0 site and the show is on one of its own stages (`source_venues`)
2. a festival
3. the company's own website
4. a venue below tier 0 on its own stage
5. any other site

Venue, date, time and ticket link come from the best listing that gives them; a listing with a time beats one without.
Shows outside Europe by tier 0 companies, choreographers or venues are kept.

## Publishing and the duplicate check (`review.py`, job **publish**)

Runs after the collect jobs (or alone: mode **review**).
1. **Publish.** Each staging row is matched against what is on the site: same venue and start; same show under a
   slightly different title; one site without a venue; and the same show in the same city and day under another venue
   name (a festival calling the Old Stage "Royal Danish Theatre"). A clear match becomes one more listing of that show.
   A possible match is **held** (`match_review`) instead of being published as a second row. A copy of a date without its
   venue that the same site also gives complete is dropped.
2. **Look for pairs** among published shows that may be the same performance (`find_duplicate_candidates`).
3. **Claude reviews** every held row, pair and disagreement between trusted sites (`performance_conflicts`), with what each
   site says and the text of the listing pages. It answers same / different, which row to keep, or which listing is wrong.
4. **Only confident answers are applied** (confidence 0.85 or more; variable `REVIEW_MIN_CONFIDENCE`). A merge moves every
   listing to the kept row and snapshots the removed one in `duplicate_review`. Everything else stays open with Claude's
   reasoning, for a person: `select * from review_queue();`

## What it does for each website

1. Opens the schedule page in a real browser (Chromium), refuses cookies, clicks "load more", scrolls.
2. Claude reads the page and returns every upcoming dance performance: one row per date and time, one row per piece for mixed bills, English title plus original title, venue, choreographer, company, genre tags, booking link, image and a short description in its own words.
3. If dates or times are only on production pages, it opens those too (up to 40 per site). It follows pagination and next-month links (up to 8 pages).
4. If a source has no schedule page yet, it starts from the home page and finds it, then saves it in `sources.schedule_url`.
5. Writes the rows to `import_staging_performances` and updates the source: status (ok, partial, waiting, no_dance, failed), a short note, and when it is due again (tier 0, the core, every 7 days; tier 1 every 14; tiers 2 and 3 every 30; a festival with no programme out yet every 30).

The collect jobs never write to `performances`, `theaters`, `companies` or `choreographers`. Those change only in the
publish step, through database functions: `promote_staging_performances()` adds rows and listings, `apply_review()` applies
a review verdict. A row is removed only when the review finds it is a duplicate, and the removed row is kept as a snapshot.

It respects each site's robots.txt and waits 2 seconds between page loads.

## Finding every date (tiers 0 and 1)

The core sites get a more thorough treatment. Each lens looks for dates the schedule page does not show directly:

- **Calendars by month**: when the next-month link has a pattern (`?month=2026-11`, `/2026/11/`, `/202611`,
  `?luna=11&anul=2026`), the following months are opened directly, three at a time: reading goes on until three months
  past the last month that has shows (up to 12 months ahead; months do not count against the 14 schedule pages).
  A remembered month page that has passed (`/calendar/2026-10/` in December) is read as the current month instead.
  When the later months are empty, the report says "published so far up to ..." instead of a CHECK.
- **Hidden slides and tabs**: a season shown a few months at a time in a carousel, or month tabs, is read in full
  (the hidden panels' text is added to the page). "Load more" buttons are clicked in about 20 languages.
- **Background data**: booking widgets and JavaScript calendars load their dates as data. That data is kept and read with
  the page; a feed asked for one week (`from=...&to=...`) is asked again for the whole season.
- **Calendar files**: "add to calendar" (.ics) files are read directly, no model needed.
- **Buttons**: in-page "Dates", "Sessions", "Termine", "Séances"... buttons and tabs are clicked; collapsed sections opened.
- **Sitemap**: the site's sitemap.xml gives its current production pages; the dance ones are opened even if no calendar shows them.
  Event addresses are recognised in Western and Eastern European languages, in Latin or Cyrillic or Greek script
  (afisha, spektakl, repertuar, predstav, műsor, ohjelmisto, renginiai, афиша, спектакль...).
- **Several dance categories**: Claude lists every other dance-related category or filter (ballet, dance, guest performances,
  family dance). One that produces shows is remembered in `sources.extra_schedule_urls` and read every time.
- **Date hunt**: when a production shows only a range ("2 Dec to 2 Jan") or fewer dates than it announces ("12 performances"),
  the collector opens the production's own dates or booking page, and for a touring stop the host venue's own page.
  Third-party ticket sellers are never used. Runs still missing dates are saved in `production_runs` with where it looked,
  shown on the site as a date range, and looked at again at every visit (weekly once the opening is within 60 days).
- **Never invented**: dates are only taken as written. A weekly pattern stated on the page ("Thu and Fri 20:30") is the only
  case where dates are worked out, and those are marked as such.
- **Touring**: every date keeps its own venue, city, country and time zone, including small "on tour" / "Gastspiel" marks.
  A company's or festival's home city is never filled in for a date that names another place.
- **Coverage checks** in the report: a schedule that showed only one month, or dates that stop well before the season ends.

Checks inside Supabase (no cost): `company_venue_mismatches` (a company lists a show its venue does not, or the reverse),
`plausibility_flags` (odd local start times, a company in two cities at once, shows without venue),
`suggested_new_sources` (venues and companies we keep meeting but do not collect yet), `upcoming_runs_without_dates`.

## One-time setup (about 10 minutes)

1. **Create a private GitHub repository** and upload the files (all of them sit at the top level). The schedule file `.github/workflows/collect.yml` is created with Add file > Create new file, pasting the content of collect.yml.
2. **Add three secrets** in the repository: Settings > Secrets and variables > Actions > New repository secret:

   | Name | Value |
   |---|---|
   | `SUPABASE_URL` | `https://evzpexztlzymamakoxrg.supabase.co` |
   | `SUPABASE_SERVICE_KEY` | Supabase > Project Settings > API Keys > the **service_role** key (or a new **secret** key, `sb_secret_...`) |
   | `ANTHROPIC_API_KEY` | from console.anthropic.com > API Keys (add credit under Billing) |

   Optional: a repository **variable** `MODEL` to change the Claude model (default `claude-sonnet-5`).

## Making sure it works before anything reaches the site

1. **Preflight (free, about 15 minutes).** Actions > Collect performances > Run workflow > mode **preflight**.
   It opens every source exactly as the collector would, with no Claude, and reports per site (also saved in the Supabase table `source_checks`, so Claude can review it there):
   OK, NO DATES (home page; the collector will find the schedule), EMPTY, BLOCKED (and by what: Cloudflare, captcha, 403...),
   ERROR, or ROBOTS, plus what happened to the cookie banner. Download **all-results** for a spreadsheet of every site and a screenshot of each page.
2. **Collect with `dry` ticked** first: it reads and reports, writes nothing, and the publish job is skipped.
3. **Collect.** Run workflow > mode **collect**. The collect jobs write to the staging table; the publish job then
   publishes with the duplicate check. To only preview publishing: `select * from promote_staging_performances(dry_run => true);`

After that it runs by itself on Monday, Wednesday and Friday at 01:00 UTC. Each run only reads the sources that are due.

## What protects the data

- **Blocked sites** (Cloudflare, captcha, Akamai, 403) are recognised and marked `blocked`. No Claude tokens are spent on them and nothing is written for them.
  "Checking your browser" pages that let a normal browser through after a few seconds are waited out.
  It never solves captchas, fakes a human, or hides behind other IP addresses.
- **Cookie banners**: it refuses them (20+ consent tools recognised, also inside iframes, button wording in 14 languages).
  It accepts only when the site shows nothing until you do. Anything left covering the page is removed from view.
- **Empty or thin pages** get a second, longer wait for late JavaScript. Pages with nothing on them are not sent to Claude.
- **Suspiciously few results**: if a site gives less than half of what Saffitt already has for it (what it lists, what plays on its own stages, its company's shows: view `source_coverage`), the report flags it (CHECK) and nothing is removed.
- **Copies inside one reading**: a date given once with its venue and once without, or twice with times exactly 1 or 2 hours apart (a UTC copy from the page's background data), is kept once: with its venue, and at the time written in the page text.
- **Unchanged pages** are not sent to Claude again (page cache for up to 28 days), so weekly runs cost a fraction of the first.
  "Unchanged" ignores ticket noise: sold out, few tickets left, seat counts, "today/tomorrow" labels, and lines that only moved around.
- **Production pages already read are not opened again** unless the next show is less than 4 weeks away or the reading is over a month old. New productions are always read.
- **Batch API** (half price) for every page: first every site's schedule page in one bundle, then the further schedule
  pages (other categories, next pages, months) and the production pages in small bundles sent while browsing goes on;
  an answer that points to more pages opens them in the next bundle. **Each site is saved as soon as its own pages are
  answered**, so a job that is stopped part way keeps every site it finished.
- **Nothing paid for is thrown away**: bundles still running when the job ends are not cancelled. Their pages are noted in
  `collector_page_cache` (role `pending|...`), the site is looked at again at the next run, and that run first collects the
  answers (Anthropic keeps them 29 days), so an unchanged page costs nothing the second time. A site whose very first page
  was still with Claude is left untouched and due.
- **Finding the dance programme**: when a homepage shows no dance but links to the dance programme, dance filter or calendar,
  that page is read and becomes the site's schedule page. Programme overview pages open their show pages (one level down).
- **Even split**: sites are dealt to the 12 jobs like cards, so every job gets the same number of sites from each tier.
- **Safety limits**: every job finishes within 320 minutes (GitHub stops jobs at 355). A page that freezes the browser for
  5 minutes is abandoned and the browser restarted, so one bad site cannot stall a job.
- **Credit runs out**: the run stops sending pages, saves everything found so far, and says so at the top of the report.
  Sources not finished stay due, so the next run picks them up. A run started with no credit stops at once with a clear message.
- **Tiers 2 and 3** read at most 15 production pages per site (tiers 0 and 1: 40).
- **Time cap**: max 25 minutes per site, 40 production pages, 8 listing pages (14 for tiers 0-1), 10 date-hunt pages. The rest is picked up next run.
- **PDF schedules** are read too.

## If some sites block GitHub's servers

Some sites refuse visitors from data centres but not from homes. Those can be retried automatically from your own computer:
1. Repository > Settings > Actions > Runners > **New self-hosted runner**, and follow the steps for your Mac/PC (needs Python 3).
2. Settings > Secrets and variables > Actions > Variables > add `HOME_RUNNER` = `true`.

After each weekly run, the `blocked` sites are then retried from your computer whenever it is on. What it finds is published by the next run.

## Reading the results

- Each run shows a summary table per shard (source, status, performances, pages, notes) and the Claude tokens used. The same report is saved as a downloadable artifact.
- In Supabase, `sources.status` and `sources.notes` show how each site went last time; the admin Sources page shows the same.
- The publish job's summary lists what was published and what Claude decided; the full report (each case, verdict,
  confidence and reason) is the **review-report** artifact.
- What still waits for a person: `select * from review_queue();`. To answer one yourself:
  `select apply_review('pair', 123, 'different');`, `select apply_review('held', 8685, 'same', '<performance id>');`,
  `select apply_review('conflict', 4, 'ignore_listings', null, array[5678]);`

## Running by hand

Actions > Collect performances > Run workflow, with optional inputs:
- `priority`: only 0 (core), 1, 2 or 3
- `name`: only sources whose name contains this text (to retry one site)
- `mode`: preflight (free check) or collect
- `limit`: sources per shard (12 shards run in parallel)
- `dry`: read and report only

Locally: `pip install -r requirements.txt && python -m playwright install chromium`, set the three environment variables, then `python run.py --name "Sadler" --dry`.

## Limits worth knowing

- Sites that need a login come back as `failed`; sites that refuse bots as `blocked`. The note and screenshot say why.
- GitHub gives private repositories 2,000 free minutes a month; a full first run of all sources can use most of that, and more is billed per minute. A public repository has unlimited minutes (your keys stay secret either way).
- A site that changes its layout is handled automatically in most cases, because Claude reads the page rather than fixed HTML positions.
- Claude usage grows with the number of pages read. The run summary shows tokens per run so you can track cost.
