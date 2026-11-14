-- One row per contract of the City of Chicago: its award after revisions beside what was paid
-- against it, each payment counted once (repeats left out), and, for a contract with a fixed award
-- (above 0), the part of the award not yet paid and the part paid over it, neither below 0.
with paid as (
    select
        contract_number,
        sum(amount) as paid,
        count(*) as payments
    from {{ ref('int_chicago__payments') }}
    where not is_repeat and contract_number is not null
    group by contract_number
),

ends as (
    select
        contract_number,
        max(ends_on) as ends_on
    from {{ ref('stg_chicago__contracts') }}
    group by contract_number
),

spend as (
    select
        contracts.contract_number,
        contracts.award_after_revisions,
        coalesce(paid.paid, 0) as paid,
        coalesce(paid.payments, 0) as payments,
        contracts.first_approved_on,
        ends.ends_on
    from {{ ref('int_chicago__contracts') }} as contracts
    left join paid on contracts.contract_number = paid.contract_number
    left join ends on contracts.contract_number = ends.contract_number
)

select
    contract_number,
    case
        when award_after_revisions > 0 then 'fixed'
        when award_after_revisions = 0 then 'none'
        else 'negative'
    end as award_kind,
    award_after_revisions,
    paid,
    payments,
    case when award_after_revisions > 0 then award_after_revisions end as fixed_award,
    case when award_after_revisions > 0 then paid end as paid_against_fixed_award,
    case
        when award_after_revisions > paid and award_after_revisions > 0
            then award_after_revisions - paid
        when award_after_revisions > 0 then 0
    end as unpaid_award,
    case
        when paid > award_after_revisions and award_after_revisions > 0
            then paid - award_after_revisions
        when award_after_revisions > 0 then 0
    end as paid_over_award,
    first_approved_on,
    ends_on
from spend
