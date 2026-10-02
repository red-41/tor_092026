-- Saffitt, 28 Sep 2026. Applied directly as migrations; kept here as a record.
-- v3_7   views respect access rules (source list no longer readable by visitors); fixed function search paths
alter view public.source_coverage set (security_invoker = true);
alter view public.title_is_company_name set (security_invoker = true);
alter view public.performance_listing set (security_invoker = true);
alter function public.desktop_stage set search_path = public, extensions;
alter function public.apply_duplicate_review(text) set search_path = public;
alter function public.is_venue_source(text) set search_path = public;
alter function public.source_rank(uuid, uuid, uuid) set search_path = public;
alter function public.is_generic_title(text, uuid) set search_path = public, extensions;
-- v3_7b  merges carry users' saved shows and journal entries to the kept row (move_user_refs)
-- v3_8   auth.uid() evaluated once per query in every policy; duplicate policies removed
-- v3_8b  indexes on every foreign key
-- v3_9   admin edits stick (editor listing, rank 0) and admin deletions are remembered (performance_suppressions)
-- v3_9b  sitemap_entries(): every public page with its last change, for the sitemap
-- v3_9c  pipeline functions executable only by the collector (service role)
-- v3_10  admins may read the review queues, listings per site and conflicts
do $$
declare t text;
begin
  foreach t in array array['duplicate_review','match_review','performance_conflicts','performance_sources','source_venues'] loop
    execute format('drop policy if exists "Admins can read %s" on public.%I', t, t);
    execute format('create policy "Admins can read %s" on public.%I for select to authenticated using (has_role((select auth.uid()), ''admin''::app_role))', t, t);
  end loop;
end $$;
