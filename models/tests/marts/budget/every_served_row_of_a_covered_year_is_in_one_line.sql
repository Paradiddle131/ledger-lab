-- Fails on every covered year and kind whose lines are made of more or fewer served rows than the
-- file serves for it, so no served row is dropped or counted in two lines.
with served as (
    select
        fiscal_year,
        kind,
        count(distinct source_row) as served_rows
    from {{ ref('stg_nl_government__budget_moments') }}
    group by fiscal_year, kind
),

covered as (
    select distinct fiscal_year
    from {{ ref('fiscal_periods') }}
    where entity_id = 'nl-government'
),

lined as (
    select
        fiscal_year,
        kind,
        sum(served_rows) as served_rows
    from {{ ref('budget_against_outturn') }}
    group by fiscal_year, kind
)

select
    served.fiscal_year,
    served.kind,
    served.served_rows,
    lined.served_rows as lined_rows
from served
inner join covered on served.fiscal_year = covered.fiscal_year
left join lined on served.fiscal_year = lined.fiscal_year and served.kind = lined.kind
where lined.served_rows is null or lined.served_rows <> served.served_rows
