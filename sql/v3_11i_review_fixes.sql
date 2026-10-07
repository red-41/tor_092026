-- Saffitt v3, part 11i: fixes from the independent review of 7 October.
-- 1. Splitter: "(revised by X)", "(version by X)", "Act I", "in collaboration with" are not people; "X after Y" without
--    brackets credits X only; em and en dashes separate names; a company written as "A & B" stays one company.
-- 2. Surname matching needs at least three letters.
-- 3. Evening header: a row titled like its programme is the header only when it has no work of its own (or a work
--    another row of the evening also has) and either has no running position, no choreographer, a multi-person
--    credit line, or people already credited on other rows of the same evening. A piece that happens to carry the
--    programme's name stays a piece.
-- 4. listing_query picks the page of evening ids first, then builds the rows: no more sorting 15,000 rows with
--    their json. performance_filter_options and sitemap_entries read the light tables.
-- 5. credit_review loses the write grants Supabase adds by default.

-- 1. Splitter
create or replace function public.split_credit_line(p_name text) returns text[]
language sql immutable set search_path to 'public' as $function$
  with cleaned as (
    select regexp_replace(
             regexp_replace(
               regexp_replace(coalesce(p_name, ''),
                 $r$\s*\((after|nach|d.apr[eè]s|based on|frei nach|from|inspired by|(new\s+)?version|revised|revival|revived|restaged|staged|staging|re-?adapted|recreated|reconstructed|supervised|arranged|production|produced|act\s|acts\s|scene\s|part\s)[^)]*\)$r$, '', 'gi'),
               $r$\s*,?\s+(after|nach|d.apr[eè]s|based on|frei nach)\s+.*$$r$, '', 'i'),   -- "Pierre Lacotte after Joseph Mazilier"
             $r$\s+\(([^)]*)\)\s*$$r$, ', \1', 'g') s              -- a bracket at the end is a part of its own: "(Kor'sia)"
  ),
  parts as (
    select trim(both ' ,;:' from regexp_replace(t.part,
             $r$^\s*(?:(?:and|und|et|y)\s+)?(?:choreograph(?:y|ie|ed)(?: by|:)?|chor[ée]graphie(?: de|:)?|coreograf[ií]a(?: di| de|:)?|choreografie(?: von|:)?)?\s*$r$,
             '', 'i')) part, t.ord
      from cleaned,
           regexp_split_to_table(cleaned.s,
             $r$\s*(/|,|&|;|\+|\||—|–|\s-\s|\s(and|und|et|y|e|en|og|och|with|mit|avec|con|feat\.?|featuring|in collaboration with|in collaboration|in samenwerking met|en collaboration avec|in zusammenarbeit mit)\s)\s*$r$, 'i')
             with ordinality as t(part, ord)
  )
  select coalesce(array_agg(part order by ord), '{}')
    from parts
   where length(part) >= 2
     and part !~* $r$^(after|nach|d.apr[eè]s|based on|frei nach|inspired by|from|by|with|revised|revival|revived|restaged|staged|staging|re-?adapted|recreated|reconstructed|supervised|arranged|version|new version|production|produced|act|acts|scene|part)(\s|$)$r$
     and part !~* $r$^(gäste|gaeste|guests?|others?|andere|anderen|autres|altri|otros|weitere|friends|company|ensemble|dancers|cast|et al\.?|collective|kollektiv|u\.a\.|etc\.?|various|diverse|verschiedene|tba|tbc|tbd|unknown|mixed|several|multiple|divers|various artists|choreograph(y|ie|ers?)|chorégraphie|coreografia|line-up tba|others|version|original|restaging|staging|revival|reconstruction|adaptation|inspiration|collaboration|col\.|coll\.|n\.n\.|concept|direction|regie|music|musik|libretto|text)$$r$
     and part !~ $r$^[A-Z]{2,3}$$r$                                   -- (CH), NDT: codes, not names; "Xie" stays
     and part !~* $r$(^|\s)(the\s+)?(dancers|members|ensemble|cast|company|collective|kollektiv|compagnie|compañía|companhia|compagnia|troupe|students|pupils)(\s|$)$r$
$function$;

-- 2. Surnames: at least three letters
create or replace function public.resolve_credit_part(p_part text,
  out member_id uuid, out company_id uuid, out candidates uuid[], out status text)
language plpgsql set search_path to 'public' as $function$
declare s text := slugify(p_part); v_surname boolean; v_clean text[];
begin
  status := 'unresolved'; candidates := '{}';
  if s = '' then status := 'ignored'; return; end if;
  select c.id into member_id from choreographers c
   where c.kind = 'person' and (slugify(c.name) = s or s = any (select slugify(a) from unnest(coalesce(c.aliases, '{}')) a))
   order by c.created_at limit 1;
  if member_id is not null then status := 'person'; return; end if;
  select co.id into company_id from companies co
   where slugify(co.name) = s or s = any (select slugify(a) from unnest(coalesce(co.aliases, '{}')) a)
   order by co.created_at limit 1;
  if company_id is not null then status := 'company'; return; end if;
  v_surname := p_part !~ '\s' or p_part ~* $r$^(de|van|von|di|da|del|della|de la|le|la|du|des|van der|van den|ter|ten|af|av)\s+\S+$$r$;
  if v_surname then
    if length(s) >= 3 then
      select coalesce(array_agg(c.id order by c.created_at), '{}') into candidates
        from choreographers c where c.kind = 'person' and slugify(c.name) like '%-' || s;
      if cardinality(candidates) = 1 then member_id := candidates[1]; status := 'person'; end if;
    end if;
    return;
  end if;
  -- only a clean single name becomes a new person; anything else waits for an editor
  v_clean := split_credit_line(p_part);
  if cardinality(v_clean) <> 1 or slugify(v_clean[1]) <> s then return; end if;
  insert into choreographers (name, nationality, kind) values (trim(p_part), 'Unknown', 'person') returning id into member_id;
  status := 'new person';
end $function$;

-- A company written as "A & B" ("Club Guy & Roni") is matched before its halves are resolved.
create or replace function public.rebuild_credit_line(p_line uuid) returns void
language plpgsql set search_path to 'public' as $function$
declare c record; v_parts text[]; v_part text; r record; v_resolved uuid; i int := 0;
        v_members uuid[] := '{}'; v_companies uuid[] := '{}';
        v_next text; v_between text; v_try text; p1 int; p2 int; v_skip int := 0; v_co uuid;
begin
  select * into c from choreographers where id = p_line;
  if c.id is null then return; end if;
  v_parts := split_credit_line(c.name);
  if cardinality(v_parts) = 1 and slugify(v_parts[1]) = slugify(c.name) then
    update choreographers set kind = 'person', member_ids = array[p_line], credit_company_ids = '{}'
     where id = p_line and (kind <> 'person' or member_ids <> array[p_line] or credit_company_ids <> '{}');
    update credit_review set status = 'ignored', decided_at = now() where line_id = p_line and status = 'open';
    perform refresh_member_details(array[p_line]);
    return;
  end if;
  foreach v_part in array v_parts loop
    i := i + 1;
    if v_skip > 0 then v_skip := v_skip - 1; continue; end if;
    select cr.resolved_member into v_resolved from credit_review cr
     where cr.line_id = p_line and cr.part = v_part and cr.status = 'resolved';
    if v_resolved is not null then
      if not (v_resolved = any (v_members)) then v_members := v_members || v_resolved; end if;
      continue;
    end if;
    -- the text between this part and the next, as the source wrote it
    v_between := null; v_next := null;
    if i < cardinality(v_parts) then
      v_next := v_parts[i + 1];
      p1 := position(v_part in c.name); p2 := position(v_next in c.name);
      if p1 > 0 and p2 > p1 then v_between := substr(c.name, p1 + length(v_part), p2 - p1 - length(v_part)); end if;
    end if;
    -- "A & B" that is one company
    if v_between ~* '^\s*(&|and|und|et|y|og|och|e|en)\s*$' then
      select co.id into v_co from companies co
       where slugify(co.name) = slugify(v_part || ' & ' || v_next)
          or slugify(v_part || ' & ' || v_next) = any (select slugify(a) from unnest(coalesce(co.aliases, '{}')) a)
       order by co.created_at limit 1;
      if v_co is not null then
        if not (v_co = any (v_companies)) then v_companies := v_companies || v_co; end if;
        update credit_review set status = 'ignored', decided_at = now()
         where line_id = p_line and part in (v_part, v_next) and status = 'open';
        v_skip := 1;
        continue;
      end if;
    end if;
    select * into r from resolve_credit_part(v_part);
    -- a lone first name joined by "and" to "First Surname": try "First-name Surname"
    if r.status = 'unresolved' and v_part !~ '\s' and v_next ~ '\s' and v_between ~* '^\s*(&|\+|and|et|und|y|og|och|e|en)\s*$' then
      v_try := v_part || ' ' || regexp_replace(v_next, '^\S+\s+', '');
      select * into r from resolve_credit_part(v_try);
    end if;
    if r.status in ('person', 'new person') then
      if not (r.member_id = any (v_members)) then v_members := v_members || r.member_id; end if;
      update credit_review set status = 'resolved', resolved_member = r.member_id, decided_at = now()
       where line_id = p_line and part = v_part and status = 'open';
    elsif r.status = 'company' then
      if not (r.company_id = any (v_companies)) then v_companies := v_companies || r.company_id; end if;
      update credit_review set status = 'ignored', decided_at = now() where line_id = p_line and part = v_part and status = 'open';
    elsif r.status = 'unresolved' then
      insert into credit_review (line_id, part, candidates) values (p_line, v_part, r.candidates)
      on conflict (line_id, part) do update set candidates = excluded.candidates where credit_review.status = 'open';
    end if;
  end loop;
  update credit_review set status = 'ignored', decided_at = now()
   where line_id = p_line and status = 'open' and not (part = any (v_parts));
  update choreographers set kind = 'credit_line', member_ids = v_members, credit_company_ids = v_companies
   where id = p_line;
  perform refresh_member_details(array[p_line]);
end $function$;

-- 3. Evening header rule
create or replace view public.evening_listing with (security_invoker = true) as
WITH g AS (
  SELECT r.ek, m.n_rows,
         array_agg(r.id ORDER BY r.is_header, r.program_position, r.performance_date, r.created_at) AS performance_ids,
         CASE WHEN count(*) FILTER (WHERE NOT r.is_header) > 0
              THEN array_agg(r.id ORDER BY r.program_position, r.performance_date, r.created_at) FILTER (WHERE NOT r.is_header)
              ELSE array_agg(r.id ORDER BY r.program_position, r.performance_date, r.created_at) END AS piece_ids,
         uniq_uuids(array_cat_agg(r.member_ids ORDER BY r.is_header, r.program_position, r.performance_date, r.created_at)) AS choreographer_ids,
         (array_agg(r.ticket_url ORDER BY r.is_header, r.program_position, r.performance_date) FILTER (WHERE r.ticket_url IS NOT NULL))[1] AS ticket_url,
         (array_agg(r.image_url ORDER BY r.is_header, r.program_position, r.performance_date) FILTER (WHERE r.image_url IS NOT NULL))[1] AS any_image,
         (array_agg(r.description ORDER BY r.is_header, r.program_position, r.performance_date) FILTER (WHERE r.description IS NOT NULL))[1] AS any_description,
         (array_agg(r.company_id ORDER BY r.is_header, r.program_position, r.performance_date) FILTER (WHERE r.company_id IS NOT NULL))[1] AS any_company,
         bool_and(r.cancelled) AS cancelled,
         COALESCE((SELECT array_agg(DISTINCT t.t ORDER BY t.t) FROM unnest(array_cat_agg(r.tags)) t(t)), '{}'::text[]) AS tags,
         COALESCE((SELECT array_agg(DISTINCT e.e ORDER BY e.e) FROM unnest(array_cat_agg(r.editorial)) e(e)), '{}'::text[]) AS editorial,
         string_agg(DISTINCT r.credit_line, ', '::text) AS credit_lines,
         string_agg(lower(unaccent_fallback(concat_ws(' '::text, r.ptitle, r.credit_line, r.member_names, r.work_title))), ' '::text) AS more_text
    FROM (SELECT performances.evening_key AS ek, count(*) AS n_rows FROM performances GROUP BY performances.evening_key HAVING count(*) > 1) m
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
            LEFT JOIN works w ON w.id = p_1.work_id) r ON r.ek = m.ek
   GROUP BY r.ek, m.n_rows
)
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
  LEFT JOIN g ON g.ek = p.evening_key
  LEFT JOIN programs pg ON pg.id = p.program_id AND g.ek IS NOT NULL
  LEFT JOIN companies cco ON cco.id = g.any_company
  LEFT JOIN choreographers x1 ON x1.id = (COALESCE(g.choreographer_ids, l.choreographer_ids))[1]
 WHERE g.ek IS NULL OR g.performance_ids[1] = p.id;

-- 4a. listing_query: page of ids first, rows second
create or replace function public.listing_query(p jsonb) returns jsonb
language plpgsql security definer set search_path to 'public' as $function$
declare
  v_uid uuid := auth.uid();
  v_admin boolean := v_uid is not null and has_role(v_uid, 'admin');
  v_limit int := least(greatest(coalesce((p->>'limit')::int, 50), 1),
                       case when v_admin then 1000 when v_uid is null then 50 else 200 end);
  v_offset int := greatest(coalesce((p->>'offset')::int, 0), 0);
  v_where text := 'true';
  v_order text;
  v_drop text := case when v_uid is null then 'ticket_url' else '-' end;
  v_rows jsonb; v_n int; v_total int;
  w text; k text;
  arr text[];
begin
  if v_uid is null and v_offset + v_limit > 300 then
    v_limit := 300 - v_offset;
    if v_limit <= 0 then return jsonb_build_object('rows', '[]'::jsonb, 'total', null, 'capped', true); end if;
  end if;

  if coalesce((p->>'upcoming')::boolean, true) then v_where := v_where || ' and performance_date >= now()'; end if;
  if jsonb_typeof(p->'words') = 'array' then
    for w in select jsonb_array_elements_text(p->'words') loop
      if length(w) > 0 then v_where := v_where || format(' and search_text ilike %L', '%' || left(w, 60) || '%'); end if;
    end loop;
  end if;
  -- people: any person credited on the evening
  if jsonb_typeof(p->'choreographer_ids') = 'array' and jsonb_array_length(p->'choreographer_ids') > 0 then
    select array_agg(x) into arr from jsonb_array_elements_text(p->'choreographer_ids') x;
    v_where := v_where || format(' and choreographer_ids && %L::uuid[]', arr);
  end if;
  if p ? 'choreographer_id' and p->>'choreographer_id' is not null then
    v_where := v_where || format(' and %L::uuid = any(choreographer_ids)', p->>'choreographer_id');
  end if;
  -- performances: any piece of the evening
  if jsonb_typeof(p->'ids') = 'array' and jsonb_array_length(p->'ids') > 0 then
    select array_agg(x) into arr from jsonb_array_elements_text(p->'ids') x;
    v_where := v_where || format(' and performance_ids && %L::uuid[]', arr);
  end if;
  foreach k in array array['company_id', 'theater_id', 'program_id'] loop
    if jsonb_typeof(p->(k || 's')) = 'array' and jsonb_array_length(p->(k || 's')) > 0 then
      select array_agg(x) into arr from jsonb_array_elements_text(p->(k || 's')) x;
      v_where := v_where || format(' and %I = any(%L::uuid[])', k, arr);
    end if;
  end loop;
  foreach k in array array['company_id', 'theater_id', 'program_id', 'work_id'] loop
    if p ? k and p->>k is not null then v_where := v_where || format(' and %I = %L::uuid', k, p->>k); end if;
  end loop;
  foreach k in array array['city', 'country'] loop
    if jsonb_typeof(p->(k || '_list')) = 'array' and jsonb_array_length(p->(k || '_list')) > 0 then
      select array_agg(x) into arr from jsonb_array_elements_text(p->(k || '_list')) x;
      v_where := v_where || format(' and %I = any(%L::text[])', k, arr);
    end if;
  end loop;
  foreach k in array array['tags', 'editorial'] loop
    if jsonb_typeof(p->k) = 'array' and jsonb_array_length(p->k) > 0 then
      select array_agg(x) into arr from jsonb_array_elements_text(p->k) x;
      v_where := v_where || format(' and %I && %L::text[]', k, arr);
    end if;
  end loop;
  if p->>'program_mode' = 'only' then v_where := v_where || ' and piece_count > 1'; end if;
  if p->>'program_mode' = 'exclude' then v_where := v_where || ' and piece_count = 1'; end if;
  if p->>'date_from' is not null then v_where := v_where || format(' and local_date >= %L::date', p->>'date_from'); end if;
  if p->>'date_to' is not null then v_where := v_where || format(' and local_date <= %L::date', p->>'date_to'); end if;
  if p->>'start_from' is not null then v_where := v_where || format(' and performance_date >= %L::timestamptz', p->>'start_from'); end if;
  if p->>'start_before' is not null then v_where := v_where || format(' and performance_date < %L::timestamptz', p->>'start_before'); end if;
  if p->>'exclude_id' is not null then v_where := v_where || format(' and not (%L::uuid = any(performance_ids))', p->>'exclude_id'); end if;

  v_order := case when p->>'order' = 'program' then 'program_position nulls last, id'
                  else 'local_date, cancelled, performance_date, program_position nulls last, id' end;

  -- the page of ids first (narrow rows, cheap sort), then the full rows for those ids only
  execute format(
    'select jsonb_agg(j order by rn), count(*) from (
       select (to_jsonb(e) - ''search_text'' - ''date_source'' - %L) j, k.rn
         from (select id, row_number() over (order by %s) rn
                 from evening_listing where %s order by %s offset %s limit %s) k
         join evening_listing e on e.id = k.id) x',
    v_drop, v_order, v_where, v_order, v_offset, v_limit) into v_rows, v_n;

  if coalesce((p->>'count')::boolean, false) then
    execute format('select count(*) from evening_listing where %s', v_where) into v_total;
  end if;

  perform _listing_budget(coalesce(v_n, 0));
  return jsonb_build_object('rows', coalesce(v_rows, '[]'::jsonb), 'total', v_total, 'capped', false);
end $function$;

-- 4b. filter options from the light rows (same sets as the evenings: a filter applies to the evening anyway)
create or replace function public.performance_filter_options() returns jsonb
language sql stable security definer set search_path to 'public' as $function$
  with up as (select company_id, company_name, theater_id, theater_name, city, country, tags, choreographer_ids
                from performance_listing where performance_date >= now() - interval '6 hours')
  select jsonb_build_object(
    'companies', (select coalesce(jsonb_agg(distinct jsonb_build_object('id', company_id, 'name', company_name)), '[]') from up where company_id is not null),
    'theaters', (select coalesce(jsonb_agg(distinct jsonb_build_object('id', theater_id, 'name', theater_name, 'city', city, 'country', country)), '[]') from up where theater_id is not null),
    'choreographers', (select coalesce(jsonb_agg(distinct jsonb_build_object('id', x.id, 'name', x.name)), '[]')
                         from (select distinct unnest(choreographer_ids) as cid from up) u join choreographers x on x.id = u.cid),
    'cities', (select coalesce(jsonb_agg(distinct city), '[]') from up where city is not null),
    'city_places', (select coalesce(jsonb_agg(distinct jsonb_build_object('city', city, 'country', country)), '[]') from up where city is not null),
    'countries', (select coalesce(jsonb_agg(distinct country), '[]') from up where country is not null),
    'tags', (select coalesce(jsonb_agg(distinct tg), '[]') from up, unnest(tags) tg where tg <> 'Time TBC')
  );
$function$;

-- 4c. sitemap from the light tables (one address per evening: any row's slug opens the evening)
create or replace function public.sitemap_entries() returns table(kind text, slug text, lastmod timestamptz)
language sql stable security definer set search_path to 'public' as $function$
  with people as (select distinct unnest(l.member_ids) as cid
                    from performances p join choreographers l on l.id = p.choreographer_id where p.performance_date >= now())
  select 'performance', s.slug, s.lastmod from (
    select distinct on (p.evening_key) p.slug, coalesce(p.updated_at, p.created_at) as lastmod
      from performances p where p.performance_date >= now() and p.slug is not null
     order by p.evening_key, p.program_position nulls last, p.performance_date, p.created_at) s
  union all
  select 'choreographer', c.slug, coalesce(c.updated_at, c.created_at)
    from choreographers c join people on people.cid = c.id where c.slug is not null and c.kind = 'person'
  union all
  select 'company', co.slug, coalesce(co.updated_at, co.created_at) from companies co
   where co.slug is not null and exists (select 1 from performances p where p.company_id = co.id and p.performance_date >= now())
  union all
  select 'theater', t.slug, coalesce(t.updated_at, t.created_at) from theaters t
   where t.slug is not null and exists (select 1 from performances p where p.theater_id = t.id and p.performance_date >= now())
  union all
  select 'program', g.slug, coalesce(g.updated_at, g.created_at) from programs g
   where g.slug is not null and exists (select 1 from performances p where p.program_id = g.id and p.performance_date >= now())
$function$;

-- 5. credit_review is read and updated by admins only (RLS); no table-level writes for the API roles beyond that
revoke insert, delete, truncate, references, trigger on public.credit_review from anon, authenticated;
