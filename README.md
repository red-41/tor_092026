# Saffitt collector

Reads the dance and ballet schedules of every website in the Saffitt `sources` table and puts the performances into the staging table. A nightly job inside Supabase then moves them onto the site.

```
sources table ──> GitHub Actions (weekly, 12 in parallel) ──> import_staging_performances ──> promote_staging_performances() ──> performances
                   headless browser + Claude reads pages                                   (nightly, inside Supabase)
```

## What it does for each website

1. Opens the schedule page in a real browser (Chromium), refuses cookies, clicks "load more", scrolls.
2. Claude reads the page and returns every upcoming dance performance: one row per date and time, one row per piece for mixed bills, English title plus original title, venue, choreographer, company, genre tags, booking link, image and a short description in its own words.
3. If dates or times are only on production pages, it opens those too (up to 40 per site). It follows pagination and next-month links (up to 8 pages).
4. If a source has no schedule page yet, it starts from the home page and finds it, then saves it in `sources.schedule_url`.
5. Writes the rows to `import_staging_performances` and updates the source: status (ok, partial, waiting, no_dance, failed), a short note, and when it is due again (top priority every 7 days, other venues every 14, festivals every 30).

It never writes to `performances`, `theaters`, `companies` or `choreographers` directly. Those change only when `promote_staging_performances()` runs, which updates existing rows in place and never deletes.

It respects each site's robots.txt and waits 2 seconds between page loads.

## One-time setup (about 10 minutes)

1. **Create a private GitHub repository** and upload everything in this folder (including the hidden `.github` folder).
2. **Add three secrets** in the repository: Settings > Secrets and variables > Actions > New repository secret:

   | Name | Value |
   |---|---|
   | `SUPABASE_URL` | `https://evzpexztlzymamakoxrg.supabase.co` |
   | `SUPABASE_SERVICE_KEY` | Supabase > Project Settings > API Keys > the **service_role** key (or a new **secret** key, `sb_secret_...`) |
   | `ANTHROPIC_API_KEY` | from console.anthropic.com > API Keys (add credit under Billing) |

   Optional: a repository **variable** `MODEL` to change the Claude model (default `claude-sonnet-5`).

## Making sure it works before anything reaches the site

1. **Preflight (free, about 15 minutes).** Actions > Collect performances > Run workflow > mode **preflight**.
   It opens every source exactly as the collector would, with no Claude and no database writes, and reports per site:
   OK, NO DATES (home page; the collector will find the schedule), EMPTY, BLOCKED (and by what: Cloudflare, captcha, 403...),
   ERROR, or ROBOTS, plus what happened to the cookie banner. Download **all-results** for a spreadsheet of every site and a screenshot of each page.
2. **Collect.** Run workflow > mode **collect**. This writes only to the staging table, never to the live site.
3. **Check the staging rows** (or ask Claude to): `select * from promote_staging_performances(dry_run => true);`
4. **Then switch on the nightly move-in**, once, in the Supabase SQL Editor:
   ```sql
   create extension if not exists pg_cron;
   select cron.schedule('promote-staging-nightly', '0 3 * * *',
     $$select count(*) from promote_staging_performances()$$);
   ```

After that it runs by itself every Monday at 01:00 UTC. Each run only reads the sources that are due.

## What protects the data

- **Blocked sites** (Cloudflare, captcha, Akamai, 403) are recognised and marked `blocked`. No Claude tokens are spent on them and nothing is written for them.
  "Checking your browser" pages that let a normal browser through after a few seconds are waited out.
  It never solves captchas, fakes a human, or hides behind other IP addresses.
- **Cookie banners**: it refuses them (20+ consent tools recognised, also inside iframes, button wording in 14 languages).
  It accepts only when the site shows nothing until you do. Anything left covering the page is removed from view.
- **Empty or thin pages** get a second, longer wait for late JavaScript. Pages with nothing on them are not sent to Claude.
- **Suspiciously few results**: if a site gives less than half of what is already on Saffitt from that source, the report flags it (CHECK) and nothing is removed. Promotion never deletes.
- **Unchanged pages** are not sent to Claude again (page cache for up to 28 days), so weekly runs cost a fraction of the first.
- **Time cap**: max 25 minutes per site, 40 production pages, 8 listing pages. The rest is picked up next run.
- **PDF schedules** are read too.

## If some sites block GitHub's servers

Some sites refuse visitors from data centres but not from homes. Those can be retried automatically from your own computer:
1. Repository > Settings > Actions > Runners > **New self-hosted runner**, and follow the steps for your Mac/PC (needs Python 3).
2. Settings > Secrets and variables > Actions > Variables > add `HOME_RUNNER` = `true`.

After each weekly run, the `blocked` sites are then retried from your computer whenever it is on.

## Reading the results

- Each run shows a summary table per shard (source, status, performances, pages, notes) and the Claude tokens used. The same report is saved as a downloadable artifact.
- In Supabase, `sources.status` and `sources.notes` show how each site went last time; the admin Sources page shows the same.
- Before the nightly move-in you can preview what it would do:
  ```sql
  select * from promote_staging_performances(dry_run => true);
  ```

## Running by hand

Actions > Collect performances > Run workflow, with optional inputs:
- `priority`: only 1, 2 or 3
- `name`: only sources whose name contains this text (to retry one site)
- `mode`: preflight (free check) or collect
- `limit`: sources per shard (12 shards run in parallel)
- `dry`: read and report only

Locally: `pip install -r requirements.txt && python -m playwright install chromium`, set the three environment variables, then `python -m collector.run --name "Sadler" --dry`.

## Limits worth knowing

- Sites that need a login come back as `failed`; sites that refuse bots as `blocked`. The note and screenshot say why.
- GitHub gives private repositories 2,000 free minutes a month; a full first run of all sources can use most of that, and more is billed per minute. A public repository has unlimited minutes (your keys stay secret either way).
- A site that changes its layout is handled automatically in most cases, because Claude reads the page rather than fixed HTML positions.
- Claude usage grows with the number of pages read. The run summary shows tokens per run so you can track cost.
