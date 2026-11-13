-- One row per payment of the City, in the payments mart's column shape and order, then the City's
-- own detail. A payment joins its contract by the served contract number, against one row per
-- contract, so no payment is ever repeated; `DV` (a direct voucher, paid on no contract) becomes
-- no contract number, and a number the contracts table lacks stays, with no award.
-- A dated payment takes the fiscal year and period of its day; a rolled-up row takes the fiscal
-- year that is exactly its calendar year (the City's year runs January to December) and no
-- period, and the 2002 row, which holds 1996 to 2002, takes neither. A year the calendars do not
-- cover has neither.
with payments as (
    select * from {{ ref('stg_chicago__payments') }}
),

{%- set year = 'cast(payments.rolled_up_year as ' ~ dbt.type_string() ~ ')' %}

fiscal_years as (
    select
        entity_id,
        fiscal_year,
        min(period_start) as year_start,
        max(period_end) as year_end
    from {{ ref('fiscal_periods') }}
    where entity_id = 'chicago'
    group by entity_id, fiscal_year
)

select
    payments.payment_key,
    'chicago' as entity_id,
    'USD' as currency,
    payments.payment_date,
    coalesce(fiscal_calendar.fiscal_year, fiscal_years.fiscal_year) as fiscal_year,
    fiscal_calendar.fiscal_period,
    payments.amount,
    payments.vendor_name,
    payments.vendor_is_hashed,
    payments.voucher_number as document_number,
    nullif(payments.contract_number, 'DV') as contract_number,
    contracts.award_after_revisions,
    'chicago-payments' as source_table,
    payments.rolled_up_year is not null as is_rolled_up,
    payments.rolled_up_year,
    payments.department_name
from payments
left join {{ ref('fiscal_calendar') }} as fiscal_calendar
    on
        fiscal_calendar.entity_id = 'chicago'
        and payments.payment_date = fiscal_calendar.calendar_date
left join fiscal_years
    on
        payments.rolled_up_year <> 2002
        and fiscal_years.year_start = cast({{ year }} || '-01-01' as date)
        and fiscal_years.year_end = cast({{ year }} || '-12-31' as date)
left join {{ ref('int_chicago__contracts') }} as contracts
    on payments.contract_number = contracts.contract_number
