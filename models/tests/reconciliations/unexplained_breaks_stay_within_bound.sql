-- The reconciliation passes while its unexplained breaks stay within bound: at most 2 in 1,000
-- contract numbers, and an excess over their awards of at most 2 in 1,000 of the dollars paid on
-- contracts. The pinned files measure 1.1 and 1.4 in 1,000 (100 contracts, $133.3M over), so a
-- new kind of break the classes do not explain soon fails it.
with totals as (
    select
        count(*) as contract_numbers,
        sum(case when break_class = 'unexplained' then 1 else 0 end) as unexplained,
        sum(paid) as paid,
        sum(case when break_class = 'unexplained' then difference else 0 end) as unexplained_excess
    from {{ ref('chicago_contract_reconciliation') }}
)

select *
from totals
where unexplained * 1000 > 2 * contract_numbers or unexplained_excess * 1000 > 2 * paid
