-- Saffitt v3, part 11h: the credit queue after the first full run.
-- 1. A row that turned out to be one person closes any review left from an earlier split ("(LA)HORDE").
-- 2. A first name followed by "& First Surname" borrows that surname: "Imre & Marne van Opstal" credits
--    Imre van Opstal and Marne van Opstal; "Christian et François Ben Aïm" credits Christian Ben Aïm.
--    Only when the two parts are joined by an "and" word (not by "/" or ","), and the first part has no surname match.
-- 3. The people list (choreographer_listing) is readable by the public site: it only carries names and counts.
-- 4. Listing views lose the write grants Supabase adds by default.

create or replace function public.rebuild_credit_line(p_line uuid) returns void
language plpgsql set search_path to 'public' as $function$
declare c record; v_parts text[]; v_part text; r record; v_resolved uuid; i int := 0;
        v_members uuid[] := '{}'; v_companies uuid[] := '{}';
        v_next text; v_between text; v_try text; p1 int; p2 int;
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
    select cr.resolved_member into v_resolved from credit_review cr
     where cr.line_id = p_line and cr.part = v_part and cr.status = 'resolved';
    if v_resolved is not null then
      if not (v_resolved = any (v_members)) then v_members := v_members || v_resolved; end if;
      continue;
    end if;
    select * into r from resolve_credit_part(v_part);
    -- a lone first name joined by "and" to "First Surname": try "First-name Surname"
    if r.status = 'unresolved' and v_part !~ '\s' and i < cardinality(v_parts) then
      v_next := v_parts[i + 1];
      p1 := position(v_part in c.name); p2 := position(v_next in c.name);
      if v_next ~ '\s' and p1 > 0 and p2 > p1 then
        v_between := substr(c.name, p1 + length(v_part), p2 - p1 - length(v_part));
        if v_between ~* '^\s*(&|\+|and|et|und|y|og|och|e|en)\s*$' then
          v_try := v_part || ' ' || regexp_replace(v_next, '^\S+\s+', '');
          select * into r from resolve_credit_part(v_try);
          if r.status in ('person', 'new person') then
            update credit_review set status = 'resolved', resolved_member = r.member_id, decided_at = now()
             where line_id = p_line and part = v_part and status = 'open';
          end if;
        end if;
      end if;
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

-- 3. NOT applied (needs the owner's decision, see v3_11_README.md): making the people list run with the owner's
--    rights so the public site can read its counts:
--    alter view public.choreographer_listing set (security_invoker = false);

-- 4. Views are read-only for the API roles.
revoke insert, update, delete, truncate, references, trigger on public.evening_listing, public.choreographer_listing,
  public.performance_listing, public.performance_credits from anon, authenticated;
