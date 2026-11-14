-- The mart holds each entity's payments once: as many rows as the entity's own model (Chicago's
-- staging, every served row; the UK's intermediate payments, each month's latest file), so a join
-- that repeats or drops a payment, or an entity left out of the union, fails here.
with served as (
    select 'chicago' as entity_id, count(*) as payments from {{ ref('stg_chicago__payments') }}
    union all
    select 'uk-transport', count(*) from {{ ref('int_uk_transport__payments') }}
),

mart as (
    select entity_id, count(*) as payments from {{ ref('payments') }} group by entity_id
)

select served.entity_id, served.payments as served, mart.payments as in_mart
from served
full outer join mart on served.entity_id = mart.entity_id
where coalesce(served.payments, -1) <> coalesce(mart.payments, -1)
