-- Fails on every entity whose figures the group totals and the rules leaving figures out do not
-- account for exactly once: the rows and the own-currency amount of the payments mart and of the
-- Dutch cash-expenditure lines must equal those the group totals count plus those a rule leaves
-- out, so a figure dropped or counted twice on the way, by a join or a rule, fails here.
with served as (
    select entity_id, count(*) as figures, sum(amount) as amount
    from {{ ref('payments') }}
    group by entity_id

    union all

    select entity_id, count(*), sum(outturn_eur)
    from {{ ref('budget_against_outturn') }}
    where kind = 'U'
    group by entity_id
),

accounted as (
    select entity_id, sum(figures) as figures, sum(amount) as amount
    from (
        select entity_id, figures, amount from {{ ref('group_totals') }}
        union all
        select entity_id, 1, amount
        from {{ ref('group_totals_figures') }}
        where left_out_by is not null
    ) as both_sides
    group by entity_id
)

select
    coalesce(served.entity_id, accounted.entity_id) as entity_id,
    served.figures as figures_served,
    accounted.figures as figures_accounted,
    served.amount as amount_served,
    accounted.amount as amount_accounted
from served
full outer join accounted on served.entity_id = accounted.entity_id
where
    served.figures is null
    or accounted.figures is null
    or served.figures <> accounted.figures
    or coalesce(served.amount, 0) <> coalesce(accounted.amount, 0)
