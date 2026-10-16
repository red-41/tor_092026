# Supabase cleanup review, 7 October 2026

Method: every table, view, function, trigger, policy and index in `public` was checked against (1) the other
database objects (function bodies, view definitions, policies, triggers), (2) the collector code in this repo,
(3) the site's calls as recorded by the server since 8 June 2026 (`pg_stat_statements`, by API role),
(4) table and index usage statistics. Edge functions: 5, all in use (check-subscription, create-checkout,
customer-portal, delete-account, sitemap). Storage: no buckets. No duplicate policies.

## A. Safe to delete now (nothing references them anywhere)

```sql
drop table public.credit_line_members;            -- empty; replaced by choreographers.member_ids on 7 Oct
drop table public.credit_line_companies;          -- empty; replaced by choreographers.credit_company_ids
drop table public.backup_perf_pre_recompute_20260927;   -- 413-row backup from 27 Sep, verified since
drop function public.apply_duplicate_review(text);      -- old review step, replaced by apply_review / review_queue
drop function public.source_is_company(uuid);           -- replaced by is_venue_source
drop function public.is_public_planner(uuid);           -- replaced by get_planner_owner_by_token
drop view public.public_planner_profiles;               -- same
drop view public.possible_duplicate_performances;       -- replaced by find_duplicate_candidates
drop view public.unconfirmed_range_dates;               -- replaced by performance_listing.dates_to_confirm
drop view public.performance_credits;                   -- information only, no reader (performance -> people)
```

## B. Unused by the site and the collector; diagnostics for the SQL editor only

Drop them if you never open them. `plausibility_flags` reports wrong cities and should go or be fixed.

```sql
drop view public.plausibility_flags;
drop view public.company_venue_mismatches;
drop view public.suggested_new_sources;
drop view public.upcoming_runs_without_dates;
drop view public.title_is_company_name;        -- used by hand for the "52 generic titles" backlog
drop function public.desktop_stage(text, text, text, text, jsonb);   -- manual staging from a desktop run (27 Sep);
                                                 -- keep if the Russian houses will be collected by hand
```

## C. Empty, but the site still references them: remove together with a Lovable cleanup, not seamless

| Object | Why it exists | Evidence of site use |
|---|---|---|
| table `venues` (0 rows) | legacy, replaced by `theaters` | 13 anonymous reads in 4 months, last 24 Sep |
| tables `dancers`, `performance_dancers` (0 rows) | an admin "dancers" page, never filled | admin list sorted by name, 3 reads |
| column `choreographers.country` (never filled) | `nationality` is used instead | in generated types |
| column `theaters.capacity` (never filled) | | in generated types |
| column `profiles.avatar_url` (never filled) | avatar in the UI | in generated types |

`user_ratings` is empty too but stays: the delete-account function clears it and the rating UI writes it.

## D. Not redundant, but worth a housekeeping rule

- `import_staging_performances` keeps every promoted row forever (27,649 rows, 24 MB, about 5,000 more per run).
  Prune `status in ('promoted', 'rejected') and scraped_at < now() - interval '14 days'` in `review_housekeeping()`.
- 21 indexes have never been used (each 320 kB or smaller, 3 MB in all). Most support foreign keys on delete; keep.
- `collector_page_cache` (16 MB) has no rows older than 28 days; the cache is self-limiting.

## Everything else is in use

Site (via server functions): `listing_query`, `performance_by_slug`, `performance_filter_options`, `explore_facets`,
`upcoming_counts`, `top_upcoming_cities`, `sitemap_entries`, `get_public_planner_items`, `get_planner_owner_by_token`,
`has_role`, and the tables `profiles`, `user_lists`, `user_roles`, `premium_grants`, `journal_entries`,
`manual_planner_entries`, `choreographers`, `companies`, `theaters`, `works`, `programs`.
Collector and review: `promote_staging_performances`, `refresh_source_venues`, `review_housekeeping`,
`find_duplicate_candidates`, `review_queue`, `apply_review`, `merge_performances`, `suppress_performance`,
`recompute_performance`, `resolve_match`, `source_coverage`, `source_venues`, `source_checks`,
`collector_page_cache`, `performance_sources`, `performance_conflicts`, `performance_suppressions`,
`match_review`, `duplicate_review`, `production_runs`, `listing_usage`, `evening_groups`, `credit_review`.
