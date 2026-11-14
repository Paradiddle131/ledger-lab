-- The group-totals data product: one row per entity, span and basis, the span being one of the
-- entity's fiscal periods, or its whole fiscal year for figures the source gives only by year,
-- summing the figures that count (`int_group_totals__figures` with no rule leaving them out) in the
-- entity's own currency and in the reporting currency at actual and at constant rates. A group
-- total for a span is the sum of its rows across entities, read with each row's basis.
select
    {{ dbt.concat([
        'entity_id', "'-'",
        dbt.cast('fiscal_year', dbt.type_string()), "'-'",
        "coalesce(" ~ dbt.cast('fiscal_period', dbt.type_string()) ~ ", 'year')", "'-'",
        'basis',
    ]) }} as group_total_key,
    entity_id,
    fiscal_year,
    fiscal_period,
    period_start,
    period_end,
    basis,
    count(*) as figures,
    currency,
    sum(amount) as amount,
    reporting_currency,
    sum(amount_at_actual_rates) as amount_at_actual_rates,
    sum(amount_at_constant_rates) as amount_at_constant_rates,
    constant_rates_on
from {{ ref('int_group_totals__figures') }}
where left_out_by is null
group by
    entity_id,
    fiscal_year,
    fiscal_period,
    period_start,
    period_end,
    basis,
    currency,
    reporting_currency,
    constant_rates_on
