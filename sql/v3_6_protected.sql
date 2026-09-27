-- Saffitt v3, part 6: protected artists. A show by a protected choreographer, or by a tier 0 company, is never
-- removed as out of scope or as a school show (duplicate merges still keep one copy of it).
alter table public.choreographers add column if not exists protected boolean not null default false;
comment on column public.choreographers.protected is 'Never removed by scope rules (e.g. Marcos Morau: flamenco-inspired work stays).';
update public.choreographers set protected = true where name ~* 'marcos morau';

create or replace function public.is_protected_performance(p_id uuid) returns boolean
language sql stable set search_path to 'public' as $$
  select exists (
    select 1 from performances p
      left join choreographers c on c.id = p.choreographer_id
      left join companies co on co.id = p.company_id
     where p.id = p_id and (
           coalesce(c.protected, false)
        or exists (select 1 from sources s where s.priority = 0 and s.kind ~* 'company'
                     and (s.company_id = p.company_id
                          or (c.name is not null and s.name ilike '%' || c.name || '%')
                          or (co.name is not null and s.name ilike '%' || co.name || '%')))))
$$;

-- Scope removals skip protected shows: they are set back to 'rejected' with a note instead.
create or replace function public.apply_duplicate_review(p_run text)
returns table(verdict text, removed integer) language plpgsql as $function$
begin
  update duplicate_review r set status = 'rejected', decided_at = now(),
         review_note = coalesce(r.review_note || ' ', '') || '[protected artist: kept]'
   where r.review_run = p_run and r.status = 'approved' and r.verdict in ('out_of_scope', 'school_show')
     and is_protected_performance(r.drop_id);
  update duplicate_review r set drop_snapshot = to_jsonb(pl) from performance_listing pl
   where pl.id = r.drop_id and r.review_run = p_run and r.drop_snapshot is null;
  update performances k set
    ticket_url = coalesce(k.ticket_url, d.ticket_url),
    image_url = coalesce(k.image_url, d.image_url),
    description = coalesce(k.description, d.description),
    original_title = coalesce(k.original_title, d.original_title)
  from duplicate_review r join performances d on d.id = r.drop_id
  where r.review_run = p_run and r.status = 'approved' and r.keep_id = k.id;
  return query
  with del as (
    delete from performances p using duplicate_review r
    where r.review_run = p_run and r.status = 'approved' and p.id = r.drop_id
    returning r.verdict)
  select del.verdict, count(*)::int from del group by 1;
  update duplicate_review set status = 'applied', decided_at = now() where review_run = p_run and status = 'approved';
end $function$;
revoke execute on function public.apply_duplicate_review(text) from anon, authenticated;

-- (v3_6b) the API gateway refuses a DELETE without WHERE: recompute_performance clears its scratch table with "where true".
-- (v3_6c) one clear same-time match among other sessions of the day is taken as the match instead of being held.
do $$
declare d text;
begin
  d := pg_get_functiondef('public.promote_staging_performances(boolean, integer, timestamptz, bigint, boolean)'::regprocedure);
  if position('v_nstrong' in d) > 0 then return; end if;
  d := replace(d, 'v_strong uuid;', 'v_strong uuid; v_nstrong int;');
  d := replace(d, 'into v_cands, v_strong', ', count(*) filter (where c.strong) into v_cands, v_strong, v_nstrong');
  d := replace(d, 'if v_strong is not null and cardinality(v_cands) = 1 then',
                  'if v_strong is not null and (cardinality(v_cands) = 1 or v_nstrong = 1) then   -- one clear match among other sessions');
  execute d;
end $$;
