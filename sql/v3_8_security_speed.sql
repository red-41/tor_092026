-- Saffitt v3, part 8 (applied 2026-09-28): security and speed, no behaviour change.
-- v3_7:  views performance_listing, source_coverage, title_is_company_name run with the caller's rights (security_invoker);
--        search_path pinned on desktop_stage, apply_duplicate_review, is_venue_source, source_rank, is_generic_title.
-- v3_7b: merge_performances moves users' saved shows and journal entries to the kept row (move_user_refs).
-- v3_8:  every RLS policy evaluates auth.uid() once per query: auth.uid() -> (select auth.uid());
--        duplicate policies dropped: "Admins can manage roles" (user_roles), "Users can update their own profile" (profiles).
-- v3_8b: indexes on all foreign keys the advisor listed, plus staging status and user_lists (entity_type, entity_id).
-- v3_9:  editor overrides and remembered removals (see v3_9_editor_overrides.sql).
-- v3_9b: sitemap_entries() for the website's sitemap.
-- v3_9c: pipeline functions executable by service_role only (revoked from PUBLIC, anon, authenticated).
do $$
declare f record;
begin
  for f in select p.oid::regprocedure sig from pg_proc p join pg_namespace n on n.oid = p.pronamespace
            where n.nspname = 'public' and p.proname in (
              'promote_staging_performances','apply_review','merge_performances','resolve_match','apply_duplicate_review',
              'find_duplicate_candidates','review_queue','review_housekeeping','recompute_performance','upsert_listing',
              'refresh_source_venues','suppress_performance','move_user_refs','desktop_stage','editor_listing',
              'remember_deleted_performance','performance_evidence','is_protected_performance')
  loop
    execute format('revoke execute on function %s from public, anon, authenticated', f.sig);
    execute format('grant execute on function %s to service_role', f.sig);
  end loop;
end $$;
