-- One row per contract: its award after revisions, the sum of the original award and every
-- revision's change, and what the reconciliation reads about how it got there.
with ends as (
    select
        contract_number,
        max(ends_on) as last_ends_on
    from {{ ref('stg_chicago__contracts') }}
    group by contract_number
)

select
    revisions.contract_number,
    cast(sum(revisions.award_amount) as {{ dbt.type_numeric() }}) as award_after_revisions,
    count(*) as revision_count,
    sum(case when revisions.award_amount < 0 then 1 else 0 end) as negative_revision_count,
    min(revisions.approved_on) as first_approved_on,
    max(revisions.approved_on) as last_approved_on,
    max(ends.last_ends_on) as last_ends_on
from {{ ref('int_chicago__contract_revisions') }} as revisions
left join ends on revisions.contract_number = ends.contract_number
group by revisions.contract_number
