-- One row per payment of the City, in the payments mart's column shape and order, then the City's
-- own detail. A payment joins its contract by the served contract number, against one row per
-- contract, so no payment is ever repeated; `DV` (a direct voucher, paid on no contract) becomes
-- no contract number, and a number the contracts table lacks stays, with no award.
-- A dated payment takes the fiscal year and period of its day; a rolled-up row takes the fiscal
-- year that is exactly its calendar year (the City's year runs January to December) and no
-- period, and the 2002 row, which holds 1996 to 2002, takes neither. A year the calendars do not
-- cover has neither.
-- Every served row stays, and a row that repeats another is flagged, in this order: a copy after
-- the first of rows identical in every column (`exact`); a row without a department beside one
-- that has a department and is otherwise the same (`without_department`); a row without a
-- department beside one that has a department and is the same but for the vendor name, the City
-- serving one payment under two names of its vendor (`without_department_other_name`); a
-- rolled-up row whose amount equals to the cent the unflagged dated payments of its vendor,
-- contract and year (`rolled_up_itemised`). The first row of each group is the one left unflagged.
with payments as (
    select
        *,
        count(department_name) over (
            partition by
                voucher_number, amount, payment_date, rolled_up_year, contract_number, vendor_name
        ) as rows_with_department,
        count(department_name) over (
            partition by voucher_number, amount, payment_date, rolled_up_year, contract_number
        ) as rows_with_department_any_name
    from {{ ref('stg_chicago__payments') }}
),

same_payment as (
    select
        *,
        case
            when copy_number > 1 then 'exact'
            when department_name is null and rows_with_department > 0 then 'without_department'
            when department_name is null and rows_with_department_any_name > 0
                then 'without_department_other_name'
        end as same_payment_kind
    from payments
),

dated_years as (
    select
        vendor_name,
        contract_number,
        extract(year from payment_date) as paid_in_year,
        sum(amount) as paid
    from same_payment
    where payment_date is not null and same_payment_kind is null
    group by vendor_name, contract_number, extract(year from payment_date)
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
),

flagged as (
    select
        payments.*,
        coalesce(
            payments.same_payment_kind,
            case when dated_years.paid = payments.amount then 'rolled_up_itemised' end
        ) as repeat_kind
    from same_payment as payments
    left join dated_years
        on
            payments.rolled_up_year = dated_years.paid_in_year
            and payments.vendor_name = dated_years.vendor_name
            and payments.contract_number = dated_years.contract_number
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
    payments.repeat_kind is not null as is_repeat,
    payments.repeat_kind,
    payments.rolled_up_year is not null as is_rolled_up,
    payments.rolled_up_year,
    payments.department_name
from flagged as payments
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
