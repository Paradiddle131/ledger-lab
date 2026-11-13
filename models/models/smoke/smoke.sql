-- Proves the engine runs the dialect the layered models will use: a CTE, a window function
-- filtered with QUALIFY, and a cross-database date macro cast to a date (DuckDB's truncation
-- returns a timestamp where BigQuery's returns a date). It reads no source.
with amounts as (
    select 1 as entity_id, cast('2025-04-30' as date) as posted_on, 100 as amount
    union all
    select 1, cast('2025-05-31' as date), 250
    union all
    select 2, cast('2025-04-30' as date), 75
)

select
    entity_id,
    posted_on,
    amount,
    cast({{ dbt.date_trunc('month', 'posted_on') }} as date) as posted_month
from amounts
qualify row_number() over (partition by entity_id order by posted_on desc) = 1
