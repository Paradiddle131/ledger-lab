-- One row per budget line of the Dutch central government, year and kind of amount, for the years
-- the fiscal calendar covers: each budget moment in its own column, the year's current budget
-- (the latest moment the file has reached that year) with the moment it came from, and the
-- outturn in its own column beside it.
--
-- A budget line is what the publisher serves for one article: usually one served row, but some
-- articles are served on two rows that each carry some of the moments and repeat the others.
-- Served rows of the same year, kind, chapter (code and name) and article number are one line when
-- no moment carries two different non-zero amounts across them; the line then takes, at each
-- moment, the one non-zero amount (a repeat counted once), else zero. Rows that do carry two
-- different non-zero amounts at a moment are different articles sharing a code, and each is its
-- own line. A moment that has no non-zero amount anywhere in a year has not been reached that
-- year (or did not exist then, such as the September supplementary budget before 2023), so every
-- line's amount at it is null, never a zero a sum would treat as real.
with served as (
    select * from {{ ref('stg_nl_government__budget_moments') }}
),

article_moments as (
    select
        fiscal_year,
        kind,
        chapter_code,
        chapter_name,
        article_number,
        moment,
        count(distinct case when amount_eur <> 0 then amount_eur end) as non_zero_amounts
    from served
    group by fiscal_year, kind, chapter_code, chapter_name, article_number, moment
),

articles as (
    select
        fiscal_year,
        kind,
        chapter_code,
        chapter_name,
        article_number,
        max(non_zero_amounts) <= 1 as is_one_line
    from article_moments
    group by fiscal_year, kind, chapter_code, chapter_name, article_number
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
                        served.chapter_name,
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
            and served.chapter_name = articles.chapter_name
            and served.article_number = articles.article_number
),

reached as (
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
        lined.chapter_name,
        lined.article_number,
        lined.line_source_row,
        lined.moment,
        count(*) as served_rows,
        cast(
            case
                when max(reached.moment) is not null
                    then coalesce(max(case when lined.amount_eur <> 0 then lined.amount_eur end), 0)
            end
            as {{ dbt.type_numeric() }}
        ) as amount_eur
    from lined
    left join reached
        on lined.fiscal_year = reached.fiscal_year and lined.moment = reached.moment
    group by
        lined.fiscal_year,
        lined.kind,
        lined.chapter_code,
        lined.chapter_name,
        lined.article_number,
        lined.line_source_row,
        lined.moment
),

lines as (
    select
        line_moments.fiscal_year,
        line_moments.kind,
        line_moments.chapter_code,
        line_moments.chapter_name,
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
        line_moments.chapter_name,
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
    outturn_eur
from numbered
