-- Fails on every served row whose chapter or article name still holds a doubly encoded letter
-- (UTF-8 read as Latin-1, such as `Ã§` for `ç`) or an edge space once staging has repaired them,
-- so a newly served one fails the build instead of landing in a name.
select
    source_row,
    chapter_name,
    article_name
from {{ ref('stg_nl_government__budget_moments') }}
where
    chapter_name like '%Ã%'
    or article_name like '%Ã%'
    or chapter_name like '%Â%'
    or article_name like '%Â%'
    or chapter_name <> trim(chapter_name)
    or article_name <> trim(article_name)
