-- Saffitt v3, part 5: the review step between collecting and publishing (B).
-- review.py publishes staging rows, finds possible duplicates, and asks Claude about every uncertain case
-- through review_queue(); verdicts are applied with apply_review(). Low-confidence answers stay open for a person.

-- 1. Evidence now carries each listing's id, so a reviewer can say which site is wrong.
create or replace function public.performance_evidence(p_id uuid) returns jsonb
language sql stable set search_path to 'public' as $$
  select jsonb_build_object(
    'id', pl.id, 'title', pl.title, 'original_title', pl.original_title, 'date', pl.local_date, 'time', pl.local_time,
    'time_tbc', pl.time_tbc, 'venue', pl.theater_name, 'city', pl.city, 'country', pl.country,
    'company', pl.company_name, 'choreographer', pl.choreographer_name, 'program', pl.program_title, 'ticket_url', pl.ticket_url,
    'listed_by', (select jsonb_agg(jsonb_build_object(
        'listing_id', ps.id, 'source', s.name, 'source_kind', s.kind, 'tier', s.priority,
        'rank', source_rank(ps.source_id, ps.theater_id, coalesce(ps.company_id, pl.company_id)),
        'listed_title', coalesce(ps.listed_title, ps.title), 'listed_venue', coalesce(ps.listed_venue, t.name),
        'time', to_char(ps.starts_at at time zone coalesce(ps.timezone, pl.timezone), 'HH24:MI'), 'time_tbc', ps.time_tbc,
        'url', ps.ticket_url, 'site', s.website, 'last_seen', ps.last_seen_at::date) order by ps.id)
      from performance_sources ps left join sources s on s.id = ps.source_id left join theaters t on t.id = ps.theater_id
      where ps.performance_id = pl.id and not ps.ignored))
  from performance_listing pl where pl.id = p_id
$$;

-- 2. Precedence with stable tie-breaks, and a conflict a reviewer dismissed is not reopened while nothing changed.
create or replace function public.recompute_performance(p_id uuid) returns void
language plpgsql set search_path to 'public', 'extensions' as $$
declare
  v record; w_theater uuid; w_when record; w_ticket text; w_credit record; w_img text; w_desc text; w_comp uuid;
  w_cancel boolean; v_tags text[]; v_diff record; v_vd jsonb;
begin
  select * into v from performances where id = p_id;
  if v.id is null then return; end if;
  create temp table if not exists _rk (like performance_sources including defaults) on commit drop;
  alter table _rk add column if not exists rk int;
  delete from _rk;
  insert into _rk select ps.*, source_rank(ps.source_id, ps.theater_id, coalesce(ps.company_id, v.company_id))
    from performance_sources ps where ps.performance_id = p_id and not ps.ignored;
  if not exists (select 1 from _rk) then return; end if;

  select theater_id into w_theater from _rk where theater_id is not null
    order by rk, (theater_id = v.theater_id) desc, last_seen_at desc limit 1;
  select starts_at, time_tbc, timezone into w_when from _rk where starts_at is not null
    order by time_tbc, rk, (starts_at = v.performance_date) desc, last_seen_at desc limit 1;
  select ticket_url into w_ticket from _rk where ticket_url is not null
    order by rk, (ticket_url = v.ticket_url) desc, last_seen_at desc limit 1;
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
  select jsonb_agg(jsonb_build_object('source', s.name, 'rank', r.rk, 'time', to_char(r.starts_at at time zone coalesce(r.timezone, 'UTC'), 'YYYY-MM-DD HH24:MI'), 'url', r.ticket_url) order by r.id)
    as d, max(abs(extract(epoch from (r.starts_at - w_when.starts_at)))) as gap
    into v_diff
    from _rk r left join sources s on s.id = r.source_id
   where r.rk <= 3 and not r.time_tbc and w_when.starts_at is not null and not coalesce(w_when.time_tbc, false);
  if coalesce(v_diff.gap, 0) > 900 and not exists (select 1 from performance_conflicts c where c.performance_id = p_id
       and c.field = 'time' and c.status = 'dismissed' and c.detail = v_diff.d) then
    insert into performance_conflicts (performance_id, field, detail) values (p_id, 'time', v_diff.d) on conflict do nothing;
  end if;
  if exists (select 1 from _rk r where r.rk <= 3 and r.theater_id is not null and r.theater_id <> w_theater) then
    select jsonb_agg(jsonb_build_object('source', s.name, 'rank', r.rk, 'venue', t.name, 'url', r.ticket_url) order by r.id)
      into v_vd
      from _rk r left join sources s on s.id = r.source_id left join theaters t on t.id = r.theater_id
     where r.theater_id is not null;
    if not exists (select 1 from performance_conflicts c where c.performance_id = p_id
                     and c.field = 'venue' and c.status = 'dismissed' and c.detail = v_vd) then
      insert into performance_conflicts (performance_id, field, detail) values (p_id, 'venue', v_vd) on conflict do nothing;
    end if;
  end if;
end $$;

-- 3. Housekeeping: held rows for past dates expire; pairs whose rows are gone are closed.
create or replace function public.review_housekeeping() returns jsonb
language plpgsql set search_path to 'public' as $$
declare a int; b int; c int;
begin
  update match_review m set status = 'expired', decided_at = now()
    from import_staging_performances s
   where s.id = m.staging_id and m.status = 'open' and (s.performance_date < current_date or s.status <> 'held');
  get diagnostics a = row_count;
  update import_staging_performances s set status = 'rejected', action = 'rejected', reject_reason = 'held row expired'
   where s.status = 'held' and s.performance_date < current_date;
  update duplicate_review r set status = 'rejected', decided_at = now(), review_note = coalesce(r.review_note, 'row no longer exists')
   where r.verdict = 'candidate' and r.status = 'proposed'
     and (not exists (select 1 from performances p where p.id = r.drop_id) or not exists (select 1 from performances p where p.id = r.keep_id));
  get diagnostics b = row_count;
  update performance_conflicts c set status = 'dismissed', resolved_at = now(), note = coalesce(c.note, 'performance is past')
   where c.status = 'open' and exists (select 1 from performances p where p.id = c.performance_id and p.performance_date < now());
  get diagnostics c = row_count;
  return jsonb_build_object('held_expired', a, 'pairs_closed', b, 'conflicts_past', c);
end $$;

-- 4. Everything waiting for a verdict, with its evidence. Items a reviewer already answered "unsure" on the
--    same day are skipped, so one run does not ask twice.
create or replace function public.review_queue(p_limit int default 200) returns jsonb
language sql stable set search_path to 'public' as $$
  select coalesce(jsonb_agg(q.x), '[]'::jsonb) from (
    (select jsonb_build_object('kind', 'held', 'id', m.staging_id, 'reason', m.reason,
        'new_listing', jsonb_build_object('source', so.name, 'source_kind', so.kind, 'tier', so.priority,
            'site', so.website, 'title', s.title, 'work', s.work, 'venue', s.venue, 'city', s.city, 'country', s.country,
            'date', s.performance_date, 'time', s.performance_time, 'time_tbc', s.time_tbc, 'company', s.company,
            'choreographer', s.choreographer, 'program', s.program, 'url', s.tickets_url),
        'candidates', (select jsonb_agg(performance_evidence(c)) from unnest(m.candidates) c
                        where exists (select 1 from performances p where p.id = c))) x
       from match_review m join import_staging_performances s on s.id = m.staging_id left join sources so on so.id = s.source_id
      where m.status = 'open' and s.status = 'held' and coalesce(m.review_note, '') not like 'unsure ' || current_date || '%'
      order by s.performance_date, m.id limit p_limit)
    union all
    (select jsonb_build_object('kind', 'pair', 'id', r.id, 'reason', r.reason,
        'a', performance_evidence(r.keep_id), 'b', performance_evidence(r.drop_id)) x
       from duplicate_review r
      where r.verdict = 'candidate' and r.status = 'proposed'
        and coalesce(r.review_note, '') not like 'unsure ' || current_date || '%'
        and exists (select 1 from performances p where p.id = r.drop_id)
        and exists (select 1 from performances p where p.id = r.keep_id)
      order by r.id limit p_limit)
    union all
    (select jsonb_build_object('kind', 'conflict', 'id', c.id, 'field', c.field, 'performance', performance_evidence(c.performance_id)) x
       from performance_conflicts c
      where c.status = 'open' and coalesce(c.note, '') not like 'unsure ' || current_date || '%'
      order by c.id limit p_limit)
  ) q
$$;

-- 5. Apply one verdict.
--    held:     same (p_keep = the candidate it is) | different | unsure
--    pair:     same (p_keep = the row to keep)      | different | unsure
--    conflict: ignore_listings (p_ignore = listing ids that are wrong) | dismiss | unsure
create or replace function public.apply_review(p_kind text, p_id bigint, p_verdict text, p_keep uuid default null,
  p_ignore bigint[] default null, p_note text default null) returns text
language plpgsql set search_path to 'public', 'extensions' as $$
declare r record; v_drop uuid; v_perf uuid; v_note text := left(coalesce(p_note, ''), 2000);
begin
  if p_verdict = 'unsure' then
    if p_kind = 'held' then update match_review set review_note = 'unsure ' || current_date || ': ' || v_note where staging_id = p_id;
    elsif p_kind = 'pair' then update duplicate_review set review_note = 'unsure ' || current_date || ': ' || v_note where id = p_id;
    elsif p_kind = 'conflict' then update performance_conflicts set note = 'unsure ' || current_date || ': ' || v_note where id = p_id;
    end if;
    return 'left for a person';
  end if;

  if p_kind = 'held' then
    select * into r from match_review where staging_id = p_id and status = 'open';
    if r.id is null then return 'not open'; end if;
    if p_verdict = 'same' then
      if p_keep is null or not (p_keep = any (r.candidates)) then return 'keep is not a candidate'; end if;
      v_perf := resolve_match(p_id, 'same', p_keep, v_note);
      return 'listed on ' || coalesce(v_perf::text, '?');
    elsif p_verdict = 'different' then
      v_perf := resolve_match(p_id, 'different', null, v_note);
      return 'published as ' || coalesce(v_perf::text, '?');
    end if;

  elsif p_kind = 'pair' then
    select * into r from duplicate_review where id = p_id and verdict = 'candidate' and status = 'proposed';
    if r.id is null then return 'not open'; end if;
    if p_verdict = 'same' then
      if p_keep is null or p_keep not in (r.keep_id, r.drop_id) then return 'keep is not one of the pair'; end if;
      v_drop := case when p_keep = r.keep_id then r.drop_id else r.keep_id end;
      update duplicate_review set verdict = 'duplicate', status = 'applied', decided_at = now(),
        keep_id = p_keep, drop_id = v_drop, review_note = v_note,
        drop_snapshot = (select to_jsonb(pl) from performance_listing pl where pl.id = v_drop)
       where id = p_id;
      perform merge_performances(v_drop, p_keep, 'review: ' || v_note);
      return 'merged into ' || p_keep;
    elsif p_verdict = 'different' then
      update duplicate_review set verdict = 'not_duplicate', status = 'applied', decided_at = now(), review_note = v_note where id = p_id;
      return 'kept both';
    end if;

  elsif p_kind = 'conflict' then
    select * into r from performance_conflicts where id = p_id and status = 'open';
    if r.id is null then return 'not open'; end if;
    if p_verdict = 'ignore_listings' then
      if p_ignore is null or cardinality(p_ignore) = 0 then return 'no listing named'; end if;
      update performance_sources set ignored = true, ignored_reason = v_note
       where performance_id = r.performance_id and id = any (p_ignore);
      update performance_conflicts set status = 'resolved', resolved_at = now(), note = v_note where id = p_id;
      perform recompute_performance(r.performance_id);
      return 'ignored ' || cardinality(p_ignore) || ' listing(s)';
    elsif p_verdict = 'dismiss' then
      update performance_conflicts set status = 'dismissed', resolved_at = now(), note = v_note where id = p_id;
      return 'dismissed';
    end if;
  end if;
  return 'unknown verdict';
end $$;

revoke execute on function public.review_housekeeping() from anon, authenticated;
revoke execute on function public.review_queue(int) from anon, authenticated;
revoke execute on function public.apply_review(text, bigint, text, uuid, bigint[], text) from anon, authenticated;
revoke execute on function public.find_duplicate_candidates(text, int) from anon, authenticated;
revoke execute on function public.merge_performances(uuid, uuid, text) from anon, authenticated;
revoke execute on function public.recompute_performance(uuid) from anon, authenticated;

-- 6. Coverage of a site counts what it lists (any listing), what plays on its stages, and its company's shows.
create or replace view public.source_coverage as
select s.id as source_id, s.name, s.priority, s.status,
  count(distinct p.id) filter (where exists (select 1 from performance_sources ps where ps.performance_id = p.id and ps.source_id = s.id)) as by_source,
  count(distinct p.id) filter (where p.theater_id in (select v.theater_id from source_venues v where v.source_id = s.id)) as at_venue,
  count(distinct p.id) filter (where s.company_id is not null and p.company_id = s.company_id) as by_company,
  count(distinct p.id) as total,
  count(distinct p.id) filter (where p.source_id is distinct from s.id) as credited_elsewhere
from sources s
left join performances p on p.performance_date >= now() and (
     exists (select 1 from performance_sources ps where ps.performance_id = p.id and ps.source_id = s.id)
  or p.theater_id in (select v.theater_id from source_venues v where v.source_id = s.id)
  or (s.company_id is not null and p.company_id = s.company_id))
group by s.id;

-- 7. Faster pair search (a few seconds instead of half a minute), and a pair already waiting for review is not
--    queued again by the next run.
create or replace function public.find_duplicate_candidates(p_run text, p_days int default 400)
returns int language plpgsql set search_path to 'public', 'extensions' as $$
declare n int;
begin
  drop table if exists _fp;
  create temp table _fp on commit drop as
    select p.id, (p.performance_date at time zone coalesce(t.timezone, p.timezone, 'Europe/Paris'))::date d,
           (p.performance_date at time zone coalesce(t.timezone, p.timezone, 'Europe/Paris'))::time tm,
           'Time TBC' = any (coalesce(p.tags, '{}')) tbc, lower(coalesce(t.city, p.city, '?')) city, p.theater_id th,
           p.company_id co, p.work_id w, p.program_id prog, slugify(regexp_replace(p.title, '\s*\(.*\)\s*$', '')) nt
      from performances p left join theaters t on t.id = p.theater_id
     where p.performance_date >= current_date - 1 and p.performance_date < current_date + p_days + 1;
  delete from _fp where d < current_date or d >= current_date + p_days;
  create index on _fp (d, city);
  analyze _fp;
  with sess as (   -- a show given three or more times that day at one venue: its sessions are not duplicates
    select th, d, nt from _fp where th is not null group by 1, 2, 3 having count(*) >= 3),
  pairs as (
    select a.id a_id, b.id b_id, a.th, a.d, a.nt, a.th = b.th same_venue, a.tbc or b.tbc one_tbc,
           case when a.tbc or b.tbc then 0 else abs(extract(epoch from (a.tm - b.tm))) / 60 end gap_min
      from _fp a join _fp b on a.id < b.id and a.d = b.d and a.city = b.city
     where (a.prog is null or b.prog is null or a.prog <> b.prog)
       and ((a.co = b.co and a.co is not null) or a.w = b.w or a.nt = b.nt or similarity(a.nt, b.nt) > 0.55
            or (length(a.nt) >= 5 and b.nt like '%' || a.nt || '%') or (length(b.nt) >= 5 and a.nt like '%' || b.nt || '%'))
       and (a.tbc or b.tbc or abs(extract(epoch from (a.tm - b.tm))) <= 5400
            or (a.th = b.th and abs(extract(epoch from (a.tm - b.tm))) in (3600, 7200))))   -- UTC copies
  insert into duplicate_review (review_run, drop_id, keep_id, reason, verdict)
  select p_run, x.b_id, x.a_id,
         case when x.one_tbc then 'same day, one without time'
              when not coalesce(x.same_venue, false) then 'same day, different or missing venue, start ' || round(x.gap_min) || ' min apart'
              when x.gap_min in (60, 120) then 'same venue, starts exactly ' || round(x.gap_min) || ' min apart (time zone copy?)'
              else 'same venue, start ' || round(x.gap_min) || ' min apart' end,
         'candidate'
    from pairs x
   where not (coalesce(x.same_venue, false) and not x.one_tbc
              and exists (select 1 from sess s where s.th = x.th and s.d = x.d and s.nt = x.nt))
     and not exists (select 1 from duplicate_review r
                      where ((r.drop_id = x.b_id and r.keep_id = x.a_id) or (r.drop_id = x.a_id and r.keep_id = x.b_id))
                        and (r.verdict = 'not_duplicate' or (r.verdict = 'candidate' and r.status = 'proposed')))
  on conflict do nothing;
  get diagnostics n = row_count;
  return n;
end $$;
revoke execute on function public.find_duplicate_candidates(text, int) from anon, authenticated;

-- 8. A known stage's own time zone wins over the one read with the date (a Lisbon company's Madrid dates were
--    read with Lisbon time). Matadero had been created with Lisbon's zone.
do $$
declare d text;
begin
  d := pg_get_functiondef('public.promote_staging_performances(boolean, integer, timestamptz, bigint, boolean)'::regprocedure);
  if position('coalesce(r.timezone, t.timezone, ''Europe/Paris'')' in d) = 0 then raise exception 'pattern not found'; end if;
  execute replace(d, 'coalesce(r.timezone, t.timezone, ''Europe/Paris'')', 'coalesce(t.timezone, r.timezone, ''Europe/Paris'')');
end $$;
update public.theaters set timezone = 'Europe/Madrid' where name = 'Centro de Danza Matadero' and city = 'Madrid' and timezone = 'Europe/Lisbon';

-- 9. (applied as v3_5d) exact 1 or 2 hour twins at one venue are candidates too: that is how UTC copies look.
