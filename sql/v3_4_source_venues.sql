-- Saffitt v3, part 4: which stages a venue website speaks for, kept up to date; stable tie-breaks in precedence.

-- A venue site speaks for a stage when (1) the stage carries the site's name, or (2) the stage is in one of the
-- site's cities ("Antwerp / Ghent" means both) and no other site already speaks for it. When two sites qualify
-- for the same stage, the one that lists most shows there gets it. Rows added by hand (added_by <> 'auto') are kept.
create or replace function public.refresh_source_venues() returns int
language plpgsql set search_path to 'public', 'extensions' as $$
declare n int := 0; k int;
begin
  insert into source_venues (source_id, theater_id, added_by)
  select distinct ps.source_id, ps.theater_id, 'auto'
    from performance_sources ps join sources s on s.id = ps.source_id join theaters t on t.id = ps.theater_id
   where is_venue_source(s.kind)
     and length(slugify(split_part(s.name, ' (', 1))) >= 4
     and slugify(t.name) like '%' || slugify(split_part(s.name, ' (', 1)) || '%'
  on conflict do nothing;
  get diagnostics k = row_count; n := n + k;

  insert into source_venues (source_id, theater_id, added_by)
  select distinct on (x.theater_id) x.source_id, x.theater_id, 'auto'
    from (select ps.source_id, ps.theater_id, count(*) cnt
            from performance_sources ps join sources s on s.id = ps.source_id join theaters t on t.id = ps.theater_id
           where is_venue_source(s.kind) and t.city is not null and s.city is not null
             and lower(unaccent_fallback(t.city)) in
                 (select lower(unaccent_fallback(trim(c))) from regexp_split_to_table(s.city, '\s*/\s*') c)
           group by 1, 2) x
   where not exists (select 1 from source_venues v where v.theater_id = x.theater_id)
   order by x.theater_id, x.cnt desc
  on conflict do nothing;
  get diagnostics k = row_count; n := n + k;
  return n;
end $$;
revoke execute on function public.refresh_source_venues() from anon, authenticated;

-- City spelled differently on the site and the stage.
insert into source_venues (source_id, theater_id, added_by)
select s.id, t.id, 'manual' from sources s, theaters t
 where s.name = 'GöteborgsOperan' and t.name = 'Göteborg Opera (GöteborgsOperan)'
on conflict do nothing;

select public.refresh_source_venues();

-- Stable choices: when two sites have the same rank, keep what the performance shows now.
do $$
declare d text;
begin
  d := pg_get_functiondef('public.recompute_performance(uuid)'::regprocedure);
  d := replace(d, 'select theater_id into w_theater from _rk where theater_id is not null order by rk, last_seen_at desc limit 1;',
                  'select theater_id into w_theater from _rk where theater_id is not null order by rk, (theater_id = v.theater_id) desc, last_seen_at desc limit 1;');
  d := replace(d, 'order by time_tbc, rk, last_seen_at desc limit 1;',
                  'order by time_tbc, rk, (starts_at = v.performance_date) desc, last_seen_at desc limit 1;');
  d := replace(d, 'select ticket_url into w_ticket from _rk where ticket_url is not null order by rk, last_seen_at desc limit 1;',
                  'select ticket_url into w_ticket from _rk where ticket_url is not null order by rk, (ticket_url = v.ticket_url) desc, last_seen_at desc limit 1;');
  if position('(ticket_url = v.ticket_url)' in d) = 0 or position('(theater_id = v.theater_id)' in d) = 0
     or position('(starts_at = v.performance_date)' in d) = 0 then
    raise exception 'recompute_performance: pattern not found';
  end if;
  execute d;
end $$;
