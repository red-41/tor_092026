-- Saffitt v3, part 11b: credits. A performance's choreographer row may be a credit line ("Sol León & Paul Lightfoot",
-- "Balanchine / Morau / Preljocaj / Xie", "Nureyev, after Petipa"). Each credit line is split into its people, and
-- only people appear in lists, search and filters. The line itself stays as written, for display.
--   choreographers.kind            'person' or 'credit_line'
--   credit_line_members            line -> each person credited (a person is its own single member)
--   credit_line_companies          a company named in a credit line ("Marcos Morau & La Veronal")
--   credit_review                  parts nobody could resolve (surname only, ambiguous), for an editor
--   performance_credits (view)     performance -> people
-- "after X" is not a credit (decision of 7 Oct): the new choreographer is credited, X is not.

alter table public.choreographers add column if not exists kind text not null default 'person';
do $$ begin
  alter table public.choreographers add constraint choreographers_kind_check check (kind in ('person', 'credit_line'));
exception when duplicate_object then null; end $$;
comment on column public.choreographers.kind is 'person: one choreographer. credit_line: a line naming several people (split into credit_line_members).';

create table if not exists public.credit_line_members (
  line_id uuid not null references public.choreographers(id) on delete cascade,
  member_id uuid not null references public.choreographers(id) on delete cascade,
  position int not null default 1,
  primary key (line_id, member_id)
);
create index if not exists credit_line_members_member_idx on public.credit_line_members (member_id);
alter table public.credit_line_members enable row level security;
do $$ begin
  create policy "Credits are viewable by everyone" on public.credit_line_members for select using (true);
exception when duplicate_object then null; end $$;
grant select on public.credit_line_members to anon, authenticated;

create table if not exists public.credit_line_companies (
  line_id uuid not null references public.choreographers(id) on delete cascade,
  company_id uuid not null references public.companies(id) on delete cascade,
  primary key (line_id, company_id)
);
alter table public.credit_line_companies enable row level security;
do $$ begin
  create policy "Credit companies are viewable by everyone" on public.credit_line_companies for select using (true);
exception when duplicate_object then null; end $$;
grant select on public.credit_line_companies to anon, authenticated;

create table if not exists public.credit_review (
  id bigserial primary key,
  line_id uuid not null references public.choreographers(id) on delete cascade,
  part text not null,
  candidates uuid[] not null default '{}',
  status text not null default 'open' check (status in ('open', 'resolved', 'ignored')),
  resolved_member uuid references public.choreographers(id) on delete set null,
  created_at timestamptz not null default now(),
  decided_at timestamptz,
  unique (line_id, part)
);
alter table public.credit_review enable row level security;
do $$ begin
  create policy "Admins read credit reviews" on public.credit_review for select to authenticated using (has_role(auth.uid(), 'admin'));
  create policy "Admins change credit reviews" on public.credit_review for update to authenticated using (has_role(auth.uid(), 'admin'));
exception when duplicate_object then null; end $$;
grant select, update on public.credit_review to authenticated;

-- 1. Split a credit line into its parts.
create or replace function public.split_credit_line(p_name text) returns text[]
language sql immutable set search_path to 'public' as $fn$
  with cleaned as (
    select regexp_replace(
             regexp_replace(coalesce(p_name, ''), $r$\s*\((after|nach|d.apr[eè]s|based on|frei nach|from|inspired by)[^)]*\)$r$, '', 'gi'),
             $r$\s*\(([^)]*)\)$r$, ', \1', 'g') s                 -- other brackets become a part of their own: (Kor'sia)
  ),
  parts as (
    select trim(both ' ,;:' from t.part) part, t.ord
      from cleaned,
           regexp_split_to_table(cleaned.s, $r$\s*(/|,|&|;|\+|\||\s(and|und|et|y|e|en|og|och|with|mit|avec|con|feat\.?|featuring)\s)\s*$r$, 'i')
             with ordinality as t(part, ord)
  )
  select coalesce(array_agg(part order by ord), '{}')
    from parts
   where length(part) >= 2
     and part !~* $r$^(after|nach|d.apr[eè]s|based on|frei nach|inspired by|from)\s$r$
     and part !~* $r$^(gäste|gaeste|guests?|others?|friends|company|ensemble|dancers|cast|et al\.?|collective|kollektiv|u\.a\.|etc\.?|various|diverse|verschiedene|tba|tbc|tbd|unknown|mixed|several|multiple|divers|various artists|choreograph(y|ie|ers?)|chorégraphie|coreografia|line-up tba|others)$$r$
$fn$;

-- 2. Who a part is: a person on the site, a company, one person with that surname, a new person, or unresolved.
create or replace function public.resolve_credit_part(p_part text,
  out member_id uuid, out company_id uuid, out candidates uuid[], out status text)
language plpgsql set search_path to 'public' as $fn$
declare s text := slugify(p_part);
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
  if p_part !~ '\s' then                       -- a surname alone ("Balanchine / Morau / Preljocaj / Xie")
    select coalesce(array_agg(c.id order by c.created_at), '{}') into candidates
      from choreographers c where c.kind = 'person' and slugify(c.name) like '%-' || s;
    if cardinality(candidates) = 1 then member_id := candidates[1]; status := 'person'; end if;
    return;
  end if;
  insert into choreographers (name, nationality, kind) values (trim(p_part), 'Unknown', 'person') returning id into member_id;
  status := 'new person';
end $fn$;

-- 3. (Re)build the members of one choreographer row.
create or replace function public.rebuild_credit_line(p_line uuid) returns void
language plpgsql set search_path to 'public' as $fn$
declare c record; parts text[]; part text; i int := 0; r record; v_resolved uuid;
begin
  select * into c from choreographers where id = p_line;
  if c.id is null then return; end if;
  parts := split_credit_line(c.name);
  delete from credit_line_members where line_id = p_line;
  delete from credit_line_companies where line_id = p_line;
  delete from credit_review where line_id = p_line and status = 'open';
  if cardinality(parts) = 1 and slugify(parts[1]) = slugify(c.name) then
    update choreographers set kind = 'person' where id = p_line and kind <> 'person';
    insert into credit_line_members values (p_line, p_line, 1) on conflict do nothing;
    return;
  end if;
  update choreographers set kind = 'credit_line' where id = p_line and kind <> 'credit_line';
  foreach part in array parts loop
    i := i + 1;
    select cr.resolved_member into v_resolved from credit_review cr
     where cr.line_id = p_line and cr.part = part and cr.status = 'resolved';
    if v_resolved is not null then
      insert into credit_line_members values (p_line, v_resolved, i) on conflict do nothing;
      continue;
    end if;
    select * into r from resolve_credit_part(part);
    if r.status in ('person', 'new person') then
      insert into credit_line_members values (p_line, r.member_id, i) on conflict do nothing;
    elsif r.status = 'company' then
      insert into credit_line_companies values (p_line, r.company_id) on conflict do nothing;
    elsif r.status = 'unresolved' then
      insert into credit_review (line_id, part, candidates) values (p_line, part, r.candidates)
      on conflict (line_id, part) do update set candidates = excluded.candidates where credit_review.status = 'open';
    end if;
  end loop;
end $fn$;

-- 4. An editor resolves a part: this person. A full name is remembered as an alias for next time.
create or replace function public.resolve_credit_review(p_id bigint, p_member uuid) returns void
language plpgsql set search_path to 'public' as $fn$
declare v record;
begin
  select * into v from credit_review where id = p_id;
  if v.id is null then return; end if;
  if p_member is null then
    update credit_review set status = 'ignored', decided_at = now() where id = p_id;
    return;
  end if;
  update credit_review set status = 'resolved', resolved_member = p_member, decided_at = now() where id = p_id;
  if v.part ~ '\s' then
    update choreographers set aliases = array_append(coalesce(aliases, '{}'), v.part)
     where id = p_member and not (v.part = any (coalesce(aliases, '{}')));
  end if;
  perform rebuild_credit_line(v.line_id);
end $fn$;
revoke execute on function public.resolve_credit_review(bigint, uuid) from anon;

-- 5. Every new or renamed choreographer row gets its members at once.
create or replace function public.choreographer_credits_trigger() returns trigger
language plpgsql set search_path to 'public' as $fn$
begin
  perform rebuild_credit_line(new.id);
  return null;
end $fn$;
drop trigger if exists choreographer_credits on public.choreographers;
create trigger choreographer_credits after insert or update of name on public.choreographers
  for each row execute function public.choreographer_credits_trigger();

-- 6. A company named in the credit line fills an empty company field.
create or replace function public.credit_company_fallback() returns trigger
language plpgsql set search_path to 'public' as $fn$
begin
  if new.company_id is null and new.choreographer_id is not null then
    select company_id into new.company_id from credit_line_companies where line_id = new.choreographer_id limit 1;
  end if;
  return new;
end $fn$;
drop trigger if exists credit_company_fallback on public.performances;
create trigger credit_company_fallback before insert or update of choreographer_id on public.performances
  for each row execute function public.credit_company_fallback();

-- 7. Performance -> people.
create or replace view public.performance_credits with (security_invoker = true) as
  select p.id as performance_id, m.member_id as choreographer_id, m.position
    from performances p join credit_line_members m on m.line_id = p.choreographer_id;
grant select on public.performance_credits to anon, authenticated;

-- 8. A show by a protected person is protected whoever else is on the bill.
create or replace function public.is_protected_performance(p_id uuid) returns boolean
language sql stable set search_path to 'public' as $fn$
  select exists (
    select 1 from performances p
      left join choreographers c on c.id = p.choreographer_id
      left join companies co on co.id = p.company_id
     where p.id = p_id and (
           coalesce(c.protected, false)
        or exists (select 1 from credit_line_members m join choreographers x on x.id = m.member_id
                    where m.line_id = p.choreographer_id and x.protected)
        or exists (select 1 from sources s where s.priority = 0 and s.kind ~* 'company'
                     and (s.company_id = p.company_id
                          or (c.name is not null and s.name ilike '%' || c.name || '%')
                          or (co.name is not null and s.name ilike '%' || co.name || '%')))))
$fn$;
