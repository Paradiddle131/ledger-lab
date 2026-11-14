-- One row per contract number of the City of Chicago, from the contracts table or from a payment
-- that carries a number the contracts table lacks: the award the payments are held to, what was
-- paid against it, the difference, and the one class that says why they differ.
-- Paid totals leave out every row flagged `is_repeat`, so each payment the repeat rule finds
-- served again counts once; direct vouchers (no contract number) are on no contract and stay out.
-- The award held to is the award after revisions, except where revisions that are not all 0 net
-- to 0 (an award withdrawn in full): there it is the highest award ever in force, since a net 0
-- there is no blanket but an award taken back, and payments over what was ever awarded are a break.
-- The City serves each revision's award to the dime, so a contract ties when its paid total is
-- within 5 cents of the award for each revision it has.
-- A payment is placed in time at the end of the period it covers: its day, or 31 December of a
-- rolled-up row's year (of 2002 for the 2002 row, which holds 1996 to 2002).
-- The class is the first that holds, in this order:
--   contract_not_found     payments carry a number the contracts table lacks (a break)
--   negative_award         the revisions take off more than was awarded (a break)
--   no_fixed_award         every revision awards 0: a blanket that depends upon requirements
--   unpaid                 no payment yet
--   paid_in_full           paid the award, to the dime of each revision
--   unspent                paid less than the award: an award is a ceiling, not a debt
--   then, for a contract paid over its award (each a break):
--   paid_under_two_names   without the rows that repeat another row on the contract but for the
--                          vendor name, neither with a department (which the repeat rule leaves
--                          unflagged), it is within its award
--   reduced_after_payment  the paid total is within the award in force at the end of the period
--                          of the last payment: a later revision cut it below what had been paid
--   paid_before_fixed_award  the award in force rose above 0 at some point, and without what was
--                          paid before it first did, the contract is within its award: paid as a
--                          blanket, later given a fixed award
--   unexplained            none of these
-- Two tags describe a break without explaining it: a term agreement, and an excess that all sits in
-- the 2002 row.
{%- set year_end = "cast(cast(rolled_up_year as " ~ dbt.type_string() ~ ") || '-12-31' as date)" %}

with payments as (
    select
        payment_key,
        contract_number,
        amount,
        rolled_up_year,
        coalesce(payment_date, {{ year_end }}) as paid_by,
        award_after_revisions is null as contract_missing,
        dense_rank() over (
            partition by contract_number, document_number, amount, payment_date, rolled_up_year
            order by vendor_name
        ) > 1 as under_another_name
    from {{ ref('int_chicago__payments') }}
    where contract_number is not null and not is_repeat
),

-- The award in force after each day a revision was approved.
award_days as (
    select
        contract_number,
        approved_on,
        sum(sum(award_amount)) over (
            partition by contract_number
            order by approved_on
            rows between unbounded preceding and current row
        ) as award_in_force
    from {{ ref('int_chicago__contract_revisions') }}
    group by contract_number, approved_on
),

awards as (
    select
        contract_number,
        max(award_in_force) as peak_award,
        min(case when award_in_force > 0 then approved_on end) as first_fixed_on
    from award_days
    group by contract_number
),

revisions as (
    select
        contract_number,
        sum(case when award_amount <> 0 then 1 else 0 end) as nonzero_revisions
    from {{ ref('int_chicago__contract_revisions') }}
    group by contract_number
),

paid as (
    select
        payments.contract_number,
        count(*) as payment_rows,
        sum(payments.amount) as paid,
        sum(case when payments.under_another_name then payments.amount else 0 end)
            as paid_again_under_another_name,
        sum(
            case
                when awards.first_fixed_on is null or payments.paid_by < awards.first_fixed_on
                    then payments.amount
                else 0
            end
        ) as paid_before_fixed_award,
        sum(case when payments.rolled_up_year = 2002 then payments.amount else 0 end)
            as paid_in_2002_row,
        max(payments.paid_by) as last_paid_by
    from payments
    left join awards on payments.contract_number = awards.contract_number
    where not payments.contract_missing
    group by payments.contract_number
),

-- The award in force at the end of the period of the last payment.
at_last_payment as (
    select
        paid.contract_number,
        award_days.award_in_force as award_at_last_payment,
        row_number() over (
            partition by paid.contract_number order by award_days.approved_on desc
        ) as latest
    from paid
    inner join award_days
        on
            paid.contract_number = award_days.contract_number
            and award_days.approved_on <= paid.last_paid_by
),

term_agreements as (
    select contract_number
    from {{ ref('stg_chicago__contracts') }}
    group by contract_number
    having
        max(case when contract_type = 'Term Agreement' then 1 else 0 end) = 1
        or (
            contract_number like 'T%'
            and length(contract_number) = 6
            and ltrim(substr(contract_number, 2), '0123456789') = ''
        )
),

repeats as (
    select contract_number, count(*) as repeat_rows, sum(amount) as paid_in_repeats
    from {{ ref('int_chicago__payments') }}
    where contract_number is not null and is_repeat
    group by contract_number
),

contracts as (
    select
        contracts.contract_number,
        true as in_contracts_table,
        contracts.award_after_revisions,
        case
            when contracts.award_after_revisions = 0 and revisions.nonzero_revisions > 0
                then awards.peak_award
            else contracts.award_after_revisions
        end as award_held_to,
        contracts.revision_count,
        contracts.first_approved_on,
        revisions.nonzero_revisions,
        awards.first_fixed_on,
        coalesce(paid.payment_rows, 0) as payment_rows,
        coalesce(paid.paid, 0) as paid,
        paid.paid_again_under_another_name,
        at_last_payment.award_at_last_payment,
        paid.paid_before_fixed_award,
        paid.paid_in_2002_row
    from {{ ref('int_chicago__contracts') }} as contracts
    inner join revisions on contracts.contract_number = revisions.contract_number
    inner join awards on contracts.contract_number = awards.contract_number
    left join paid on contracts.contract_number = paid.contract_number
    left join at_last_payment
        on contracts.contract_number = at_last_payment.contract_number and at_last_payment.latest = 1
),

not_found as (
    select
        contract_number,
        false as in_contracts_table,
        cast(null as {{ dbt.type_numeric() }}) as award_after_revisions,
        cast(null as {{ dbt.type_numeric() }}) as award_held_to,
        0 as revision_count,
        cast(null as date) as first_approved_on,
        0 as nonzero_revisions,
        cast(null as date) as first_fixed_on,
        count(*) as payment_rows,
        sum(amount) as paid,
        sum(case when under_another_name then amount else 0 end)
            as paid_again_under_another_name,
        cast(null as {{ dbt.type_numeric() }}) as award_at_last_payment,
        cast(null as {{ dbt.type_numeric() }}) as paid_before_fixed_award,
        sum(case when rolled_up_year = 2002 then amount else 0 end) as paid_in_2002_row
    from payments
    where contract_missing
    group by contract_number
),

numbers as (
    select * from contracts
    union all
    select * from not_found
),

measured as (
    select
        numbers.*,
        coalesce(repeats.repeat_rows, 0) as repeat_rows,
        coalesce(repeats.paid_in_repeats, 0) as paid_in_repeats,
        term_agreements.contract_number is not null as is_term_agreement,
        numbers.paid - numbers.award_held_to as difference,
        0.05 * numbers.revision_count as rounding_allowance
    from numbers
    left join repeats on numbers.contract_number = repeats.contract_number
    left join term_agreements on numbers.contract_number = term_agreements.contract_number
),

classed as (
    select
        *,
        case
            when not in_contracts_table then 'contract_not_found'
            when award_held_to < 0 then 'negative_award'
            when nonzero_revisions = 0 then 'no_fixed_award'
            when payment_rows = 0 then 'unpaid'
            when abs(difference) <= rounding_allowance then 'paid_in_full'
            when difference < 0 then 'unspent'
            when paid - paid_again_under_another_name <= award_held_to + rounding_allowance
                then 'paid_under_two_names'
            when paid <= award_at_last_payment + rounding_allowance then 'reduced_after_payment'
            when
                first_fixed_on is not null
                and paid - paid_before_fixed_award <= award_held_to + rounding_allowance
                then 'paid_before_fixed_award'
            else 'unexplained'
        end as break_class
    from measured
)

select
    contract_number,
    in_contracts_table,
    award_after_revisions,
    award_held_to,
    revision_count,
    first_approved_on,
    is_term_agreement,
    payment_rows,
    paid,
    repeat_rows,
    paid_in_repeats,
    difference,
    rounding_allowance,
    paid_again_under_another_name,
    award_at_last_payment,
    paid_before_fixed_award,
    paid_in_2002_row,
    break_class,
    break_class not in ('no_fixed_award', 'unpaid', 'paid_in_full', 'unspent') as is_break,
    coalesce(
        nonzero_revisions > 0
        and difference > rounding_allowance
        and paid - paid_in_2002_row <= award_held_to + rounding_allowance,
        false
    ) as excess_in_2002_row
from classed
