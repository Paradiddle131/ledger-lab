-- One rate per publication date and currency, each positive and with no more decimal places
-- than the six the decimal type keeps, so typing never rounds a published figure.
select
    published_on,
    currency
from {{ ref('fx_published_rates') }}
group by published_on, currency
having count(*) != 1
union all
select
    published_on,
    currency
from {{ ref('fx_published_rates') }}
where
    units_per_euro <= 0
    or {{ dbt.length(dbt.split_part('published_rate', "'.'", 2)) }} > 6
