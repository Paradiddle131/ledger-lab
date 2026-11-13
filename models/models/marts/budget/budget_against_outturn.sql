-- One row per budget line of the Dutch central government, year and kind of amount, for the years
-- the fiscal calendar covers: each budget moment in its own column as the file serves it, the
-- year's current budget (the figure of the latest moment the file serves for that year) with the
-- moment it came from, the outturn in its own column beside it, and whether the line holds a zero
-- the file may have served in place of a figure.
--
-- A budget line is what the publisher serves for one article: usually one served row, but some
-- articles are served on two rows that each carry some of the moments and repeat the others.
-- Served rows of one year, kind, chapter code and article number are one line only on the file's
-- own evidence: at least one non-zero figure repeated across them, and no moment with two
-- different non-zero figures; the line then takes, at each moment, the one non-zero figure (a
-- repeat counted once), else zero. Otherwise each served row is its own line. The chapter name is
-- left out of the match, since the file serves one chapter under two names in one year.
-- A moment with no non-zero amount anywhere in a year is not served in the file for that year
-- (the September supplementary budget before 2023, or a moment the file has not been updated
-- with), so every line's amount at it is null, never a zero a sum would treat as a figure.
with served as (
    select * from {{ ref('stg_nl_government__budget_moments') }}
),

article_moments as (
    select
        fiscal_year,
        kind,
        chapter_code,
        article_number,
        moment,
        count(distinct case when amount_eur <> 0 then amount_eur end) as non_zero_figures,
        count(case when amount_eur <> 0 then 1 end) as non_zero_rows
    from served
    group by fiscal_year, kind, chapter_code, article_number, moment
),

articles as (
    select
        fiscal_year,
        kind,
        chapter_code,
        article_number,
        max(non_zero_figures) <= 1 and max(non_zero_rows) >= 2 as is_one_line
    from article_moments
    group by fiscal_year, kind, chapter_code, article_number
),

lined as (
    select
        served.*,
        case
            when articles.is_one_line
                then min(served.source_row) over (
                    partition by
                        served.fiscal_year,
                        served.kind,
                        served.chapter_code,
                        served.article_number
                )
            else served.source_row
        end as line_source_row
    from served
    inner join articles
        on
            served.fiscal_year = articles.fiscal_year
            and served.kind = articles.kind
            and served.chapter_code = articles.chapter_code
            and served.article_number = articles.article_number
),

served_moments as (
    select
        fiscal_year,
        moment
    from served
    group by fiscal_year, moment
    having max(case when amount_eur <> 0 then 1 else 0 end) = 1
),

line_moments as (
    select
        lined.fiscal_year,
        lined.kind,
        lined.chapter_code,
        lined.article_number,
        lined.line_source_row,
        lined.moment,
        count(*) as served_rows,
        cast(
            case
                when max(served_moments.moment) is not null
                    then coalesce(max(case when lined.amount_eur <> 0 then lined.amount_eur end), 0)
            end
            as {{ dbt.type_numeric() }}
        ) as amount_eur
    from lined
    left join served_moments
        on lined.fiscal_year = served_moments.fiscal_year and lined.moment = served_moments.moment
    group by
        lined.fiscal_year,
        lined.kind,
        lined.chapter_code,
        lined.article_number,
        lined.line_source_row,
        lined.moment
),

lines as (
    select
        line_moments.fiscal_year,
        line_moments.kind,
        line_moments.chapter_code,
        first_row.chapter_name,
        line_moments.article_number,
        first_row.article_name,
        line_moments.line_source_row,
        max(line_moments.served_rows) as served_rows,
        max(case when line_moments.moment = 'StandOWB' then line_moments.amount_eur end)
            as draft_budget_eur,
        max(case when line_moments.moment = 'StandO1' then line_moments.amount_eur end)
            as first_supplementary_eur,
        max(case when line_moments.moment = 'StandSBS' then line_moments.amount_eur end)
            as september_supplementary_eur,
        max(case when line_moments.moment = 'StandO2' then line_moments.amount_eur end)
            as second_supplementary_eur,
        max(case when line_moments.moment = 'Realisatie' then line_moments.amount_eur end)
            as outturn_eur
    from line_moments
    inner join served as first_row
        on
            line_moments.line_source_row = first_row.source_row
            and line_moments.moment = first_row.moment
    group by
        line_moments.fiscal_year,
        line_moments.kind,
        line_moments.chapter_code,
        first_row.chapter_name,
        line_moments.article_number,
        first_row.article_name,
        line_moments.line_source_row
),

covered_years as (
    select distinct
        entity_id,
        fiscal_year
    from {{ ref('fiscal_periods') }}
    where entity_id = 'nl-government'
),

numbered as (
    select
        lines.*,
        covered_years.entity_id,
        row_number() over (
            partition by lines.fiscal_year, lines.kind, lines.chapter_code, lines.article_number
            order by lines.line_source_row
        ) as line_in_article
    from lines
    inner join covered_years on lines.fiscal_year = covered_years.fiscal_year
)

select
    {{ dbt.concat([
        dbt.string_literal('nl-government'), "'-'",
        dbt.cast('fiscal_year', dbt.type_string()), "'-'",
        'kind', "'-'",
        'chapter_code', "'-'",
        'article_number', "'-'",
        dbt.cast('line_in_article', dbt.type_string()),
    ]) }} as budget_line_key,
    entity_id,
    'EUR' as currency,
    fiscal_year,
    kind,
    chapter_code,
    chapter_name,
    article_number,
    article_name,
    line_in_article,
    served_rows,
    draft_budget_eur,
    first_supplementary_eur,
    september_supplementary_eur,
    second_supplementary_eur,
    coalesce(
        second_supplementary_eur,
        september_supplementary_eur,
        first_supplementary_eur,
        draft_budget_eur
    ) as current_budget_eur,
    case
        when second_supplementary_eur is not null then 'StandO2'
        when september_supplementary_eur is not null then 'StandSBS'
        when first_supplementary_eur is not null then 'StandO1'
        when draft_budget_eur is not null then 'StandOWB'
    end as current_budget_moment,
    outturn_eur,
    coalesce(
        -- A draft of zero before a non-zero figure at every later moment the file serves.
        (
            draft_budget_eur = 0
            and coalesce(first_supplementary_eur, 1) <> 0
            and coalesce(september_supplementary_eur, 1) <> 0
            and coalesce(second_supplementary_eur, 1) <> 0
            and coalesce(outturn_eur, 1) <> 0
        )
        -- A later budget moment served as zero between non-zero figures of the line.
        or (
            first_supplementary_eur = 0
            and draft_budget_eur <> 0
            and (
                coalesce(september_supplementary_eur, 0) <> 0
                or coalesce(second_supplementary_eur, 0) <> 0
                or coalesce(outturn_eur, 0) <> 0
            )
        )
        or (
            september_supplementary_eur = 0
            and (draft_budget_eur <> 0 or coalesce(first_supplementary_eur, 0) <> 0)
            and (coalesce(second_supplementary_eur, 0) <> 0 or coalesce(outturn_eur, 0) <> 0)
        )
        or (
            second_supplementary_eur = 0
            and (
                draft_budget_eur <> 0
                or coalesce(first_supplementary_eur, 0) <> 0
                or coalesce(september_supplementary_eur, 0) <> 0
            )
            and coalesce(outturn_eur, 0) <> 0
        ),
        false
    ) as has_doubtful_zero
from numbered
