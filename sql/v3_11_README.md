# v3.11: credits and evenings (applied 7 October 2026)

Decisions (7 Oct): "after Petipa" gives Petipa no credit (text only); no redirects for the old combined-name
addresses; companies are not split (they need their own duplicate logic later).

## The model

- **A choreographer row is a person or a credit line** (`choreographers.kind`). A credit line is a name as the
  source wrote it: "Sol León & Paul Lightfoot", "Balanchine / Morau / Preljocaj / Xie". It stays for display.
- **`choreographers.member_ids`** lists the people in a credit line (a person lists itself).
  `member_names` and `members_json` are kept in step by `refresh_member_details`. `credit_company_ids` holds a
  company named in the line ("Marcos Morau & La Veronal"); it fills an empty company field on the performance.
- **`split_credit_line(name)`** cuts a line on `/ , & ; + |` and the words and, und, et, y, e, en, og, och, with,
  mit, avec, con. "(after X)" is dropped. Group words (dancers, ensemble, guests, various...) are dropped.
- **`resolve_credit_part(part)`**: a person on the site (name or alias, accent-insensitive), else a company, else a
  surname alone that exactly one person carries, else a new person (full names only). Anything else goes to
  **`credit_review`** (surname with several candidates, lone first names), for an editor:
  `select resolve_credit_review(id, person_id)`; `person_id = null` ignores the part. A resolved full name becomes
  an alias of the person, so the same line resolves by itself next time.
  "Imre & Marne van Opstal": a lone first name joined by an "and" word to "First Surname" borrows that surname.
- **`rebuild_credit_line(id)`** does all of the above for one row; the trigger `choreographer_credits` runs it on
  every insert or rename, so the collector needs no change.
- **`performance_credits`** (view): performance -> people. **`choreographer_listing`** (view): people only, with
  the number of upcoming evenings and the next date. `sitemap_entries()` lists people only.
- **The evening is the unit.** `performances.evening_key` (generated, indexed) = programme or performance id +
  venue + start minute. **`evening_listing`** (view) has one row per evening with the columns of
  `performance_listing` plus `pieces` (json: id, slug, title, position, work, choreographer_name, choreographers,
  tags, cancelled), `piece_count`, `performance_ids`, `evening_key`, `choreographers` (json: id, name, slug, the
  people of all pieces, in running order), `choreographer_ids`, `n_rows`. Its `id` and `slug` are the first piece's.
  A row titled like its programme next to piece rows ("ENERGY" beside "Tagadà (ENERGY)") is the evening's header:
  it feeds the evening's credits, ticket link and description and is never listed as a piece.
  A full count of all upcoming evenings takes about 200 ms; one evening by key about 6 ms.
- **The site's functions read evenings**: `listing_query(p)` (filters: `choreographer_ids` = any of these people,
  `choreographer_id`, `ids` = evenings containing any of these performances, `company_ids`, `theater_ids`,
  `program_ids`, `city_list`, `country_list`, `tags`, `editorial`, `program_mode` only|exclude, `date_from`,
  `date_to`, `words`, `exclude_id`, `order` program|date, `limit`, `offset`, `count`), `performance_by_slug(slug)`
  (adds `evening`), `performance_filter_options()`, `explore_facets(kind)`, `upcoming_counts('choreographer')`,
  `top_upcoming_cities()`.
- **Matcher**: pieces of one bill are stored one second apart to keep their order; the matcher now compares the
  minute, so the same piece listed in another order by another site is one row (v3_11a; 33 copies merged).

## Migrations applied (Supabase, in order)

v3_11a_piece_order, v3_11b1..b4 (credit tables, split, rebuild, triggers), v3_11c1_listing_credits,
v3_11c1..c3 and v3_11e1..e5 (evening view, fast form), v3_11d1_credit_arrays, v3_11d2..d5, v3_11g, v3_11g2
(splitter refinements), v3_11e3_member_names, v3_11e4_listings_from_arrays, v3_11f_listing_functions_evenings,
v3_11h_credit_queue, v3_11i1..i6 (review fixes: splitter, surnames, header rule, listing speed, duo companies),
v3_11j1..j2 (evening_groups table). The files here hold v3_11a, b, c (first form), h, i and j; the live
definitions are in the database (`pg_get_functiondef`, `pg_get_viewdef`).

## After the review of 7 October (v3_11i, v3_11j)

- Splitter: "(revised by X)", "(version by X)", "(Act I)" and "in collaboration with" no longer make people;
  "Pierre Lacotte after Joseph Mazilier" credits Lacotte only; em and en dashes separate names; a company written
  as "A & B" ("Club Guy & Roni") is kept as the company, but a duo of two personal names registered as a company
  ("Sarah Baltzinger & Isaiah Wilson") credits both people as well. A one-word name is always a person ("Nach").
- Surname matching needs at least three letters.
- Evening header rule: a row titled like its programme is the header only when it has no work of its own (or a
  work another row of the evening also has) and no running position, or no choreographer, or a multi-person
  credit line, or people already credited on other rows. Volksoper's piece "Carmen Suite" inside the programme
  "Carmen Suite" stays a piece; Introdans' "ENERGY" next to "Tagadà (ENERGY)" (same work) is the header.
- **`evening_groups`** (table) holds the grouped data for every evening with several rows, kept by triggers on
  performances (each row change refreshes its evening, about 5 ms), choreographers (people or name changes) and
  programs (title). `refresh_evening_groups()` rebuilds all. Rows are never deleted: an evening that stops having
  several rows is marked `n_rows = 1` and ignored. The view `evening_listing` reads the table.
  Default calendar page: 2.8 s before, 0.4 s now. City and word search: 1.8 s before, 0.6 s now.
  `performance_filter_options()`: 3.6 s to 0.4 s. `sitemap_entries()`: 1.7 s to 0.1 s.
- `listing_query` fetches the page's evening keys first, then the rows for those keys.
- Header rows without a programme (Wiener Staatsoper "Visionary Dances") got their programme (data fix).
- Aliases added: Aszure Barton (Azure Barton), Julie Botet (Julie Botel), Ninette de Valois (de Valois).

## Still for a person

- `credit_review` open rows: surnames with several candidates (Ekman, MacMillan, Peck, Martínez, Walerski) and
  lone first names ("Jonas&Lander", "Baye & Asa"). 48 parts on 34 lines on 7 Oct, evening.
- Rows to delete (need a confirmed DELETE): choreographer rows created on 7 Oct that nothing references any more
  (fake people from earlier splitter versions such as "revised by German Shishkin", the typo rows "Azure Barton",
  "Julie Botel", "de Valois"), and the empty tables credit_line_members and credit_line_companies:
  `delete from choreographers c where created_at::date = '2026-10-07' and not exists (select 1 from choreographers l where c.id = any(l.member_ids) and l.id <> c.id) and not exists (select 1 from performances p where p.choreographer_id = c.id) and not exists (select 1 from works w where w.choreographer_id = c.id);`
  `drop table credit_line_members, credit_line_companies;`
- Translation duplicates ("Les Autres" / "The Others", "Feuer und Wasser" / "Fire and Water") and generic-venue
  duplicates ("Opernhaus" vs "Oper Köln") are a matcher matter, not v3.11.
- The matcher should attach a programme to a row whose title equals the programme title at the same venue and
  minute (today it is a data fix).
- `choreographer_listing` runs with the caller's rights, so the public site cannot read its counts yet. Either
  `alter view public.choreographer_listing set (security_invoker = false);` (names and counts only, no ticket
  links) or have the site use `choreographers where kind = 'person'` with `upcoming_counts('choreographer')`.
- Companies: the same duplicate pattern exists in smaller numbers ("Gauthier Dance / Theaterhaus Stuttgart" is one
  company, so no automatic split). Needs its own logic later.
