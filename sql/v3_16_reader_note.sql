-- v3_16 (11 Nov 2026): an editors' note per site, sent to the page reader with every page of that site
-- (extract.py, "Note about this site from the editors"). Use it for a site's known quirks, e.g. printed times in UTC.
alter table public.sources add column if not exists reader_note text;

update public.sources set reader_note =
  'This site prints start times in UTC, so they read 1 hour early in winter and 2 hours early in summer. '
  || 'The real local start time is in the calendar file address (.../calendar/YYYYMMDD-HHMM-title.ics): use that time. '
  || 'Without a calendar file, convert the printed time from UTC to Europe/Paris.'
 where name like 'Le Quai (CDN Angers%';

-- readings of the site made without the note are not reused
delete from public.collector_page_cache where url like '%lequai-angers.eu%';
