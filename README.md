# Saffitt collector

Reads the dance and ballet schedules of every website in the Saffitt `sources` table and puts the performances into the staging table. A nightly job inside Supabase then moves them onto the site.

```
sources table ──> GitHub Actions (weekly, 6 in parallel) ──> import_staging_performances ──> promote_staging_performances() ──> performances
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
   | `SUPABASE_SERVICE_KEY` | Supabase > Project Settings > API keys > the **service_role** (secret) key |
   | `ANTHROPIC_API_KEY` | from console.anthropic.com > API keys |

   Optional: a repository **variable** `MODEL` to change the Claude model (default `claude-sonnet-5`).
3. **Switch on the nightly move-in** once, in the Supabase SQL Editor:
   ```sql
   create extension if not exists pg_cron;
   select cron.schedule('promote-staging-nightly', '0 3 * * *',
     $$select count(*) from promote_staging_performances()$$);
   ```
4. **First run**: Actions > Collect performances > Run workflow. Tick "Dry run" the first time to see the report without writing anything, then run it again without.

After that it runs by itself every Monday at 01:00 UTC. Each run only reads the sources that are due.

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
- `limit`: sources per shard (6 shards run in parallel)
- `dry`: read and report only

Locally: `pip install -r requirements.txt && python -m playwright install chromium`, set the three environment variables, then `python -m collector.run --name "Sadler" --dry`.

## Limits worth knowing

- Sites that block automated visitors, need a login, or publish schedules only as PDF will come back as `failed`; the note says why.
- A site that changes its layout is handled automatically in most cases, because Claude reads the page rather than fixed HTML positions.
- Claude usage grows with the number of pages read. The run summary shows tokens per run so you can track cost.
