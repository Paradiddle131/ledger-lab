-- One row per ECB publication date and currency: how many units of the currency one euro buys,
-- typed as a decimal (never a float, so the published figure survives), beside the figure as
-- published; the euro's own leg is 1 on every publication date, so every translation runs
-- through the euro the same way. The `ecb_rates` var picks the rates: the committed sample of
-- real rates (`sample`) or the file the fetch kit landed (`landed`).
{%- set chosen = var('ecb_rates') %}
{%- if chosen == 'landed' %}
    {%- set rates = source('ecb', 'rates') %}
{%- elif chosen == 'sample' %}
    {%- set rates = ref('ecb_rates_sample') %}
{%- else %}
    {{ exceptions.raise_compiler_error("the ecb_rates var is 'sample' or 'landed', not '" ~ chosen ~ "'") }}
{%- endif %}

with served as (
    select
        cast({{ adapter.quote('date') }} as date) as published_on,
        currency,
        rate as published_rate,
        cast(rate as {{ dbt.type_numeric() }}) as units_per_euro
    from {{ rates }}
)

select
    published_on,
    currency,
    published_rate,
    units_per_euro
from served

union all

select distinct
    published_on,
    'EUR' as currency,
    '1' as published_rate,
    cast(1 as {{ dbt.type_numeric() }}) as units_per_euro
from served
