-- One row per contract: its award after revisions, the sum of the original award and every
-- revision's change, and what the reconciliation reads about how it got there.
select
    contract_number,
    cast(sum(award_amount) as {{ dbt.type_numeric() }}) as award_after_revisions,
    count(*) as revision_count,
    sum(case when award_amount < 0 then 1 else 0 end) as negative_revision_count,
    min(approved_on) as first_approved_on,
    max(approved_on) as last_approved_on
from {{ ref('int_chicago__contract_revisions') }}
group by contract_number
