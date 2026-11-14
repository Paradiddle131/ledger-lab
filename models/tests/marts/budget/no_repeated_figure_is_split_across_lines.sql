-- Fails on every pair of lines under one year, kind, chapter code and article number that carry
-- the same non-zero figure at the same moment: a repeat is the file's evidence that two served
-- rows are one article, so lines sharing one were split apart. The pairs listed are the
-- publisher's own error, not a split: in 2026 the Algemene Zaken article filed under chapter IIB
-- carries the Raad van State's September figure.
{%- set figures = [
    'draft_budget_eur',
    'first_supplementary_eur',
    'september_supplementary_eur',
    'second_supplementary_eur',
    'outturn_eur',
] %}
{%- set known = [
    'nl-government-2026-O-IIB-1-1',
    'nl-government-2026-U-IIB-1-1',
] %}

select
    earlier.budget_line_key,
    later.budget_line_key as later_line_key
from {{ ref('budget_against_outturn') }} as earlier
inner join {{ ref('budget_against_outturn') }} as later
    on
        earlier.fiscal_year = later.fiscal_year
        and earlier.kind = later.kind
        and earlier.chapter_code = later.chapter_code
        and earlier.article_number = later.article_number
        and earlier.line_in_article < later.line_in_article
where
    (
        {%- for figure in figures %}
            (earlier.{{ figure }} <> 0 and earlier.{{ figure }} = later.{{ figure }})
            {%- if not loop.last %} or {% endif %}
        {%- endfor %}
    )
    and earlier.budget_line_key not in ('{{ known | join("', '") }}')
