-- v3_17 (11 Nov 2026): rebuild_credit_line looked for the next name of a credit line from the start of the line, so
-- "Mirka Eliášová / Eli and company" found "Eli" inside "Eliášová" and the text between the two names got a negative
-- length: "negative substring length not allowed". The error stopped the publish job of 11 Nov after 37 minutes, with
-- 10,169 rows still waiting. The next name is now looked for after the end of the previous one.
do $$
declare d text;
begin
  d := pg_get_functiondef('public.rebuild_credit_line(uuid)'::regprocedure);
  d := replace(d, 'p2 := position(v_next in c.name);',
    'p2 := case when p1 > 0 then position(v_next in substr(c.name, p1 + length(v_part))) else 0 end;');
  d := replace(d, 'if p1 > 0 and p2 > p1 then v_between := substr(c.name, p1 + length(v_part), p2 - p1 - length(v_part)); end if;',
    'if p2 > 0 then v_between := substr(c.name, p1 + length(v_part), p2 - 1); end if;');
  if position('p2 - 1); end if;' in d) = 0 or position('position(v_next in substr(' in d) = 0 then
    raise exception 'rebuild_credit_line: the expected lines were not found, nothing changed';
  end if;
  execute d;
end $$;
