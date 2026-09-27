-- Saffitt v3, part 1: one performance, many listings.
-- Every website that lists a performance gets one row in performance_sources with what that site says.
-- The performance itself shows the values chosen by precedence (recompute_performance, part 2).

-- 1. Each performance keeps its own time zone, so shows without a known venue display at the right local time.
alter table public.performances add column if not exists timezone text;

update public.performances p set timezone = x.tz
from (select distinct on (performance_id) performance_id, timezone tz
      from public.import_staging_performances
      where performance_id is not null and timezone is not null
      order by performance_id, scraped_at desc) x
where x.performance_id = p.id and p.timezone is null;

create or replace view public.performance_listing as
 select p.id, p.slug, p.title, p.original_title, p.performance_date,
    coalesce(t.timezone, p.timezone, 'Europe/Paris') as timezone,
    (p.performance_date at time zone coalesce(t.timezone, p.timezone, 'Europe/Paris'))::date as local_date,
    to_char(p.performance_date at time zone coalesce(t.timezone, p.timezone, 'Europe/Paris'), 'HH24:MI') as local_time,
    ('Time TBC' = any (coalesce(p.tags, '{}'::text[]))) as time_tbc,
    p.tags, p.editorial, p.ticket_url, p.image_url, p.description, p.program_id, p.program_position,
    g.title as program_title, g.slug as program_slug, p.work_id, w.title as work_title,
    p.choreographer_id, c.name as choreographer_name, c.slug as choreographer_slug,
    p.company_id, co.name as company_name, co.slug as company_slug,
    p.theater_id, t.name as theater_name, t.slug as theater_slug,
    coalesce(t.city, p.city) as city, coalesce(t.country, p.country) as country,
    lower(unaccent_fallback(concat_ws(' ', p.title, p.original_title, w.title, g.title, c.name, co.name, t.name,
          coalesce(t.city, p.city), coalesce(t.country, p.country)))) as search_text,
    p.date_source, (p.date_source = 'from_range') as dates_to_confirm
   from performances p
     left join theaters t on t.id = p.theater_id
     left join companies co on co.id = p.company_id
     left join choreographers c on c.id = p.choreographer_id
     left join works w on w.id = p.work_id
     left join programs g on g.id = p.program_id;

-- 2. Which stages a venue website speaks for (its own house and stages). Filled from the source's theater and
--    from the stages in its own city where it has published shows; editable by hand.
create table if not exists public.source_venues (
  source_id uuid not null references public.sources(id) on delete cascade,
  theater_id uuid not null references public.theaters(id) on delete cascade,
  added_by text not null default 'auto',
  primary key (source_id, theater_id)
);
alter table public.source_venues enable row level security;

create or replace function public.is_venue_source(p_kind text) returns boolean language sql immutable as $$
  select coalesce(p_kind, '') !~* '(company|festival|competition)'
$$;

insert into public.source_venues (source_id, theater_id)
select id, theater_id from public.sources where theater_id is not null
on conflict do nothing;

insert into public.source_venues (source_id, theater_id)
select distinct s.id, t.id
from public.sources s
join public.performances p on p.source_id = s.id
join public.theaters t on t.id = p.theater_id
where is_venue_source(s.kind) and s.city is not null and lower(t.city) = lower(s.city)
on conflict do nothing;

-- 3. Precedence of a website for one listing (lower wins):
--    1 tier 0 venue listing a show on its own stage, 2 festival or presenter, 3 the company's own site,
--    4 a venue below tier 0 on its own stage, 5 anything else, 6 unknown source.
create or replace function public.source_rank(p_source uuid, p_theater uuid, p_company uuid)
returns int language sql stable as $$
  select case
    when s.id is null then 6
    when is_venue_source(s.kind) and p_theater is not null
         and exists (select 1 from source_venues v where v.source_id = s.id and v.theater_id = p_theater)
      then case when s.priority = 0 then 1 else 4 end
    when s.kind ~* 'festival' then 2
    when (p_company is not null and s.company_id = p_company) or s.kind ~* 'company' then 3
    else 5 end
  from (select 1) x left join sources s on s.id = p_source
$$;

-- 4. The listings.
create table if not exists public.performance_sources (
  id bigint generated always as identity primary key,
  performance_id uuid not null references public.performances(id) on delete cascade,
  source_id uuid references public.sources(id) on delete set null,
  staging_id bigint,
  listed_title text, listed_venue text, listed_city text,
  title text, original_title text,
  theater_id uuid references public.theaters(id) on delete set null,
  company_id uuid references public.companies(id) on delete set null,
  choreographer_id uuid references public.choreographers(id) on delete set null,
  work_id uuid references public.works(id) on delete set null,
  program_id uuid references public.programs(id) on delete set null,
  program_position int,
  starts_at timestamptz, time_tbc boolean not null default false, timezone text,
  ticket_url text, image_url text, description text, cancelled boolean not null default false,
  ignored boolean not null default false,          -- set when a review found this site wrong for this show
  ignored_reason text,
  first_seen_at timestamptz not null default now(),
  last_seen_at timestamptz not null default now()
);
create unique index if not exists performance_sources_one_per_site
  on public.performance_sources (performance_id, coalesce(source_id, '00000000-0000-0000-0000-000000000000'::uuid));
create index if not exists performance_sources_source on public.performance_sources (source_id);
alter table public.performance_sources enable row level security;
comment on table public.performance_sources is 'Every website that lists a performance, with what it says. The performance shows the values chosen by precedence.';

-- Backfill: the site each performance came from, with the performance's own values.
insert into public.performance_sources (performance_id, source_id, title, original_title, theater_id, company_id,
  choreographer_id, work_id, program_id, program_position, starts_at, time_tbc, timezone, ticket_url, image_url,
  description, cancelled, listed_title, first_seen_at, last_seen_at)
select p.id, p.source_id, p.title, p.original_title, p.theater_id, p.company_id, p.choreographer_id, p.work_id,
  p.program_id, p.program_position, p.performance_date, 'Time TBC' = any(coalesce(p.tags, '{}')), p.timezone,
  p.ticket_url, p.image_url, p.description, 'Cancelled' = any(coalesce(p.tags, '{}')), p.title,
  coalesce(p.created_at, now()), coalesce(p.last_seen_at, now())
from public.performances p
on conflict do nothing;

-- Backfill: other sites whose staging rows were merged into an existing performance (the history C needs).
insert into public.performance_sources (performance_id, source_id, staging_id, listed_title, listed_venue, listed_city,
  title, original_title, theater_id, company_id, choreographer_id, work_id, program_id, program_position,
  starts_at, time_tbc, timezone, ticket_url, image_url, description, cancelled, first_seen_at, last_seen_at)
select distinct on (s.performance_id, s.source_id)
  s.performance_id, s.source_id, s.id, s.title, s.venue, s.city,
  p.title, coalesce(s.original_title, p.original_title),
  coalesce((select t.id from theaters t where s.venue is not null
             and (lower(t.name) = lower(trim(s.venue)) or lower(trim(s.venue)) = any (select lower(a) from unnest(coalesce(t.aliases, '{}')) a))
           limit 1), case when s.venue is null then null else p.theater_id end),
  p.company_id, p.choreographer_id, p.work_id, p.program_id, p.program_position,
  ((s.performance_date::timestamp + coalesce(case when s.time_tbc or coalesce(s.performance_time,'') !~ '^\s*\d{1,2}:\d{2}\s*$' then null else trim(s.performance_time)::time end, time '00:00'))
     at time zone coalesce(s.timezone, p.timezone, 'Europe/Paris')),
  coalesce(s.time_tbc, false) or coalesce(s.performance_time,'') !~ '^\s*\d{1,2}:\d{2}\s*$',
  s.timezone, s.tickets_url, s.image_url, s.description, coalesce(s.cancelled, false), s.scraped_at, coalesce(s.promoted_at, s.scraped_at)
from public.import_staging_performances s
join public.performances p on p.id = s.performance_id
where s.status = 'promoted' and s.source_id is not null
order by s.performance_id, s.source_id, s.scraped_at desc
on conflict do nothing;
