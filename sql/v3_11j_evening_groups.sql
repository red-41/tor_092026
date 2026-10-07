-- Saffitt v3, part 11j: the evening groups are kept in a table instead of being recomputed on every call.
-- evening_groups holds one row per evening that has more than one performance row (pieces of a bill, or a header
-- row next to pieces). Triggers keep it in step: any insert, update or delete of a performance refreshes its
-- evening (about 5 ms); a change to a choreographer's people refreshes the evenings that credit it.
-- Rows are never deleted: an evening that stops having several rows is marked n_rows = 1 and ignored by the view.
-- refresh_evening_groups() with no argument rebuilds everything (run after a collector publish or a programme rename).

create table if not exists public.evening_groups (
  ek text primary key,
  n_rows int not null,
  performance_ids uuid[] not null,
  piece_ids uuid[] not null,
  choreographer_ids uuid[] not null default '{}',
  ticket_url text, any_image text, any_description text, any_company uuid,
  cancelled boolean not null default false,
  tags text[] not null default '{}',
  editorial text[] not null default '{}',
  credit_lines text,
  more_text text,
  refreshed_at timestamptz not null default now()
);
alter table public.evening_groups enable row level security;
do $$ begin
  create policy "Signed-in users read evening groups" on public.evening_groups for select to authenticated using (true);
exception when duplicate_object then null; end $$;
revoke all on public.evening_groups from anon, authenticated;
grant select on public.evening_groups to authenticated;
comment on table public.evening_groups is 'One row per mixed-bill evening (several performance rows at one venue and minute). Kept by triggers; refresh_evening_groups() rebuilds.';

create or replace function public.refresh_evening_groups(p_keys text[] default null) returns int
language plpgsql set search_path to 'public' as $function$
declare n int; t0 timestamptz := clock_timestamp();
begin
  -- rows are upserted; an evening that no longer has several rows is marked n_rows = 1 (the view ignores it)
  insert into evening_groups (ek, n_rows, performance_ids, piece_ids, choreographer_ids, ticket_url, any_image, any_description,
                              any_company, cancelled, tags, editorial, credit_lines, more_text, refreshed_at)
  SELECT r.ek, m.n_rows,
         array_agg(r.id ORDER BY r.is_header, r.program_position, r.performance_date, r.created_at),
         CASE WHEN count(*) FILTER (WHERE NOT r.is_header) > 0
              THEN array_agg(r.id ORDER BY r.program_position, r.performance_date, r.created_at) FILTER (WHERE NOT r.is_header)
              ELSE array_agg(r.id ORDER BY r.program_position, r.performance_date, r.created_at) END,
         uniq_uuids(array_cat_agg(r.member_ids ORDER BY r.is_header, r.program_position, r.performance_date, r.created_at)),
         (array_agg(r.ticket_url ORDER BY r.is_header, r.program_position, r.performance_date) FILTER (WHERE r.ticket_url IS NOT NULL))[1],
         (array_agg(r.image_url ORDER BY r.is_header, r.program_position, r.performance_date) FILTER (WHERE r.image_url IS NOT NULL))[1],
         (array_agg(r.description ORDER BY r.is_header, r.program_position, r.performance_date) FILTER (WHERE r.description IS NOT NULL))[1],
         (array_agg(r.company_id ORDER BY r.is_header, r.program_position, r.performance_date) FILTER (WHERE r.company_id IS NOT NULL))[1],
         bool_and(r.cancelled),
         COALESCE((SELECT array_agg(DISTINCT t.t ORDER BY t.t) FROM unnest(array_cat_agg(r.tags)) t(t)), '{}'::text[]),
         COALESCE((SELECT array_agg(DISTINCT e.e ORDER BY e.e) FROM unnest(array_cat_agg(r.editorial)) e(e)), '{}'::text[]),
         string_agg(DISTINCT r.credit_line, ', '::text),
         string_agg(lower(unaccent_fallback(concat_ws(' '::text, r.ptitle, r.credit_line, r.member_names, r.work_title))), ' '::text),
         clock_timestamp()
    FROM (SELECT performances.evening_key AS ek, count(*) AS n_rows FROM performances
           WHERE p_keys IS NULL OR performances.evening_key = ANY (p_keys)
           GROUP BY performances.evening_key HAVING count(*) > 1) m
    JOIN (SELECT p_1.id, p_1.evening_key AS ek, p_1.performance_date, p_1.program_position, p_1.created_at,
                 p_1.ticket_url, p_1.image_url, p_1.description, p_1.company_id,
                 COALESCE(p_1.tags, '{}'::text[]) AS tags, COALESCE(p_1.editorial, '{}'::text[]) AS editorial,
                 'Cancelled'::text = ANY (COALESCE(p_1.tags, '{}'::text[])) AS cancelled,
                 piece_title(p_1.title, pr.title) AS ptitle,
                 CASE WHEN pr.title IS NOT NULL AND lower(piece_title(p_1.title, pr.title)) = lower(pr.title)
                       AND (p_1.work_id IS NULL OR EXISTS (SELECT 1 FROM performances q WHERE q.evening_key = p_1.evening_key
                                                             AND q.id <> p_1.id AND q.work_id = p_1.work_id))
                      THEN (p_1.program_position IS NULL OR p_1.choreographer_id IS NULL OR c.kind = 'credit_line'
                            OR EXISTS (SELECT 1 FROM performances q JOIN choreographers cq ON cq.id = q.choreographer_id
                                        WHERE q.evening_key = p_1.evening_key AND q.id <> p_1.id
                                          AND lower(piece_title(q.title, pr.title)) <> lower(pr.title)
                                          AND COALESCE(c.member_ids, '{}'::uuid[]) <@ COALESCE(cq.member_ids, '{}'::uuid[])))
                      ELSE false END AS is_header,
                 c.name AS credit_line, c.member_names, COALESCE(c.member_ids, '{}'::uuid[]) AS member_ids, w.title AS work_title
            FROM performances p_1
            LEFT JOIN programs pr ON pr.id = p_1.program_id
            LEFT JOIN choreographers c ON c.id = p_1.choreographer_id
            LEFT JOIN works w ON w.id = p_1.work_id
           WHERE p_keys IS NULL OR p_1.evening_key = ANY (p_keys)) r ON r.ek = m.ek
   GROUP BY r.ek, m.n_rows
  on conflict (ek) do update set
    n_rows = excluded.n_rows, performance_ids = excluded.performance_ids, piece_ids = excluded.piece_ids,
    choreographer_ids = excluded.choreographer_ids, ticket_url = excluded.ticket_url, any_image = excluded.any_image,
    any_description = excluded.any_description, any_company = excluded.any_company, cancelled = excluded.cancelled,
    tags = excluded.tags, editorial = excluded.editorial, credit_lines = excluded.credit_lines, more_text = excluded.more_text,
    refreshed_at = excluded.refreshed_at;
  get diagnostics n = row_count;
  update evening_groups set n_rows = 1, refreshed_at = clock_timestamp()
   where n_rows > 1 and refreshed_at < t0 and (p_keys is null or ek = any (p_keys));
  return n;
end $function$;
revoke execute on function public.refresh_evening_groups(text[]) from anon, authenticated;

-- a performance changes: refresh its evening (and the one it left)
create or replace function public.evening_groups_perf_trigger() returns trigger
language plpgsql set search_path to 'public' as $function$
declare keys text[] := '{}';
begin
  if tg_op in ('INSERT', 'UPDATE') then keys := keys || new.evening_key; end if;
  if tg_op in ('DELETE', 'UPDATE') and (tg_op = 'DELETE' or old.evening_key is distinct from new.evening_key) then
    keys := keys || old.evening_key;
  end if;
  perform refresh_evening_groups(keys);
  return null;
end $function$;
create or replace trigger evening_groups_perf after insert or update or delete on public.performances
  for each row execute function public.evening_groups_perf_trigger();

-- a choreographer's people or name change: refresh the evenings that credit it
create or replace function public.evening_groups_choreo_trigger() returns trigger
language plpgsql set search_path to 'public' as $function$
begin
  perform refresh_evening_groups(coalesce((select array_agg(distinct p.evening_key) from performances p where p.choreographer_id = new.id), '{}'));
  return null;
end $function$;
create or replace trigger evening_groups_choreo after update of member_ids, name, member_names on public.choreographers
  for each row execute function public.evening_groups_choreo_trigger();

-- a programme rename changes which row is the header
create or replace function public.evening_groups_program_trigger() returns trigger
language plpgsql set search_path to 'public' as $function$
begin
  perform refresh_evening_groups(coalesce((select array_agg(distinct p.evening_key) from performances p where p.program_id = new.id), '{}'));
  return null;
end $function$;
create or replace trigger evening_groups_program after update of title on public.programs
  for each row execute function public.evening_groups_program_trigger();

select refresh_evening_groups();

-- the view reads the table
create or replace view public.evening_listing with (security_invoker = true) as
SELECT p.id, l.slug,
       CASE WHEN g.ek IS NOT NULL AND l.program_title IS NOT NULL THEN l.program_title ELSE piece_title(l.title, l.program_title) END AS title,
       l.original_title, l.performance_date,
       date_trunc('minute'::text, p.performance_date) AS evening_start,
       l.timezone, l.local_date, l.local_time, l.time_tbc,
       COALESCE(g.tags, l.tags, '{}'::text[]) AS tags,
       COALESCE(g.editorial, l.editorial, '{}'::text[]) AS editorial,
       COALESCE(g.ticket_url, l.ticket_url) AS ticket_url,
       CASE WHEN g.ek IS NOT NULL THEN COALESCE(pg.image_url, g.any_image) ELSE l.image_url END AS image_url,
       CASE WHEN g.ek IS NOT NULL THEN COALESCE(pg.description, g.any_description) ELSE l.description END AS description,
       l.program_id, l.program_position, l.program_title, l.program_slug,
       COALESCE(cardinality(g.piece_ids), 1) AS piece_count,
       CASE WHEN g.ek IS NOT NULL THEN
         (SELECT jsonb_agg(jsonb_build_object('id', p2.id, 'slug', p2.slug, 'title', piece_title(p2.title, pr2.title),
                   'position', p2.program_position, 'work_id', p2.work_id, 'work_title', w2.title,
                   'choreographer_name', c2.name, 'choreographers', COALESCE(c2.members_json, '[]'::jsonb),
                   'tags', p2.tags, 'cancelled', 'Cancelled'::text = ANY (COALESCE(p2.tags, '{}'::text[]))) ORDER BY pid.ord)
            FROM unnest(g.piece_ids) WITH ORDINALITY pid(id, ord)
            JOIN performances p2 ON p2.id = pid.id
            LEFT JOIN programs pr2 ON pr2.id = p2.program_id
            LEFT JOIN choreographers c2 ON c2.id = p2.choreographer_id
            LEFT JOIN works w2 ON w2.id = p2.work_id)
       ELSE NULL::jsonb END AS pieces,
       COALESCE(g.performance_ids, ARRAY[p.id]) AS performance_ids,
       p.evening_key, l.work_id, l.work_title,
       (COALESCE(g.choreographer_ids, l.choreographer_ids))[1] AS choreographer_id,
       CASE WHEN g.ek IS NOT NULL THEN COALESCE((SELECT string_agg(x.name, ', '::text ORDER BY t.o)
                                                   FROM unnest(g.choreographer_ids) WITH ORDINALITY t(x_id, o)
                                                   JOIN choreographers x ON x.id = t.x_id), g.credit_lines)
            ELSE l.choreographer_name END AS choreographer_name,
       x1.slug AS choreographer_slug,
       CASE WHEN g.ek IS NOT NULL THEN (SELECT COALESCE(jsonb_agg(jsonb_build_object('id', x.id, 'name', x.name, 'slug', x.slug) ORDER BY t.o), '[]'::jsonb)
                                          FROM unnest(g.choreographer_ids) WITH ORDINALITY t(x_id, o)
                                          JOIN choreographers x ON x.id = t.x_id)
            ELSE l.choreographers END AS choreographers,
       COALESCE(g.choreographer_ids, l.choreographer_ids) AS choreographer_ids,
       COALESCE(l.company_id, g.any_company) AS company_id,
       COALESCE(l.company_name, cco.name) AS company_name,
       COALESCE(l.company_slug, cco.slug) AS company_slug,
       l.theater_id, l.theater_name, l.theater_slug, l.city, l.country,
       CASE WHEN g.ek IS NOT NULL THEN (l.search_text || ' '::text) || g.more_text ELSE l.search_text END AS search_text,
       l.date_source, l.dates_to_confirm,
       COALESCE(g.cancelled, l.cancelled) AS cancelled,
       COALESCE(g.n_rows, 1::bigint) AS n_rows
  FROM performances p
  JOIN performance_listing l ON l.id = p.id
  LEFT JOIN evening_groups g ON g.ek = p.evening_key AND g.n_rows > 1
  LEFT JOIN programs pg ON pg.id = p.program_id AND g.ek IS NOT NULL
  LEFT JOIN companies cco ON cco.id = g.any_company
  LEFT JOIN choreographers x1 ON x1.id = (COALESCE(g.choreographer_ids, l.choreographer_ids))[1]
 WHERE g.ek IS NULL OR g.performance_ids[1] = p.id;
