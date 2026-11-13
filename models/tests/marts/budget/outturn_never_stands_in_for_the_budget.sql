-- Fails on every line where the outturn takes a budget figure's place: a current budget that is
-- missing or comes from no budget moment, or a year whose outturn is published but whose current
-- budget is not the second supplementary one, the last budget before the outturn.
select
    budget_line_key,
    current_budget_moment,
    outturn_eur
from {{ ref('budget_against_outturn') }}
where
    current_budget_eur is null
    or current_budget_moment is null
    or current_budget_moment not in ('StandOWB', 'StandO1', 'StandSBS', 'StandO2')
    or (outturn_eur is not null and current_budget_moment <> 'StandO2')
