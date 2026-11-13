-- One row per contract and revision: the original award (revision `0`) or a change to it, each
-- with the amount it adds or, when negative, takes off. The publisher numbers revisions in its
-- current system (`1`, `2`, ...) and lists modifications and time extensions imported from its
-- legacy one under their own document numbers (`T1`, `P90853`, ...), which are revisions too. A
-- revision served on several rows (under two spellings of the vendor, or a hashed and a clear
-- one) counts once; its rows agree on the amount and the approval day, and the unique test on this
-- model fails the build if they ever do not.
select
    contract_number,
    revision_number,
    award_amount,
    approved_on,
    count(*) as served_rows
from {{ ref('stg_chicago__contracts') }}
group by contract_number, revision_number, award_amount, approved_on
