-- Every contract paid over the award it is held to is a break, and every class that is not a
-- break holds only what it says: a tie within the rounding allowance, a contract paid less or
-- nothing, or an award of 0. A rule that let an over-award contract through as anything but a
-- break fails here.
select contract_number, break_class, award_held_to, paid, difference
from {{ ref('chicago_contract_reconciliation') }}
where
    (not is_break and break_class <> 'no_fixed_award' and difference > rounding_allowance)
    or (break_class = 'paid_in_full' and abs(difference) > rounding_allowance)
    or (break_class = 'unspent' and difference >= -rounding_allowance)
    or (break_class = 'unpaid' and payment_rows > 0)
    or (break_class = 'no_fixed_award' and award_held_to <> 0)
    or (not in_contracts_table and break_class <> 'contract_not_found')
