-- Saffitt v3, part 3: publishing staging rows as listings.
-- Same matching as v2, plus: (a) copies of a date without venue/time made in the same run are dropped,
-- (b) the same show under another venue name in the same city and day is recognised (Afanador),
-- (c) an uncertain match is held for review instead of being published as a second row,
-- (d) every published row records which site listed it (performance_sources) and the shown values follow precedence.

-- Two titles name the same show: similar enough, or one contains the other ("Afanador" / "Afanador - Ballet Nacional de España").
create or replace function public.title_close(a text, b text) returns boolean language sql stable
set search_path to 'public', 'extensions' as $$
  select similarity(slugify(a), slugify(b)) >= 0.3
      or (length(slugify(b)) >= 5 and slugify(a) like '%' || slugify(b) || '%')
      or (length(slugify(a)) >= 5 and slugify(b) like '%' || slugify(a) || '%')
$$;

create or replace function public.upsert_listing(p_perf uuid, r public.import_staging_performances, p_title text,
  p_theater uuid, p_comp uuid, p_chor uuid, p_work uuid, p_prog uuid, p_ts timestamptz, p_tbc boolean, p_tz text)
returns void language sql set search_path to 'public' as $$
  insert into performance_sources as ps (performance_id, source_id, staging_id, listed_title, listed_venue, listed_city,
    title, original_title, theater_id, company_id, choreographer_id, work_id, program_id, program_position,
    starts_at, time_tbc, timezone, ticket_url, image_url, description, cancelled)
  values (p_perf, r.source_id, r.id, r.title, nullif(trim(r.venue), ''), nullif(trim(r.city), ''),
    p_title, nullif(trim(r.original_title), ''), p_theater, p_comp, p_chor, p_work, p_prog, r.program_position,
    p_ts, p_tbc, p_tz, nullif(trim(r.tickets_url), ''), nullif(trim(r.image_url), ''), nullif(trim(r.description), ''),
    coalesce(r.cancelled, false))
  on conflict (performance_id, coalesce(source_id, '00000000-0000-0000-0000-000000000000'::uuid)) do update set
    staging_id = excluded.staging_id, listed_title = excluded.listed_title, listed_venue = excluded.listed_venue,
    listed_city = excluded.listed_city, title = excluded.title,
    original_title = coalesce(excluded.original_title, ps.original_title),
    theater_id = excluded.theater_id, company_id = coalesce(excluded.company_id, ps.company_id),
    choreographer_id = coalesce(excluded.choreographer_id, ps.choreographer_id),
    work_id = coalesce(excluded.work_id, ps.work_id), program_id = coalesce(excluded.program_id, ps.program_id),
    program_position = coalesce(excluded.program_position, ps.program_position),
    starts_at = excluded.starts_at, time_tbc = excluded.time_tbc, timezone = excluded.timezone,
    ticket_url = coalesce(excluded.ticket_url, ps.ticket_url), image_url = coalesce(excluded.image_url, ps.image_url),
    description = coalesce(excluded.description, ps.description), cancelled = excluded.cancelled,
    last_seen_at = now()
$$;

drop function if exists public.promote_staging_performances(boolean, integer, timestamptz);
create or replace function public.promote_staging_performances(dry_run boolean default false, max_rows integer default null,
  scraped_before timestamptz default null, only_id bigint default null, skip_fuzzy boolean default false)
returns table(staging_id bigint, action text, performance_id uuid, detail text)
language plpgsql set search_path to 'public', 'extensions' as $function$
declare
  r record;
  v_tz text; v_theater uuid; v_comp uuid; v_chor uuid; v_work uuid; v_prog uuid;
  v_ts timestamptz; v_title text; v_tags text[]; v_perf uuid; v_old record; v_notes text[];
  v_local_date date; v_pos int; v_how text; v_tbc boolean; v_city text; v_cands uuid[]; v_strong uuid;
  v_row import_staging_performances;
begin
  for r in select * from import_staging_performances s where s.status = 'pending'
             and (only_id is null or s.id = only_id)
             and (scraped_before is null or s.scraped_at < scraped_before) order by s.id limit max_rows loop
    v_notes := '{}'; v_theater := null; v_comp := null; v_chor := null; v_work := null; v_prog := null; v_perf := null;
    v_old := null; v_how := null; v_cands := null; v_strong := null;
    select * into v_row from import_staging_performances s where s.id = r.id;

    if coalesce(nullif(trim(r.title), ''), nullif(trim(r.work), '')) is null or r.performance_date is null then
      if not dry_run then
        update import_staging_performances s set status = 'rejected', action = 'rejected',
          reject_reason = 'missing title/work or date' where s.id = r.id;
      end if;
      staging_id := r.id; action := 'reject'; performance_id := null; detail := 'missing title/work or date';
      return next; continue;
    end if;

    v_tbc := coalesce(r.time_tbc, false) or coalesce(r.performance_time, '') !~ '^\s*\d{1,2}:\d{2}\s*$';

    -- (a) a copy of a date without venue (or without time) that the same site also gave complete in the same run
    if nullif(trim(coalesce(r.venue, '')), '') is null and exists (
         select 1 from import_staging_performances o
          where o.source_id = r.source_id and o.id <> r.id and o.performance_date = r.performance_date
            and slugify(coalesce(o.title, o.work)) = slugify(coalesce(r.title, r.work))
            and nullif(trim(coalesce(o.venue, '')), '') is not null
            and abs(extract(epoch from (o.scraped_at - r.scraped_at))) < 86400
            and (v_tbc or o.performance_time = r.performance_time)) then
      if not dry_run then
        update import_staging_performances s set status = 'rejected', action = 'rejected',
          reject_reason = 'copy without venue of a date the same site lists with its venue' where s.id = r.id;
      end if;
      staging_id := r.id; action := 'reject'; performance_id := null; detail := 'same-run copy without venue';
      return next; continue;
    end if;

    v_theater := r.theater_id;
    if v_theater is null and r.source_id is not null and nullif(trim(coalesce(r.venue, '')), '') is null then
      select so.theater_id into v_theater from sources so where so.id = r.source_id and is_venue_source(so.kind);
    end if;
    if v_theater is null and nullif(trim(coalesce(r.venue, '')), '') is not null then
      select m.theater_id, m.how into v_theater, v_how from match_theater(r.venue, r.city, r.country) m;
      if v_theater is not null and v_how = 'close name' then
        v_notes := v_notes || ('venue matched by close name: ' || r.venue);
        if not dry_run then
          update theaters t set aliases = array_append(coalesce(t.aliases, '{}'), trim(r.venue))
           where t.id = v_theater and not (trim(r.venue) = any (coalesce(t.aliases, '{}')));
        end if;
      end if;
      if v_theater is null then
        v_notes := v_notes || ('new venue: ' || r.venue);
        if not dry_run then
          insert into theaters (name, location, city, country, timezone)
          values (trim(r.venue), coalesce(nullif(concat_ws(', ', r.city, r.country), ''), 'Unknown'), r.city, r.country, r.timezone)
          returning id into v_theater;
        end if;
      end if;
    end if;

    select coalesce(t.timezone, r.timezone, 'Europe/Paris'), coalesce(t.city, nullif(trim(r.city), ''))
      into v_tz, v_city from (select 1) x left join theaters t on t.id = v_theater;

    v_comp := r.company_id;
    if v_comp is null and r.source_id is not null and nullif(trim(coalesce(r.company, '')), '') is null then
      select so.company_id into v_comp from sources so where so.id = r.source_id;
    end if;
    if v_comp is null and nullif(trim(coalesce(r.company, '')), '') is not null then
      select c.id into v_comp from companies c
       where slugify(c.name) = slugify(r.company) or slugify(r.company) = any (select slugify(a) from unnest(coalesce(c.aliases, '{}')) a)
       order by c.created_at limit 1;
      if v_comp is null then
        v_notes := v_notes || ('new company: ' || r.company);
        if not dry_run then
          insert into companies (name, location, city, country)
          values (trim(r.company), coalesce(nullif(concat_ws(', ', r.city, r.country), ''), 'Unknown'), r.city, r.country)
          returning id into v_comp;
        end if;
      end if;
    end if;

    v_chor := r.choreographer_id;
    if v_chor is null and nullif(trim(coalesce(r.choreographer, '')), '') is not null then
      select c.id into v_chor from choreographers c
       where slugify(c.name) = slugify(r.choreographer) or slugify(r.choreographer) = any (select slugify(a) from unnest(coalesce(c.aliases, '{}')) a)
       order by c.created_at limit 1;
      if v_chor is null then
        v_notes := v_notes || ('new choreographer: ' || r.choreographer);
        if not dry_run then
          insert into choreographers (name, nationality)
          values (trim(r.choreographer), coalesce(nullif(trim(r.choreographer_nationality), ''), 'Unknown'))
          returning id into v_chor;
        end if;
      end if;
    end if;

    v_work := r.work_id;
    if v_work is null and nullif(trim(coalesce(r.work, '')), '') is not null then
      if r.work_is_story then
        select w.id into v_work from works w where slugify(w.title) = slugify(r.work) and w.choreographer_id is null
         order by w.created_at limit 1;
      else
        select w.id into v_work from works w
         where slugify(w.title) = slugify(r.work) and (v_chor is null or w.choreographer_id = v_chor or w.choreographer_id is null)
         order by (w.choreographer_id = v_chor) desc nulls last, w.created_at limit 1;
      end if;
      if v_work is null then
        v_notes := v_notes || ('new work: ' || r.work);
        if not dry_run then
          insert into works (title, choreographer_id, tags)
          values (trim(r.work), case when r.work_is_story then null else v_chor end,
                  coalesce(r.tags, case when r.genre is not null then array[r.genre] end))
          returning id into v_work;
        end if;
      end if;
    end if;

    if nullif(trim(coalesce(r.program, '')), '') is not null then
      select g.id into v_prog from programs g
       where slugify(g.title) = slugify(r.program) and (g.company_id is not distinct from v_comp or v_comp is null)
       order by g.created_at limit 1;
      if v_prog is null then
        v_notes := v_notes || ('new program: ' || r.program);
        if not dry_run then
          insert into programs (title, company_id) values (trim(r.program), v_comp) returning id into v_prog;
        end if;
      end if;
    end if;

    v_pos := coalesce(r.program_position, 1);
    v_ts := ((r.performance_date::timestamp
              + coalesce(case when v_tbc then null else trim(r.performance_time)::time end, time '00:00'))
             at time zone v_tz)
            + make_interval(secs => case when r.program is not null then v_pos - 1 else 0 end);
    v_local_date := r.performance_date;

    v_title := coalesce(nullif(trim(r.title), ''), trim(r.work));
    if r.program is not null and v_title not like '%(' || trim(r.program) || ')' then
      v_title := v_title || ' (' || trim(r.program) || ')';
    end if;

    v_tags := coalesce(r.tags, case when r.genre is not null then array[r.genre] else '{}'::text[] end);
    if v_tbc then v_tags := array_append(array_remove(v_tags, 'Time TBC'), 'Time TBC'); end if;
    if r.cancelled then v_tags := array_append(array_remove(v_tags, 'Cancelled'), 'Cancelled'); end if;

    -- 1. same venue, same start, same work or title
    select p.* into v_old from performances p
     where v_theater is not null and p.theater_id = v_theater and p.performance_date = v_ts
       and ((v_work is not null and p.work_id = v_work) or slugify(p.title) = slugify(v_title))
     limit 1;
    -- 2. a time is now known for a show saved without one
    if v_old.id is null and not v_tbc then
      select p.* into v_old from performances p
       where v_theater is not null and p.theater_id = v_theater
         and (p.performance_date at time zone v_tz)::date = v_local_date
         and 'Time TBC' = any(coalesce(p.tags, '{}'))
         and ((v_work is not null and p.work_id = v_work) or slugify(p.title) = slugify(v_title))
       limit 1;
    end if;
    -- 3. same venue and start, slightly different title (company site vs venue site)
    if v_old.id is null and v_theater is not null then
      select p.* into v_old from performances p
       where p.theater_id = v_theater and p.performance_date = v_ts
         and (   (v_comp is not null and p.company_id = v_comp and similarity(slugify(p.title), slugify(v_title)) >= 0.3)
              or similarity(slugify(p.title), slugify(v_title)) >= 0.6
              or (length(slugify(r.title)) >= 5 and slugify(p.title) like '%' || slugify(r.title) || '%')
              or (length(slugify(p.title)) >= 5 and slugify(v_title) like '%' || slugify(p.title) || '%'))
       order by similarity(slugify(p.title), slugify(v_title)) desc
       limit 1;
      if v_old.id is not null then v_notes := v_notes || ('same show under another title: ' || v_old.title); end if;
    end if;
    -- 4. one of the two sites gives no venue: same company, same start, similar title
    if v_old.id is null and v_comp is not null then
      select p.* into v_old from performances p
       where p.company_id = v_comp and p.performance_date = v_ts
         and ((v_theater is null) <> (p.theater_id is null))
         and (   similarity(slugify(p.title), slugify(v_title)) >= 0.3
              or (length(slugify(coalesce(r.title, ''))) >= 5 and slugify(p.title) like '%' || slugify(r.title) || '%')
              or (length(slugify(p.title)) >= 5 and slugify(v_title) like '%' || slugify(p.title) || '%')
              or (v_work is not null and p.work_id = v_work))
       order by similarity(slugify(p.title), slugify(v_title)) desc
       limit 1;
      if v_old.id is not null then v_notes := v_notes || 'matched the same show where one site gives no venue'::text; end if;
    end if;
    -- 5. same show, same city and day, under another venue name or a slightly different time (Afanador)
    if v_old.id is null and v_city is not null and not skip_fuzzy then
      select array_agg(c.id order by c.strong desc, c.sim desc), (array_agg(c.id order by c.sim desc) filter (where c.strong))[1]
        into v_cands, v_strong
        from (select p.id,
                     similarity(slugify(regexp_replace(p.title, '\s*\(.*\)\s*$', '')), slugify(regexp_replace(v_title, '\s*\(.*\)\s*$', ''))) sim,
                     ((v_comp is not null and p.company_id = v_comp) or (v_work is not null and p.work_id = v_work))
                     and (title_close(p.title, v_title) or (v_work is not null and p.work_id = v_work))
                     and (v_tbc or 'Time TBC' = any(coalesce(p.tags, '{}'))
                          or abs(extract(epoch from (p.performance_date - v_ts))) <= 900) as strong
                from performances p left join theaters t on t.id = p.theater_id
               where p.performance_date between v_ts - interval '1 day' and v_ts + interval '1 day'
                 and (p.performance_date at time zone coalesce(t.timezone, p.timezone, v_tz))::date = v_local_date
                 and lower(coalesce(t.city, p.city, '')) = lower(v_city)
                 and not (p.theater_id is not distinct from v_theater and not v_tbc
                          and not ('Time TBC' = any(coalesce(p.tags, '{}'))))   -- other sessions at the same venue are not this show
                 and (   (v_comp is not null and p.company_id = v_comp) or (v_work is not null and p.work_id = v_work)
                      or similarity(slugify(p.title), slugify(v_title)) >= 0.5
                      or (length(slugify(coalesce(r.title, ''))) >= 5 and slugify(p.title) like '%' || slugify(r.title) || '%'))
                 and (v_tbc or 'Time TBC' = any(coalesce(p.tags, '{}')) or abs(extract(epoch from (p.performance_date - v_ts))) <= 5400)
             ) c;
      if v_strong is not null and cardinality(v_cands) = 1 then
        select p.* into v_old from performances p where p.id = v_strong;
        v_notes := v_notes || ('same show in the same city and day under another venue or time: ' || v_old.title);
      elsif v_cands is not null then
        -- (c) not sure: hold for review rather than publish a possible second row
        if not dry_run then
          update import_staging_performances s set status = 'held', action = 'held for review',
            reject_reason = 'possible duplicate of ' || array_to_string(v_cands, ', ') where s.id = r.id;
          insert into match_review (staging_id, candidates, reason)
          values (r.id, v_cands, case when v_strong is not null then 'several strong matches' else 'possible match in same city and day' end)
          on conflict on constraint match_review_staging_id_key do nothing;
        end if;
        staging_id := r.id; action := 'hold'; performance_id := v_cands[1];
        detail := 'possible duplicate, held for review'; return next; continue;
      end if;
    end if;

    -- a site giving no time for a show that already has one: just record the listing
    if v_old.id is null and v_tbc then
      select p.* into v_old from performances p
       where v_theater is not null and p.theater_id = v_theater
         and (p.performance_date at time zone v_tz)::date = v_local_date
         and not ('Time TBC' = any(coalesce(p.tags, '{}')))
         and ((v_work is not null and p.work_id = v_work) or slugify(p.title) = slugify(v_title))
       limit 1;
    end if;

    if v_old.id is not null then
      if not dry_run then
        update performances p set
          original_title = coalesce(p.original_title, nullif(trim(r.original_title), '')),
          work_id = coalesce(p.work_id, v_work), choreographer_id = coalesce(p.choreographer_id, v_chor),
          company_id = coalesce(p.company_id, v_comp), program_id = coalesce(p.program_id, v_prog),
          city = coalesce(p.city, nullif(trim(r.city), '')), country = coalesce(p.country, nullif(trim(r.country), '')),
          date_source = coalesce(p.date_source, r.date_source), timezone = coalesce(p.timezone, v_tz),
          last_seen_at = now()
        where p.id = v_old.id;
        perform upsert_listing(v_old.id, v_row, v_title, v_theater, v_comp, v_chor, v_work, v_prog, v_ts, v_tbc, v_tz);
        perform recompute_performance(v_old.id);
        update import_staging_performances s set status = 'promoted', action = 'listed on existing',
          performance_id = v_old.id, promoted_at = now() where s.id = r.id;
      end if;
      staging_id := r.id;
      action := case when v_old.performance_date <> v_ts and not v_tbc then 'retime' else 'update' end;
      performance_id := v_old.id; detail := array_to_string(v_notes, '; ');
      return next;
    else
      if not dry_run then
        insert into performances (title, original_title, work_id, choreographer_id, company_id, theater_id,
                                  performance_date, tags, ticket_url, image_url, description,
                                  program_id, program_position, source_id, last_seen_at, date_source, city, country, timezone)
        values (v_title, nullif(trim(r.original_title), ''), v_work, v_chor, v_comp, v_theater,
                v_ts, (select array_agg(distinct x) from unnest(v_tags) x), nullif(trim(r.tickets_url), ''),
                nullif(trim(r.image_url), ''), nullif(trim(r.description), ''),
                v_prog, r.program_position, r.source_id, now(), r.date_source, nullif(trim(r.city), ''), nullif(trim(r.country), ''), v_tz)
        returning id into v_perf;
        perform upsert_listing(v_perf, v_row, v_title, v_theater, v_comp, v_chor, v_work, v_prog, v_ts, v_tbc, v_tz);
        update import_staging_performances s set status = 'promoted', action = 'inserted',
          performance_id = v_perf, promoted_at = now() where s.id = r.id;
      end if;
      staging_id := r.id; action := 'insert'; performance_id := v_perf; detail := array_to_string(v_notes, '; ');
      return next;
    end if;
  end loop;

  if not dry_run then
    update sources so set last_collected_at = now(), status = case when so.status = 'todo' then 'ok' else so.status end
     where so.id in (select s.source_id from import_staging_performances s where s.promoted_at > now() - interval '1 minute');
  end if;
end;
$function$;

-- A held staging row after review: 'same' attaches it to the chosen performance, 'different' publishes it as new.
create or replace function public.resolve_match(p_staging bigint, p_verdict text, p_chosen uuid default null, p_note text default null)
returns uuid language plpgsql set search_path to 'public', 'extensions' as $$
declare v_perf uuid;
begin
  if not exists (select 1 from import_staging_performances where id = p_staging and status = 'held') then return null; end if;
  update match_review set status = p_verdict, chosen = p_chosen, review_note = p_note, decided_at = now()
   where staging_id = p_staging;
  update import_staging_performances set status = 'pending', reject_reason = null where id = p_staging;
  -- publish it, skipping the fuzzy step (a reviewer has decided)
  perform 1 from promote_staging_performances(false, null, null, p_staging, true);
  select performance_id into v_perf from import_staging_performances where id = p_staging;
  if p_verdict = 'same' and p_chosen is not null and v_perf is not null and v_perf <> p_chosen then
    perform merge_performances(v_perf, p_chosen, coalesce(p_note, 'reviewed: same show'));
    v_perf := p_chosen;
  end if;
  return v_perf;
end $$;
