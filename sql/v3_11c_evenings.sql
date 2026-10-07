-- Saffitt v3, part 11c: the evening as the unit. Pieces of one mixed bill (same programme, venue and start minute)
-- are shown as one row with a list of pieces; a full-length ballet is one row as before. Credits are the union of
-- the pieces' people. Column names follow performance_listing so the site's calls keep working.
create or replace function public.evening_key(p_program uuid, p_id uuid, p_theater uuid, p_date timestamptz) returns text
language sql immutable set search_path to 'public' as $fn$
  select md5(coalesce(p_program, p_id)::text || '|' || coalesce(p_theater::text, '') || '|'
             || date_trunc('minute', p_date at time zone 'UTC')::text)
$fn$;

create or replace view public.evening_listing with (security_invoker = true) as
with b as (
  select p.id, p.program_id, p.theater_id, p.performance_date, p.program_position, p.created_at,
         p.ticket_url, p.image_url, p.description, p.company_id, p.tags,
         evening_key(p.program_id, p.id, p.theater_id, p.performance_date) as ek,
         date_trunc('minute', p.performance_date) as evening_start,
         g.title as program_title,
         case when g.title is not null and p.title like '% (' || g.title || ')'
              then left(p.title, length(p.title) - length(g.title) - 3) else p.title end as piece_title
    from performances p left join programs g on g.id = p.program_id
),
b2 as (
  select b.*, (b.program_title is not null and slugify(b.piece_title) = slugify(b.program_title)) as is_header from b
),
g as (
  select ek, evening_start, program_id, theater_id,
         count(*) as n_rows,
         array_agg(id order by is_header, program_position nulls last, performance_date, created_at) as performance_ids,
         case when count(*) filter (where not is_header) > 0
              then array_agg(id order by program_position nulls last, performance_date, created_at) filter (where not is_header)
              else array_agg(id order by program_position nulls last, performance_date, created_at) end as piece_ids,
         (array_agg(ticket_url order by is_header, program_position nulls last, performance_date) filter (where ticket_url is not null))[1] as ticket_url,
         (array_agg(image_url order by is_header, program_position nulls last, performance_date) filter (where image_url is not null))[1] as any_image,
         (array_agg(description order by is_header, program_position nulls last, performance_date) filter (where description is not null))[1] as any_description,
         (array_agg(company_id order by is_header, program_position nulls last, performance_date) filter (where company_id is not null))[1] as any_company,
         bool_and('Cancelled' = any (coalesce(tags, '{}'))) as cancelled
    from b2
   group by ek, evening_start, program_id, theater_id
)
select
  g.performance_ids[1] as id, l.slug,
  case when g.n_rows > 1 and l.program_title is not null then l.program_title
       else (select piece_title from b2 where b2.id = g.performance_ids[1]) end as title,
  l.original_title, l.performance_date, g.evening_start, l.timezone, l.local_date, l.local_time, l.time_tbc,
  px.tags, px.editorial,
  g.ticket_url,
  coalesce(case when g.n_rows > 1 then pg.image_url end, g.any_image) as image_url,
  coalesce(case when g.n_rows > 1 then pg.description end, g.any_description, pg.description) as description,
  g.program_id, l.program_position, l.program_title, l.program_slug,
  cardinality(g.piece_ids) as piece_count, px.pieces, g.performance_ids, g.ek as evening_key,
  l.work_id, l.work_title,
  em.first_id as choreographer_id,
  case when g.n_rows > 1 then coalesce(em.names, px.credit_lines) else l.choreographer_name end as choreographer_name,
  em.first_slug as choreographer_slug,
  coalesce(em.choreographers, '[]'::jsonb) as choreographers,
  coalesce(em.choreographer_ids, '{}'::uuid[]) as choreographer_ids,
  coalesce(l.company_id, g.any_company) as company_id,
  coalesce(l.company_name, cco.name) as company_name, coalesce(l.company_slug, cco.slug) as company_slug,
  l.theater_id, l.theater_name, l.theater_slug, l.city, l.country,
  px.search_text, l.date_source, l.dates_to_confirm, g.cancelled,
  g.n_rows
from g
join performance_listing l on l.id = g.performance_ids[1]
left join programs pg on pg.id = g.program_id
left join companies cco on cco.id = g.any_company
cross join lateral (
  select jsonb_agg(jsonb_build_object('id', x.id, 'name', x.name, 'slug', x.slug) order by mm.ord) as choreographers,
         array_agg(x.id order by mm.ord) as choreographer_ids,
         string_agg(x.name, ', ' order by mm.ord) as names,
         (array_agg(x.id order by mm.ord))[1] as first_id,
         (array_agg(x.slug order by mm.ord))[1] as first_slug
    from (select m.member_id, min(pid.ord * 100 + m.position) as ord
            from unnest(g.piece_ids) with ordinality as pid(id, ord)
            join performances p2 on p2.id = pid.id
            join credit_line_members m on m.line_id = p2.choreographer_id
           group by m.member_id) mm
    join choreographers x on x.id = mm.member_id) em
cross join lateral (
  select jsonb_agg(jsonb_build_object('id', pl2.id, 'slug', pl2.slug,
             'title', (select piece_title from b2 where b2.id = pl2.id), 'position', pl2.program_position,
             'work_id', pl2.work_id, 'work_title', pl2.work_title, 'choreographer_name', pl2.choreographer_name,
             'choreographers', coalesce(pl2.choreographers, '[]'::jsonb), 'tags', pl2.tags, 'cancelled', pl2.cancelled)
           order by pid.ord) as pieces,
         (select array_agg(distinct t order by t) from unnest(g.performance_ids) pa(id) join performances p3 on p3.id = pa.id,
                 unnest(coalesce(p3.tags, '{}')) t) as tags,
         (select array_agg(distinct e order by e) from unnest(g.performance_ids) pa(id) join performances p3 on p3.id = pa.id,
                 unnest(coalesce(p3.editorial, '{}')) e) as editorial,
         string_agg(distinct pl2.choreographer_name, ', ') as credit_lines,
         string_agg(pl2.search_text, ' ') as search_text
    from unnest(g.piece_ids) with ordinality as pid(id, ord)
    join performance_listing pl2 on pl2.id = pid.id) px;
grant select on public.evening_listing to anon, authenticated;
