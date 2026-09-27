-- Saffitt v3, part 2: precedence, conflicts, merging, review queues.

-- A title that is really a company name or a tour label ("Boston Ballet", "International Tour | Gütersloh").
create or replace function public.is_generic_title(p_title text, p_company uuid) returns boolean
language sql stable as $$
  select p_title is null
      or p_title ~* '(on tour|international tour|tournée|en gira|in tournée|gastspiel)'
      or exists (select 1 from companies c where c.id = p_company and slugify(c.name) = slugify(p_title))
$$;

-- Disagreements between trusted sites about one performance (tier 0 host, festival, company).
create table if not exists public.performance_conflicts (
  id bigint generated always as identity primary key,
  performance_id uuid not null references public.performances(id) on delete cascade,
  field text not null check (field in ('time', 'venue')),
  detail jsonb not null,
  status text not null default 'open' check (status in ('open', 'resolved', 'dismissed')),
  note text,
  created_at timestamptz not null default now(),
  resolved_at timestamptz
);
create unique index if not exists performance_conflicts_open on public.performance_conflicts (performance_id, field) where status = 'open';
alter table public.performance_conflicts enable row level security;

-- Set a performance's shown values from its listings, by precedence (see source_rank):
-- venue, date, time and ticket link from the best-ranked site that gives them; credits from the best-ranked site
-- whose title is a real title (not a company name or tour label); a timed listing beats a "time TBC" one.
create or replace function public.recompute_performance(p_id uuid) returns void
language plpgsql set search_path to 'public', 'extensions' as $$
declare
  v record; w_theater uuid; w_when record; w_ticket text; w_credit record; w_img text; w_desc text; w_comp uuid;
  w_cancel boolean; v_tags text[]; v_diff record;
begin
  select * into v from performances where id = p_id;
  if v.id is null then return; end if;
  create temp table if not exists _rk (like performance_sources including defaults) on commit drop;
  alter table _rk add column if not exists rk int;
  delete from _rk where true;   -- the API gateway refuses a DELETE without WHERE
  insert into _rk select ps.*, source_rank(ps.source_id, ps.theater_id, coalesce(ps.company_id, v.company_id))
    from performance_sources ps where ps.performance_id = p_id and not ps.ignored;
  if not exists (select 1 from _rk) then return; end if;

  select theater_id into w_theater from _rk where theater_id is not null order by rk, last_seen_at desc limit 1;
  select starts_at, time_tbc, timezone into w_when from _rk where starts_at is not null
    order by time_tbc, rk, last_seen_at desc limit 1;
  select ticket_url into w_ticket from _rk where ticket_url is not null order by rk, last_seen_at desc limit 1;
  select title, original_title, work_id, choreographer_id, program_id, program_position into w_credit from _rk
    where not is_generic_title(title, coalesce(company_id, v.company_id)) order by rk, last_seen_at desc limit 1;
  select company_id into w_comp from _rk where company_id is not null
    order by case when rk = 3 then 0 else rk end, last_seen_at desc limit 1;
  select image_url into w_img from _rk where image_url is not null order by rk, last_seen_at desc limit 1;
  select description into w_desc from _rk where description is not null order by rk, last_seen_at desc limit 1;
  select cancelled into w_cancel from _rk order by rk, last_seen_at desc limit 1;

  v_tags := array_remove(array_remove(coalesce(v.tags, '{}'), 'Time TBC'), 'Cancelled');
  if coalesce(w_when.time_tbc, false) then v_tags := v_tags || 'Time TBC'::text; end if;
  if coalesce(w_cancel, false) then v_tags := v_tags || 'Cancelled'::text; end if;

  update performances p set
    theater_id = coalesce(w_theater, p.theater_id),
    performance_date = coalesce(w_when.starts_at, p.performance_date),
    timezone = coalesce(w_when.timezone, p.timezone),
    ticket_url = coalesce(w_ticket, p.ticket_url),
    title = coalesce(w_credit.title, p.title),
    original_title = coalesce(w_credit.original_title, p.original_title),
    work_id = coalesce(w_credit.work_id, p.work_id),
    choreographer_id = coalesce(w_credit.choreographer_id, p.choreographer_id),
    program_id = coalesce(w_credit.program_id, p.program_id),
    program_position = coalesce(w_credit.program_position, p.program_position),
    company_id = coalesce(w_comp, p.company_id),
    image_url = coalesce(w_img, p.image_url),
    description = coalesce(w_desc, p.description),
    tags = (select array_agg(distinct x) from unnest(v_tags) x)
  where p.id = p_id
    and (p.theater_id, p.performance_date, p.ticket_url, p.title, p.company_id, p.tags, p.timezone)
        is distinct from (coalesce(w_theater, p.theater_id), coalesce(w_when.starts_at, p.performance_date),
                          coalesce(w_ticket, p.ticket_url), coalesce(w_credit.title, p.title), coalesce(w_comp, p.company_id),
                          (select array_agg(distinct x) from unnest(v_tags) x), coalesce(w_when.timezone, p.timezone));

  -- conflicts between trusted sites (rank 1 to 3)
  select jsonb_agg(jsonb_build_object('source', s.name, 'rank', r.rk, 'time', to_char(r.starts_at at time zone coalesce(r.timezone, 'UTC'), 'YYYY-MM-DD HH24:MI'), 'url', r.ticket_url))
    as d, max(abs(extract(epoch from (r.starts_at - w_when.starts_at)))) as gap
    into v_diff
    from _rk r left join sources s on s.id = r.source_id
   where r.rk <= 3 and not r.time_tbc and w_when.starts_at is not null and not coalesce(w_when.time_tbc, false);
  if coalesce(v_diff.gap, 0) > 900 then
    insert into performance_conflicts (performance_id, field, detail) values (p_id, 'time', v_diff.d) on conflict do nothing;
  end if;
  if exists (select 1 from _rk r where r.rk <= 3 and r.theater_id is not null and r.theater_id <> w_theater) then
    insert into performance_conflicts (performance_id, field, detail)
    select p_id, 'venue', jsonb_agg(jsonb_build_object('source', s.name, 'rank', r.rk, 'venue', t.name, 'url', r.ticket_url))
      from _rk r left join sources s on s.id = r.source_id left join theaters t on t.id = r.theater_id
     where r.theater_id is not null
    on conflict do nothing;
  end if;
end $$;

-- Merge two rows that are the same performance: listings move to the kept one, the other is removed.
create or replace function public.merge_performances(p_drop uuid, p_keep uuid, p_reason text default null)
returns void language plpgsql set search_path to 'public' as $$
begin
  if p_drop = p_keep then return; end if;
  update performance_sources d set performance_id = p_keep
   where d.performance_id = p_drop
     and not exists (select 1 from performance_sources k where k.performance_id = p_keep
                     and coalesce(k.source_id, '00000000-0000-0000-0000-000000000000'::uuid)
                       = coalesce(d.source_id, '00000000-0000-0000-0000-000000000000'::uuid));
  update performances k set editorial = coalesce(k.editorial, d.editorial)
    from performances d where d.id = p_drop and k.id = p_keep;
  update import_staging_performances set performance_id = p_keep where performance_id = p_drop;
  insert into duplicate_review (review_run, drop_id, keep_id, reason, verdict, status, decided_at, drop_snapshot)
  select 'merge-' || to_char(now(), 'YYYY-MM-DD'), p_drop, p_keep, coalesce(p_reason, 'merged'), 'duplicate', 'applied', now(), to_jsonb(pl)
    from performance_listing pl where pl.id = p_drop
  on conflict do nothing;
  delete from performances where id = p_drop;
  perform recompute_performance(p_keep);
end $$;

-- Staging rows that might be a show already on the site but are not certain: held until reviewed.
create table if not exists public.match_review (
  id bigint generated always as identity primary key,
  staging_id bigint not null unique,
  candidates uuid[] not null,
  reason text not null,
  status text not null default 'open' check (status in ('open', 'same', 'different', 'expired')),
  chosen uuid,
  review_note text,
  created_at timestamptz not null default now(),
  decided_at timestamptz
);
alter table public.match_review enable row level security;

-- Duplicate review: also 'candidate' (found by the checker, waiting for Claude) and 'same_show'/'different' verdicts.
alter table public.duplicate_review drop constraint if exists duplicate_review_verdict_check;
alter table public.duplicate_review add constraint duplicate_review_verdict_check
  check (verdict in ('candidate', 'duplicate', 'out_of_scope', 'school_show', 'conflict_check', 'not_duplicate'));
alter table public.duplicate_review add column if not exists review_note text;

-- Pairs of published performances that may be the same show (same city and day; same company, work or a similar
-- title; start within 90 minutes or one without a time). Several sessions of one show at one venue are not pairs.
create or replace function public.find_duplicate_candidates(p_run text, p_days int default 400)
returns int language plpgsql set search_path to 'public', 'extensions' as $$
declare n int;
begin
  with p as (
    select pl.id, pl.local_date d, pl.local_time t, pl.time_tbc tbc, lower(coalesce(pl.city, '?')) city, pl.theater_id th,
           pl.company_id co, pl.work_id w, pl.program_id prog, pl.title,
           slugify(regexp_replace(pl.title, '\s*\(.*\)\s*$', '')) nt
      from performance_listing pl
     where pl.local_date >= current_date and pl.local_date < current_date + p_days),
  pairs as (
    select a.id a_id, b.id b_id, a.th = b.th same_venue, a.tbc or b.tbc one_tbc,
           case when a.tbc or b.tbc then 0 else abs(extract(epoch from (a.t::time - b.t::time))) / 60 end gap_min
      from p a join p b on a.id < b.id and a.d = b.d and a.city = b.city
     where (a.prog is null or b.prog is null or a.prog <> b.prog)
       and ((a.co = b.co and a.co is not null) or a.w = b.w or a.nt = b.nt or similarity(a.nt, b.nt) > 0.55
            or (length(a.nt) >= 5 and b.nt like '%' || a.nt || '%') or (length(b.nt) >= 5 and a.nt like '%' || b.nt || '%'))
       and (a.tbc or b.tbc or abs(extract(epoch from (a.t::time - b.t::time))) <= 5400)),
  sessions as (   -- a show given several times that day at one venue: its sessions are not duplicates
    select a_id, b_id from pairs x
     where x.same_venue and not x.one_tbc
       and (select count(*) from p q, p a where a.id = x.a_id and q.th = a.th and q.d = a.d and q.nt = a.nt) >= 3)
  insert into duplicate_review (review_run, drop_id, keep_id, reason, verdict)
  select p_run, x.b_id, x.a_id,
         case when x.one_tbc then 'same day, one without time'
              when not coalesce(x.same_venue, false) then 'same day, different or missing venue, start ' || round(x.gap_min) || ' min apart'
              when x.gap_min in (60, 120) then 'same venue, starts exactly ' || round(x.gap_min) || ' min apart (time zone copy?)'
              else 'same venue, start ' || round(x.gap_min) || ' min apart' end,
         'candidate'
    from pairs x
   where not exists (select 1 from sessions s where s.a_id = x.a_id and s.b_id = x.b_id)
     and not exists (select 1 from duplicate_review r where r.verdict = 'not_duplicate'
                      and ((r.drop_id = x.b_id and r.keep_id = x.a_id) or (r.drop_id = x.a_id and r.keep_id = x.b_id)))
  on conflict do nothing;
  get diagnostics n = row_count;
  return n;
end $$;

-- Everything the reviewer needs about one performance: its shown values and every site that lists it.
create or replace function public.performance_evidence(p_id uuid) returns jsonb
language sql stable set search_path to 'public' as $$
  select jsonb_build_object(
    'id', pl.id, 'title', pl.title, 'original_title', pl.original_title, 'date', pl.local_date, 'time', pl.local_time,
    'time_tbc', pl.time_tbc, 'venue', pl.theater_name, 'city', pl.city, 'country', pl.country,
    'company', pl.company_name, 'choreographer', pl.choreographer_name, 'program', pl.program_title, 'ticket_url', pl.ticket_url,
    'listed_by', (select jsonb_agg(jsonb_build_object(
        'source', s.name, 'source_kind', s.kind, 'tier', s.priority, 'rank', source_rank(ps.source_id, ps.theater_id, coalesce(ps.company_id, pl.company_id)),
        'listed_title', coalesce(ps.listed_title, ps.title), 'listed_venue', coalesce(ps.listed_venue, t.name),
        'time', to_char(ps.starts_at at time zone coalesce(ps.timezone, pl.timezone), 'HH24:MI'), 'time_tbc', ps.time_tbc,
        'url', ps.ticket_url, 'site', s.website, 'last_seen', ps.last_seen_at::date))
      from performance_sources ps left join sources s on s.id = ps.source_id left join theaters t on t.id = ps.theater_id
      where ps.performance_id = pl.id and not ps.ignored))
  from performance_listing pl where pl.id = p_id
$$;
