-- Fails on every pair of lines under one year, kind, chapter code and article number whose
-- figures neither repeat nor differ at any moment where both are non-zero: such rows could be one
-- article served on two rows with nothing repeated, so each new one needs a look before it stays
-- two lines. The pairs listed have been looked at: the unallocated reserve of 2024 follows the
-- layout of the chapter's split articles (draft and first supplementary on one row, September on
-- the other) but the file repeats nothing to prove it; chapter L holds the Nationaal Groeifonds
-- beside a Foreign Affairs or a Klimaatfonds article under one number, different articles.
{%- set figures = [
    'draft_budget_eur',
    'first_supplementary_eur',
    'september_supplementary_eur',
    'second_supplementary_eur',
    'outturn_eur',
] %}
{%- set known = [
    'nl-government-2024-U-IX-10-1',
    'nl-government-2024-V-IX-10-1',
    'nl-government-2025-O-L-2-1',
    'nl-government-2025-U-L-1-1',
    'nl-government-2026-O-L-2-1',
    'nl-government-2026-U-L-1-1',
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
    {%- for figure in figures %}
        (coalesce(earlier.{{ figure }}, 0) = 0 or coalesce(later.{{ figure }}, 0) = 0)
        {%- if not loop.last %} and {% endif %}
    {%- endfor %}
    and earlier.budget_line_key not in ('{{ known | join("', '") }}')
