-- Fails on every line made of two served rows whose rows repeat no non-zero figure at any moment:
-- two rows become one line only on the file's evidence, a figure served on both.
with repeats as (
    select
        fiscal_year,
        kind,
        chapter_code,
        article_number
    from {{ ref('stg_nl_government__budget_moments') }}
    where amount_eur <> 0
    group by fiscal_year, kind, chapter_code, article_number, moment, amount_eur
    having count(*) >= 2
)

select lines.budget_line_key
from {{ ref('budget_against_outturn') }} as lines
left join (select distinct * from repeats) as repeated
    on
        lines.fiscal_year = repeated.fiscal_year
        and lines.kind = repeated.kind
        and lines.chapter_code = repeated.chapter_code
        and lines.article_number = repeated.article_number
where lines.served_rows = 2 and repeated.fiscal_year is null
