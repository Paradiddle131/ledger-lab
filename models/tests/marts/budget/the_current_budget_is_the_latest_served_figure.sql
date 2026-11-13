-- Fails on every line whose current budget is not the figure of the latest budget moment the file
-- serves for it, by label or by value, so the outturn or an earlier moment never stands in for it;
-- the moments in the order a year passes them.
select
    budget_line_key,
    current_budget_moment,
    current_budget_eur
from {{ ref('budget_against_outturn') }}
where
    current_budget_eur is null
    or (second_supplementary_eur is not null and current_budget_moment <> 'StandO2')
    or (
        second_supplementary_eur is null
        and september_supplementary_eur is not null
        and current_budget_moment <> 'StandSBS'
    )
    or (
        second_supplementary_eur is null
        and september_supplementary_eur is null
        and first_supplementary_eur is not null
        and current_budget_moment <> 'StandO1'
    )
    or current_budget_eur <> case current_budget_moment
        when 'StandO2' then second_supplementary_eur
        when 'StandSBS' then september_supplementary_eur
        when 'StandO1' then first_supplementary_eur
        when 'StandOWB' then draft_budget_eur
    end
    or current_budget_moment not in ('StandOWB', 'StandO1', 'StandSBS', 'StandO2')
