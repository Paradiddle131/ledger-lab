-- The UK department's fiscal year opens on 1 April and closes on 31 March, labelled by the
-- calendar year it opens in: 1 April 2024 opens 2024 in period 1, 31 March 2025 closes it in
-- period 12, and no other day opens or closes a year. A calendar without UK days fails too.
with uk as (
    select
        calendar_date,
        fiscal_year,
        fiscal_period,
        extract(year from calendar_date) as calendar_year,
        extract(month from calendar_date) as calendar_month,
        extract(day from calendar_date) as calendar_day,
        min(calendar_date) over (partition by fiscal_year) as year_opens,
        max(calendar_date) over (partition by fiscal_year) as year_closes
    from {{ ref('fiscal_calendar') }}
    where entity_id = 'uk-transport'
),

wrong as (
    select calendar_date
    from uk
    where
        (
            calendar_month = 4 and calendar_day = 1
            and not (
                fiscal_year = calendar_year and fiscal_period = 1 and calendar_date = year_opens
            )
        )
        or (
            calendar_month = 3 and calendar_day = 31
            and not (
                fiscal_year = calendar_year - 1
                and fiscal_period = 12
                and calendar_date = year_closes
            )
        )
        or (calendar_date = year_opens and not (calendar_month = 4 and calendar_day = 1))
        or (calendar_date = year_closes and not (calendar_month = 3 and calendar_day = 31))
)

select calendar_date from wrong
union all
select cast(null as date) as calendar_date
from (select count(*) as days from uk) as counted
where counted.days = 0
