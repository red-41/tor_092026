-- Saffitt v3, part 11a: a piece of a mixed bill is the same piece whatever its running order.
-- Pieces of one evening are stored one second apart (18:15:00, :01, :02) to keep their order. When another site
-- lists the same bill in another order, the seconds differ and the matcher inserted the piece a second time.
-- The exact-time steps now compare the minute.
do $do$
declare d text;
begin
  d := pg_get_functiondef('public.promote_staging_performances(boolean, integer, timestamptz, bigint, boolean)'::regprocedure);
  if position('date_trunc(''minute'', v_ts)' in d) > 0 then return; end if;
  d := replace(d, ' and p.performance_date = v_ts',
                  ' and p.performance_date >= date_trunc(''minute'', v_ts) and p.performance_date < date_trunc(''minute'', v_ts) + interval ''1 minute''');
  d := replace(d, 'when v_old.performance_date <> v_ts and not v_tbc then ''retime''',
                  'when date_trunc(''minute'', v_old.performance_date) <> date_trunc(''minute'', v_ts) and not v_tbc then ''retime''');
  execute d;
end $do$;

-- The pieces already listed twice: keep the older row, move the other's listings onto it.
with ev as (
  select array_agg(id order by created_at, id) ids
    from performances
   where program_id is not null
   group by program_id, theater_id, date_trunc('minute', performance_date), slugify(title), choreographer_id
  having count(*) > 1)
select count(*) from (select merge_performances(d, ids[1], 'same piece of the same bill, listed twice with a different running order') from ev, unnest(ids[2:]) d) x;
