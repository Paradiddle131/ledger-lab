-- What the group totals leave out, beside them: one row per entity, fiscal year (null where the
-- calendars do not cover it), basis and rule, with the count and own-currency amount of the
-- figures that rule leaves out, so a consumer sees each gap and its size, and the distinct payees
-- they were paid to (a figure with no payee counting as its own).
select
    entity_id,
    fiscal_year,
    basis,
    left_out_by,
    count(*) as figures,
    count(distinct coalesce(payee, figure_key)) as payees,
    currency,
    sum(amount) as amount
from {{ ref('int_group_totals__figures') }}
where left_out_by is not null
group by entity_id, fiscal_year, basis, left_out_by, currency
