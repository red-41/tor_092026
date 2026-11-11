-- v3_15 (31 Oct 2026): the reviewer stops paying for the same doubts every run.
--  * A case the reviewer answered "unsure" waits 14 days for a person before it is asked again (was: one day).
--  * Pairs whose two rows are both in the past are closed by housekeeping and never sent to the reviewer.

create or replace function public.unsure_recently(p_note text, p_days int default 14) returns boolean
language sql stable set search_path to 'public' as $$
  select coalesce(substring(p_note from '^unsure (\d{4}-\d{2}-\d{2})')::date > current_date - p_days, false)
$$;

create or replace function public.review_housekeeping() returns jsonb
language plpgsql set search_path to 'public' as $$
declare a int; b int; c int; d int;
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
  update duplicate_review r set status = 'rejected', decided_at = now(), review_note = coalesce(r.review_note, '') || ' [closed: both shows are past]'
   where r.verdict = 'candidate' and r.status = 'proposed'
     and not exists (select 1 from performances p where p.id in (r.keep_id, r.drop_id) and p.performance_date >= current_date);
  get diagnostics d = row_count;
  update performance_conflicts c set status = 'dismissed', resolved_at = now(), note = coalesce(c.note, 'performance is past')
   where c.status = 'open' and exists (select 1 from performances p where p.id = c.performance_id and p.performance_date < now());
  get diagnostics c = row_count;
  return jsonb_build_object('held_expired', a, 'pairs_closed', b + d, 'conflicts_past', c);
end $$;

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
      where m.status = 'open' and s.status = 'held' and s.performance_date >= current_date
        and not unsure_recently(m.review_note)
      order by s.performance_date, m.id limit p_limit)
    union all
    (select jsonb_build_object('kind', 'pair', 'id', r.id, 'reason', r.reason,
        'a', performance_evidence(r.keep_id), 'b', performance_evidence(r.drop_id)) x
       from duplicate_review r
      where r.verdict = 'candidate' and r.status = 'proposed'
        and not unsure_recently(r.review_note)
        and exists (select 1 from performances p where p.id = r.drop_id)
        and exists (select 1 from performances p where p.id = r.keep_id)
        and exists (select 1 from performances p where p.id in (r.keep_id, r.drop_id) and p.performance_date >= current_date)
      order by r.id limit p_limit)
    union all
    (select jsonb_build_object('kind', 'conflict', 'id', c.id, 'field', c.field, 'performance', performance_evidence(c.performance_id)) x
       from performance_conflicts c
      where c.status = 'open' and not unsure_recently(c.note)
      order by c.id limit p_limit)
  ) q
$$;

revoke all on function public.unsure_recently(text, int) from public, anon, authenticated;
