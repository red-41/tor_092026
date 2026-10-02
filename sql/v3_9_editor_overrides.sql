-- Saffitt v3, part 9: what an admin does in the admin sticks.
-- 1. An admin edit becomes a listing from "Saffitt editors", which ranks above every website, so a later
--    collection cannot overwrite it. Only the fields the admin changed are pinned; the rest keep following the sites.
-- 2. A performance an admin deletes (or that a review removes as out of scope) is remembered, and the
--    collector does not publish it again.

insert into public.sources (name, kind, priority, status, read_method, notes)
select 'Saffitt editors', 'Editor', 0, 'excluded', 'manual',
       'Edits made in the Saffitt admin. Never collected; ranks above every website.'
where not exists (select 1 from public.sources where kind = 'Editor');

create or replace function public.editor_source_id() returns uuid
language sql stable set search_path to 'public' as $$
  select id from sources where kind = 'Editor' order by created_at limit 1
$$;

create or replace function public.source_rank(p_source uuid, p_theater uuid, p_company uuid)
returns integer language sql stable set search_path to 'public' as $$
  select case
    when s.id is null then 6
    when s.kind = 'Editor' then 0
    when is_venue_source(s.kind) and p_theater is not null
         and exists (select 1 from source_venues v where v.source_id = s.id and v.theater_id = p_theater)
      then case when s.priority = 0 then 1 else 4 end
    when s.kind ~* 'festival' then 2
    when (p_company is not null and s.company_id = p_company) or s.kind ~* 'company' then 3
    else 5 end
  from (select 1) x left join sources s on s.id = p_source
$$;

-- An editor listing may leave "cancelled" unset (null = the websites decide).
alter table public.performance_sources alter column cancelled drop not null;
do $$
declare d text;
begin
  d := pg_get_functiondef('public.recompute_performance(uuid)'::regprocedure);
  if position('where cancelled is not null order by rk' in d) > 0 then return; end if;
  if position('select cancelled into w_cancel from _rk order by rk, last_seen_at desc limit 1;' in d) = 0 then
    raise exception 'recompute_performance: pattern not found';
  end if;
  execute replace(d, 'select cancelled into w_cancel from _rk order by rk, last_seen_at desc limit 1;',
                     'select cancelled into w_cancel from _rk where cancelled is not null order by rk, last_seen_at desc limit 1;');
end $$;

-- Requests from the website carry the signed-in user's token; the collector uses the service key.
create or replace function public.is_admin_request() returns boolean
language sql stable set search_path to 'public' as $$
  select coalesce(current_setting('request.jwt.claims', true)::jsonb ->> 'role', '') = 'authenticated'
     and has_role(auth.uid(), 'admin'::app_role)
$$;

create or replace function public.editor_listing() returns trigger
language plpgsql security definer set search_path to 'public' as $$
declare v_src uuid := editor_source_id(); ins boolean := tg_op = 'INSERT';
begin
  if v_src is null or not is_admin_request() then return new; end if;
  insert into performance_sources as ps (performance_id, source_id, listed_title, title, original_title, theater_id,
      company_id, choreographer_id, work_id, program_id, program_position, starts_at, time_tbc, timezone,
      ticket_url, image_url, description, cancelled)
  values (new.id, v_src, new.title,
      case when ins or new.title is distinct from old.title then new.title end,
      case when ins or new.original_title is distinct from old.original_title then new.original_title end,
      case when ins or new.theater_id is distinct from old.theater_id then new.theater_id end,
      case when ins or new.company_id is distinct from old.company_id then new.company_id end,
      case when ins or new.choreographer_id is distinct from old.choreographer_id then new.choreographer_id end,
      case when ins or new.work_id is distinct from old.work_id then new.work_id end,
      case when ins or new.program_id is distinct from old.program_id then new.program_id end,
      case when ins or new.program_position is distinct from old.program_position then new.program_position end,
      case when ins or new.performance_date is distinct from old.performance_date
                    or ('Time TBC' = any(coalesce(new.tags, '{}'))) is distinct from ('Time TBC' = any(coalesce(old.tags, '{}')))
           then new.performance_date end,
      'Time TBC' = any(coalesce(new.tags, '{}')),
      new.timezone,
      case when ins or new.ticket_url is distinct from old.ticket_url then new.ticket_url end,
      case when ins or new.image_url is distinct from old.image_url then new.image_url end,
      case when ins or new.description is distinct from old.description then new.description end,
      case when ins or ('Cancelled' = any(coalesce(new.tags, '{}'))) is distinct from ('Cancelled' = any(coalesce(old.tags, '{}')))
           then 'Cancelled' = any(coalesce(new.tags, '{}')) end)
  on conflict (performance_id, coalesce(source_id, '00000000-0000-0000-0000-000000000000'::uuid)) do update set
      listed_title = excluded.listed_title,
      title = coalesce(excluded.title, ps.title),
      original_title = coalesce(excluded.original_title, ps.original_title),
      theater_id = coalesce(excluded.theater_id, ps.theater_id),
      company_id = coalesce(excluded.company_id, ps.company_id),
      choreographer_id = coalesce(excluded.choreographer_id, ps.choreographer_id),
      work_id = coalesce(excluded.work_id, ps.work_id),
      program_id = coalesce(excluded.program_id, ps.program_id),
      program_position = coalesce(excluded.program_position, ps.program_position),
      starts_at = coalesce(excluded.starts_at, ps.starts_at),
      time_tbc = case when excluded.starts_at is not null then excluded.time_tbc else ps.time_tbc end,
      timezone = coalesce(excluded.timezone, ps.timezone),
      ticket_url = coalesce(excluded.ticket_url, ps.ticket_url),
      image_url = coalesce(excluded.image_url, ps.image_url),
      description = coalesce(excluded.description, ps.description),
      cancelled = coalesce(excluded.cancelled, ps.cancelled),
      last_seen_at = now();
  return new;
end $$;

drop trigger if exists editor_listing on public.performances;
create trigger editor_listing after insert or update on public.performances
  for each row execute function public.editor_listing();

-- 2. Removed performances are remembered.
create table if not exists public.performance_suppressions (
  id bigint generated always as identity primary key,
  source_id uuid references public.sources(id) on delete cascade,   -- null = any site
  title_slug text not null,
  local_date date not null,
  local_time text,                                                   -- null = any time that day
  theater_id uuid,
  reason text,
  created_by uuid,
  created_at timestamptz not null default now()
);
create index if not exists performance_suppressions_lookup on public.performance_suppressions (local_date, title_slug);
alter table public.performance_suppressions enable row level security;
drop policy if exists "Admins manage suppressions" on public.performance_suppressions;
create policy "Admins manage suppressions" on public.performance_suppressions for all to authenticated
  using (has_role((select auth.uid()), 'admin'::app_role)) with check (has_role((select auth.uid()), 'admin'::app_role));
comment on table public.performance_suppressions is 'Performances removed by an editor or a review. The collector does not publish them again. Delete a row here to allow it back.';

create or replace function public.suppress_performance(p_id uuid, p_reason text) returns void
language sql security definer set search_path to 'public', 'extensions' as $$
  insert into performance_suppressions (source_id, title_slug, local_date, local_time, theater_id, reason, created_by)
  select distinct x.source_id, x.slug, x.d, x.t, x.theater_id, p_reason, auth.uid()
  from (
    select ps.source_id, slugify(regexp_replace(coalesce(ps.listed_title, ps.title, p.title), '\s*\(.*\)\s*$', '')) slug,
           (p.performance_date at time zone coalesce(t.timezone, p.timezone, 'Europe/Paris'))::date d,
           case when 'Time TBC' = any(coalesce(p.tags, '{}')) then null
                else to_char(p.performance_date at time zone coalesce(t.timezone, p.timezone, 'Europe/Paris'), 'HH24:MI') end t,
           p.theater_id
      from performances p left join theaters t on t.id = p.theater_id
      left join performance_sources ps on ps.performance_id = p.id
     where p.id = p_id
    union
    select null, slugify(regexp_replace(p.title, '\s*\(.*\)\s*$', '')),
           (p.performance_date at time zone coalesce(t.timezone, p.timezone, 'Europe/Paris'))::date,
           case when 'Time TBC' = any(coalesce(p.tags, '{}')) then null
                else to_char(p.performance_date at time zone coalesce(t.timezone, p.timezone, 'Europe/Paris'), 'HH24:MI') end,
           p.theater_id
      from performances p left join theaters t on t.id = p.theater_id where p.id = p_id
  ) x where x.slug is not null and x.slug <> ''
$$;
revoke execute on function public.suppress_performance(uuid, text) from anon, authenticated;

create or replace function public.remember_deleted_performance() returns trigger
language plpgsql security definer set search_path to 'public' as $$
begin
  if is_admin_request() then
    perform suppress_performance(old.id, 'deleted in the admin');
  end if;
  return old;
end $$;
drop trigger if exists remember_deleted_performance on public.performances;
create trigger remember_deleted_performance before delete on public.performances
  for each row execute function public.remember_deleted_performance();

-- Scope removals by review (out of scope, school show) are remembered too; duplicate merges are not
-- (the kept row carries the show on).
do $$
declare d text;
begin
  d := pg_get_functiondef('public.apply_duplicate_review(text)'::regprocedure);
  if position('suppress_performance' in d) > 0 then return; end if;
  if position('  return query' in d) = 0 then raise exception 'apply_duplicate_review: pattern not found'; end if;
  execute replace(d, '  return query', '  perform suppress_performance(r.drop_id, ''review: '' || r.verdict)
     from duplicate_review r
    where r.review_run = p_run and r.status = ''approved'' and r.verdict in (''out_of_scope'', ''school_show'')
      and exists (select 1 from performances p where p.id = r.drop_id);
  return query');
end $$;

-- The collector skips anything remembered as removed.
do $$
declare d text; anchor text := 'into v_tz, v_city from (select 1) x left join theaters t on t.id = v_theater;';
begin
  d := pg_get_functiondef('public.promote_staging_performances(boolean, integer, timestamptz, bigint, boolean)'::regprocedure);
  if position('performance_suppressions' in d) > 0 then return; end if;
  if position(anchor in d) = 0 then raise exception 'promote: pattern not found'; end if;
  execute replace(d, anchor, anchor || '

    if exists (select 1 from performance_suppressions x
                where x.local_date = r.performance_date
                  and x.title_slug = slugify(regexp_replace(coalesce(nullif(trim(r.title), ''''), r.work), ''\s*\(.*\)\s*$'', ''''))
                  and (x.source_id is null or x.source_id = r.source_id)
                  and (x.theater_id is null or v_theater is null or x.theater_id = v_theater)
                  and (x.local_time is null or case when v_tbc then true else x.local_time = to_char(trim(r.performance_time)::time, ''HH24:MI'') end)) then
      if not dry_run then
        update import_staging_performances s set status = ''rejected'', action = ''rejected'',
          reject_reason = ''removed by an editor or a review'' where s.id = r.id;
      end if;
      staging_id := r.id; action := ''reject''; performance_id := null; detail := ''removed by an editor or a review'';
      return next; continue;
    end if;');
end $$;

-- Remember the removals already made by reviews (from their snapshots).
insert into public.performance_suppressions (source_id, title_slug, local_date, local_time, theater_id, reason)
select distinct null::uuid, slugify(regexp_replace(r.drop_snapshot ->> 'title', '\s*\(.*\)\s*$', '')),
       (r.drop_snapshot ->> 'local_date')::date,
       case when (r.drop_snapshot ->> 'time_tbc')::boolean then null else r.drop_snapshot ->> 'local_time' end,
       (r.drop_snapshot ->> 'theater_id')::uuid, 'review: ' || r.verdict || ' (' || r.review_run || ')'
  from public.duplicate_review r
 where r.status = 'applied' and r.drop_snapshot is not null
   and (r.verdict in ('out_of_scope', 'school_show') or r.review_run like 'fix-%')
   and (r.drop_snapshot ->> 'local_date')::date >= current_date
   and not exists (select 1 from public.performances p where p.id = r.drop_id);

revoke execute on function public.editor_listing() from anon, authenticated;
revoke execute on function public.remember_deleted_performance() from anon, authenticated;
