-- The Department for Transport's payments in the shape every entity's payments share: one row per
-- row of the latest file of each month, in pounds, on the UK's April to March periods of the
-- payment date. The calendar join is a left join, so a date the calendar lacks keeps its row and
-- fails the date test instead of dropping out.
select
    'uk-transport:' || versions.source_file || ':'
    || cast(versions.source_row as {{ dbt.type_string() }}) as payment_key,
    'uk-transport' as entity_id,
    'GBP' as currency,
    versions.payment_date,
    fiscal_calendar.fiscal_year,
    fiscal_calendar.fiscal_period,
    versions.amount,
    versions.supplier as vendor_name,
    versions.supplier like 'person:%' as vendor_is_hashed,
    versions.transaction_number as document_number,
    cast(null as {{ dbt.type_string() }}) as contract_number,
    cast(null as {{ dbt.type_numeric() }}) as award_after_revisions,
    'uk-transport-spend-2025' as source_table,
    false as is_repeat
from {{ ref('int_uk_transport__payment_versions') }} as versions
left join {{ ref('fiscal_calendar') }} as fiscal_calendar
    on
        fiscal_calendar.entity_id = 'uk-transport'
        and versions.payment_date = fiscal_calendar.calendar_date
where versions.is_current
