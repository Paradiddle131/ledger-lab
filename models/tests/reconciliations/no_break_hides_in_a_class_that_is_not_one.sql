-- Every contract paid over its award is a break, and every class that is not a break holds only
-- what it says: a tie within the rounding allowance, a contract paid less or nothing, or an award
-- of 0. A rule that let an over-award contract through as anything but a break fails here.
select contract_number, break_class, award_after_revisions, paid, difference
from {{ ref('chicago_contract_reconciliation') }}
where
    (not is_break and award_after_revisions <> 0 and difference > rounding_allowance)
    or (break_class = 'paid_in_full' and abs(difference) > rounding_allowance)
    or (break_class = 'unspent' and difference >= -rounding_allowance)
    or (break_class = 'unpaid' and payment_rows > 0)
    or (break_class = 'no_fixed_award' and award_after_revisions <> 0)
    or (not in_contracts_table and break_class <> 'contract_not_found')
