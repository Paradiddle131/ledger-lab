-- Chicago's and the Dutch government's fiscal year is the calendar year: period n is month n.
select
    entity_id,
    calendar_date
from {{ ref('fiscal_calendar') }}
where
    entity_id in ('chicago', 'nl-government')
    and (
        fiscal_year != extract(year from calendar_date)
        or fiscal_period != extract(month from calendar_date)
    )
