-- Saffitt v3, part 13: cost controls (applied 16 October 2026 as migrations v3_13_spend_ledger and
-- v3_13b_model_comparison_view; this file is the record).
--
-- 1. collector_spend: the spending ledger. Every Claude request of the collector and the duplicate check is paid
--    from small blocks reserved here first (spend_reserve). A block is refused when it would take the UTC calendar
--    month past MONTHLY_BUDGET_USD or the workflow run past RUN_BUDGET_USD. spend_record writes the corrections
--    (actual cost minus estimate) and gives back unused reservations. collector_spend_by_run: spending per run.
-- 2. sources.announcement_at: the date a source said it will publish more (season, programme, dates, tickets).
-- 3. model_comparison + model_comparison_pages: the Haiku vs Sonnet test (run.py --compare).
--
-- What was spent this month:
--   select round(sum(usd), 2) from collector_spend where at >= date_trunc('month', now() at time zone 'UTC') at time zone 'UTC';
-- Per run:
--   select * from collector_spend_by_run order by started desc;
-- Model test, overall:
--   select count(*) pages, sum(r_shows) ref_shows, sum(same_shows) same, sum(cardinality(missed)) missed,
--          sum(cardinality(extra)) extra, sum(same_dates) same_dates
--   from model_comparison_pages where run_id = '<GitHub run id>';

create table if not exists public.collector_spend (
  id bigint generated always as identity primary key,
  at timestamptz not null default now(),
  run_id text not null,
  shard int,
  part text not null default 'collect',            -- collect | review | compare
  kind text not null check (kind in ('reserve', 'release', 'adjust')),
  usd numeric(12,6) not null,
  note text
);
create index if not exists collector_spend_at on public.collector_spend (at);
create index if not exists collector_spend_run on public.collector_spend (run_id);
alter table public.collector_spend enable row level security;   -- service key only
revoke all on public.collector_spend from anon, authenticated;

create or replace function public.spend_reserve(p_run text, p_shard int, p_part text, p_usd numeric,
                                                p_month_cap numeric, p_run_cap numeric)
returns jsonb language plpgsql security definer set search_path to 'public' as $function$
declare v_month numeric; v_run numeric;
begin
  perform pg_advisory_xact_lock(hashtext('collector_spend'));
  select coalesce(sum(usd), 0) into v_month from collector_spend
   where at >= date_trunc('month', now() at time zone 'UTC') at time zone 'UTC';
  select coalesce(sum(usd), 0) into v_run from collector_spend where run_id = p_run;
  if p_month_cap is not null and v_month + p_usd > p_month_cap then
    return jsonb_build_object('ok', false, 'why', 'month', 'month', v_month, 'run', v_run,
                              'month_cap', p_month_cap, 'run_cap', p_run_cap);
  end if;
  if p_run_cap is not null and v_run + p_usd > p_run_cap then
    return jsonb_build_object('ok', false, 'why', 'run', 'month', v_month, 'run', v_run,
                              'month_cap', p_month_cap, 'run_cap', p_run_cap);
  end if;
  insert into collector_spend (run_id, shard, part, kind, usd) values (p_run, p_shard, p_part, 'reserve', p_usd);
  return jsonb_build_object('ok', true, 'month', v_month + p_usd, 'run', v_run + p_usd,
                            'month_cap', p_month_cap, 'run_cap', p_run_cap);
end $function$;

create or replace function public.spend_record(p_run text, p_shard int, p_part text, p_kind text, p_usd numeric,
                                               p_note text default null)
returns void language sql security definer set search_path to 'public' as $function$
  insert into collector_spend (run_id, shard, part, kind, usd, note) values (p_run, p_shard, p_part, p_kind, p_usd, p_note);
$function$;

create or replace view public.collector_spend_by_run with (security_invoker = true) as
select run_id, min(at) as started, max(at) as last, round(sum(usd), 2) as usd,
       round(sum(usd) filter (where part = 'collect'), 2) as collect_usd,
       round(sum(usd) filter (where part = 'review'), 2) as review_usd,
       round(sum(usd) filter (where part = 'compare'), 2) as compare_usd
from public.collector_spend group by run_id;

revoke execute on function public.spend_reserve(text, int, text, numeric, numeric, numeric) from anon, authenticated, public;
revoke execute on function public.spend_record(text, int, text, text, numeric, text) from anon, authenticated, public;
revoke all on public.collector_spend_by_run from anon, authenticated;

alter table public.sources add column if not exists announcement_at date;

create table if not exists public.model_comparison (
  id bigint generated always as identity primary key,
  at timestamptz not null default now(),
  run_id text, source_id uuid, source_name text, tier smallint,
  url text not null, role text not null, text_hash text,
  model text not null, result jsonb, usage jsonb,
  ref_model text, ref_result jsonb, ref_usage jsonb, ref_from_cache boolean
);
create index if not exists model_comparison_run on public.model_comparison (run_id);
alter table public.model_comparison enable row level security;
revoke all on public.model_comparison from anon, authenticated;

-- model_comparison_pages (one row per compared page; shows found by each model, missed, extra, titles, people):
-- see migration v3_13b_model_comparison_view, or select pg_get_viewdef('public.model_comparison_pages');
