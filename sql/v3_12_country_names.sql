-- Saffitt v3, part 12: one spelling per country (10 October 2026).
-- Some sources gave countries as codes ("BE", "NL", "UK") or in their own language ("Autriche", "Italia",
-- "Suisse"), so the site's country filter showed Belgium and "BE" side by side.
-- 1. normalize_country(text): the English name used on the site for a code (ISO 2 or 3 letters), a local or
--    foreign name, "the Netherlands", "Catalunya, Spain" (last part after a comma). Unknown text is kept as is.
-- 2. A BEFORE INSERT/UPDATE trigger on every table the collector writes a country into, so new rows can never
--    bring a code back.
-- 3. The existing rows are corrected (separate statement, see the bottom).

create or replace function public.normalize_country(p text) returns text
language plpgsql immutable set search_path to 'public' as $function$
declare
  v text; cand text; r text; c text;
  canon constant text[] := array[
    'Albania','Andorra','Argentina','Armenia','Australia','Austria','Azerbaijan','Belarus','Belgium',
    'Bosnia and Herzegovina','Brazil','Bulgaria','Cambodia','Canada','Chile','China','Colombia','Croatia','Cuba',
    'Cyprus','Czech Republic','Denmark','Egypt','Estonia','Finland','France','Georgia','Germany','Greece',
    'Hong Kong','Hungary','Iceland','India','Ireland','Israel','Italy','Japan','Kazakhstan','Kosovo','Latvia',
    'Lebanon','Liechtenstein','Lithuania','Luxembourg','Malta','Mexico','Moldova','Monaco','Montenegro','Morocco',
    'Netherlands','New Zealand','North Macedonia','Norway','Oman','Panama','Poland','Portugal','Qatar','Romania',
    'Russia','San Marino','Saudi Arabia','Serbia','Singapore','Slovakia','Slovenia','South Africa','South Korea',
    'Spain','Sweden','Switzerland','Taiwan','Tunisia','Turkey','Ukraine','United Arab Emirates',
    'United Kingdom','United States','Uzbekistan','Vietnam'];
begin
  v := nullif(btrim(regexp_replace(coalesce(p, ''), '\s+', ' ', 'g')), '');
  if v is null then return null; end if;
  foreach cand in array array[
      lower(regexp_replace(v, '^the\s+', '', 'i')),
      case when v like '%,%' then lower(btrim(regexp_replace(v, '^.*,', ''))) end,
      case when v like '%(%' then lower(btrim(regexp_replace(v, '\s*\(.*$', ''))) end]
  loop
    continue when cand is null or cand = '';
    cand := regexp_replace(cand, '\.', '', 'g');            -- "U.K.", "U.S.A."
    foreach c in array canon loop
      if lower(c) = cand then return c; end if;
    end loop;
    r := case cand
      -- ISO 3166 alpha-2
      when 'al' then 'Albania' when 'ad' then 'Andorra' when 'ar' then 'Argentina' when 'am' then 'Armenia'
      when 'au' then 'Australia' when 'at' then 'Austria' when 'az' then 'Azerbaijan' when 'by' then 'Belarus'
      when 'be' then 'Belgium' when 'ba' then 'Bosnia and Herzegovina' when 'br' then 'Brazil'
      when 'bg' then 'Bulgaria' when 'kh' then 'Cambodia' when 'ca' then 'Canada' when 'cl' then 'Chile'
      when 'cn' then 'China' when 'co' then 'Colombia' when 'hr' then 'Croatia' when 'cu' then 'Cuba'
      when 'cy' then 'Cyprus' when 'cz' then 'Czech Republic' when 'dk' then 'Denmark' when 'eg' then 'Egypt'
      when 'ee' then 'Estonia' when 'fi' then 'Finland' when 'fr' then 'France' when 'ge' then 'Georgia'
      when 'de' then 'Germany' when 'gr' then 'Greece' when 'el' then 'Greece' when 'hk' then 'Hong Kong'
      when 'hu' then 'Hungary' when 'is' then 'Iceland' when 'in' then 'India' when 'ie' then 'Ireland'
      when 'il' then 'Israel' when 'it' then 'Italy' when 'jp' then 'Japan' when 'kz' then 'Kazakhstan'
      when 'xk' then 'Kosovo' when 'lv' then 'Latvia' when 'lb' then 'Lebanon' when 'li' then 'Liechtenstein'
      when 'lt' then 'Lithuania' when 'lu' then 'Luxembourg' when 'mt' then 'Malta' when 'mx' then 'Mexico'
      when 'md' then 'Moldova' when 'mc' then 'Monaco' when 'me' then 'Montenegro' when 'ma' then 'Morocco'
      when 'nl' then 'Netherlands' when 'nz' then 'New Zealand' when 'mk' then 'North Macedonia'
      when 'no' then 'Norway' when 'om' then 'Oman' when 'pa' then 'Panama' when 'pl' then 'Poland'
      when 'pt' then 'Portugal' when 'qa' then 'Qatar' when 'ro' then 'Romania' when 'ru' then 'Russia'
      when 'sm' then 'San Marino' when 'sa' then 'Saudi Arabia' when 'rs' then 'Serbia' when 'sg' then 'Singapore'
      when 'sk' then 'Slovakia' when 'si' then 'Slovenia' when 'za' then 'South Africa' when 'kr' then 'South Korea'
      when 'es' then 'Spain' when 'se' then 'Sweden' when 'ch' then 'Switzerland' when 'tw' then 'Taiwan'
      when 'tn' then 'Tunisia' when 'tr' then 'Turkey' when 'ua' then 'Ukraine' when 'ae' then 'United Arab Emirates'
      when 'gb' then 'United Kingdom' when 'uk' then 'United Kingdom' when 'us' then 'United States'
      when 'uz' then 'Uzbekistan' when 'vn' then 'Vietnam'
      -- ISO 3166 alpha-3 (the common ones)
      when 'alb' then 'Albania' when 'arm' then 'Armenia' when 'aut' then 'Austria' when 'bel' then 'Belgium'
      when 'bgr' then 'Bulgaria' when 'bih' then 'Bosnia and Herzegovina' when 'can' then 'Canada'
      when 'che' then 'Switzerland' when 'chn' then 'China' when 'cyp' then 'Cyprus' when 'cze' then 'Czech Republic'
      when 'deu' then 'Germany' when 'ger' then 'Germany' when 'dnk' then 'Denmark' when 'esp' then 'Spain'
      when 'est' then 'Estonia' when 'fin' then 'Finland' when 'fra' then 'France' when 'gbr' then 'United Kingdom'
      when 'geo' then 'Georgia' when 'grc' then 'Greece' when 'hrv' then 'Croatia' when 'hun' then 'Hungary'
      when 'irl' then 'Ireland' when 'isl' then 'Iceland' when 'isr' then 'Israel' when 'ita' then 'Italy'
      when 'jpn' then 'Japan' when 'ltu' then 'Lithuania' when 'lux' then 'Luxembourg' when 'lva' then 'Latvia'
      when 'mco' then 'Monaco' when 'mda' then 'Moldova' when 'mkd' then 'North Macedonia' when 'mlt' then 'Malta'
      when 'nld' then 'Netherlands' when 'nor' then 'Norway' when 'pol' then 'Poland' when 'prt' then 'Portugal'
      when 'rou' then 'Romania' when 'rus' then 'Russia' when 'srb' then 'Serbia' when 'svk' then 'Slovakia'
      when 'svn' then 'Slovenia' when 'swe' then 'Sweden' when 'tur' then 'Turkey' when 'twn' then 'Taiwan'
      when 'ukr' then 'Ukraine' when 'usa' then 'United States'
      -- local and foreign names
      when 'österreich' then 'Austria' when 'osterreich' then 'Austria' when 'autriche' then 'Austria'
      when 'oostenrijk' then 'Austria'
      when 'belgique' then 'Belgium' when 'belgië' then 'Belgium' when 'belgie' then 'Belgium'
      when 'belgien' then 'Belgium' when 'belgio' then 'Belgium' when 'bélgica' then 'Belgium'
      when 'schweiz' then 'Switzerland' when 'suisse' then 'Switzerland' when 'svizzera' then 'Switzerland'
      when 'svizra' then 'Switzerland' when 'suiza' then 'Switzerland' when 'zwitserland' then 'Switzerland'
      when 'deutschland' then 'Germany' when 'allemagne' then 'Germany' when 'germania' then 'Germany'
      when 'alemania' then 'Germany' when 'duitsland' then 'Germany' when 'tyskland' then 'Germany'
      when 'niemcy' then 'Germany' when 'německo' then 'Germany' when 'saksa' then 'Germany'
      when 'españa' then 'Spain' when 'espana' then 'Spain' when 'espagne' then 'Spain' when 'spagna' then 'Spain'
      when 'spanien' then 'Spain' when 'spanje' then 'Spain' when 'catalunya' then 'Spain' when 'catalonia' then 'Spain'
      when 'cataluña' then 'Spain' when 'catalogne' then 'Spain' when 'euskadi' then 'Spain'
      when 'italia' then 'Italy' when 'italie' then 'Italy' when 'italien' then 'Italy' when 'italië' then 'Italy'
      when 'nederland' then 'Netherlands' when 'pays-bas' then 'Netherlands' when 'paesi bassi' then 'Netherlands'
      when 'países bajos' then 'Netherlands' when 'niederlande' then 'Netherlands' when 'holland' then 'Netherlands'
      when 'danmark' then 'Denmark' when 'dänemark' then 'Denmark' when 'danemark' then 'Denmark'
      when 'sverige' then 'Sweden' when 'suède' then 'Sweden' when 'suede' then 'Sweden' when 'svezia' then 'Sweden'
      when 'schweden' then 'Sweden' when 'suecia' then 'Sweden' when 'zweden' then 'Sweden'
      when 'norge' then 'Norway' when 'noreg' then 'Norway' when 'norvège' then 'Norway' when 'norvege' then 'Norway'
      when 'norwegen' then 'Norway'
      when 'suomi' then 'Finland' when 'finlande' then 'Finland' when 'finnland' then 'Finland'
      when 'polska' then 'Poland' when 'pologne' then 'Poland' when 'polen' then 'Poland'
      when 'česko' then 'Czech Republic' when 'cesko' then 'Czech Republic' when 'česká republika' then 'Czech Republic'
      when 'ceska republika' then 'Czech Republic' when 'czechia' then 'Czech Republic'
      when 'tchéquie' then 'Czech Republic' when 'tschechien' then 'Czech Republic'
      when 'magyarország' then 'Hungary' when 'magyarorszag' then 'Hungary' when 'hongrie' then 'Hungary'
      when 'ungarn' then 'Hungary'
      when 'hrvatska' then 'Croatia' when 'croatie' then 'Croatia' when 'kroatien' then 'Croatia'
      when 'slovensko' then 'Slovakia' when 'slovaquie' then 'Slovakia' when 'slowakei' then 'Slovakia'
      when 'slovenija' then 'Slovenia' when 'slovénie' then 'Slovenia' when 'slowenien' then 'Slovenia'
      when 'srbija' then 'Serbia' when 'србија' then 'Serbia' when 'serbie' then 'Serbia' when 'serbien' then 'Serbia'
      when 'ελλάδα' then 'Greece' when 'ελλάς' then 'Greece' when 'hellas' then 'Greece' when 'grèce' then 'Greece'
      when 'grece' then 'Greece' when 'griechenland' then 'Greece'
      when 'россия' then 'Russia' when 'russie' then 'Russia' when 'russland' then 'Russia'
      when 'russian federation' then 'Russia'
      when 'україна' then 'Ukraine'
      when 'türkiye' then 'Turkey' when 'turkiye' then 'Turkey' when 'turquie' then 'Turkey' when 'türkei' then 'Turkey'
      when 'eesti' then 'Estonia' when 'estonie' then 'Estonia' when 'estland' then 'Estonia'
      when 'latvija' then 'Latvia' when 'lettonie' then 'Latvia' when 'lettland' then 'Latvia'
      when 'lietuva' then 'Lithuania' when 'lituanie' then 'Lithuania' when 'litauen' then 'Lithuania'
      when 'éire' then 'Ireland' when 'eire' then 'Ireland' when 'irlande' then 'Ireland' when 'irland' then 'Ireland'
      when 'republic of ireland' then 'Ireland'
      when 'lëtzebuerg' then 'Luxembourg' when 'letzebuerg' then 'Luxembourg' when 'luxemburg' then 'Luxembourg'
      when 'ísland' then 'Iceland' when 'islande' then 'Iceland'
      when 'românia' then 'Romania' when 'roumanie' then 'Romania' when 'rumänien' then 'Romania'
      when 'българия' then 'Bulgaria' when 'bulgarie' then 'Bulgaria' when 'bulgarien' then 'Bulgaria'
      when 'royaume-uni' then 'United Kingdom' when 'regno unito' then 'United Kingdom'
      when 'reino unido' then 'United Kingdom' when 'vereinigtes königreich' then 'United Kingdom'
      when 'great britain' then 'United Kingdom' when 'britain' then 'United Kingdom'
      when 'england' then 'United Kingdom' when 'scotland' then 'United Kingdom' when 'wales' then 'United Kingdom'
      when 'northern ireland' then 'United Kingdom'
      when 'united states of america' then 'United States' when 'états-unis' then 'United States'
      when 'etats-unis' then 'United States' when 'vereinigte staaten' then 'United States'
      when 'principauté de monaco' then 'Monaco'
      when 'sakartvelo' then 'Georgia'
      when 'maroc' then 'Morocco' when 'marokko' then 'Morocco'
      when 'korea' then 'South Korea' when 'republic of korea' then 'South Korea'
      when 'macedonia' then 'North Macedonia' when 'северна македонија' then 'North Macedonia'
      when 'bosnia' then 'Bosnia and Herzegovina' when 'bosna i hercegovina' then 'Bosnia and Herzegovina'
      when 'republic of moldova' then 'Moldova'
      when 'uae' then 'United Arab Emirates'
      else null end;
    if r is not null then return r; end if;
  end loop;
  return v;
end $function$;

create or replace function public.normalize_country_trg() returns trigger
language plpgsql set search_path to 'public' as $function$
begin
  if new.country is not null then
    new.country := normalize_country(new.country);
  end if;
  return new;
end $function$;

-- "aa_" so it runs before the other BEFORE triggers of these tables.
create or replace trigger aa_normalize_country before insert or update of country on public.performances
  for each row execute function public.normalize_country_trg();
create or replace trigger aa_normalize_country before insert or update of country on public.theaters
  for each row execute function public.normalize_country_trg();
create or replace trigger aa_normalize_country before insert or update of country on public.companies
  for each row execute function public.normalize_country_trg();
create or replace trigger aa_normalize_country before insert or update of country on public.sources
  for each row execute function public.normalize_country_trg();
create or replace trigger aa_normalize_country before insert or update of country on public.import_staging_performances
  for each row execute function public.normalize_country_trg();
create or replace trigger aa_normalize_country before insert or update of country on public.production_runs
  for each row execute function public.normalize_country_trg();

-- 3. Data fix (run once):
-- update public.theaters set country = normalize_country(country) where country is distinct from normalize_country(country);
-- update public.performances set country = normalize_country(country) where country is distinct from normalize_country(country);
-- update public.companies set country = normalize_country(country) where country is distinct from normalize_country(country);
-- update public.sources set country = normalize_country(country) where country is distinct from normalize_country(country);
-- update public.production_runs set country = normalize_country(country) where country is distinct from normalize_country(country);
-- update public.import_staging_performances set country = normalize_country(country) where country is distinct from normalize_country(country);
