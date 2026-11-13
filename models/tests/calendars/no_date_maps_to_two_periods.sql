-- Each entity's day falls in exactly one fiscal period: overlapping periods put a day in two.
select
    entity_id,
    calendar_date,
    count(*) as periods
from {{ ref('fiscal_calendar') }}
group by entity_id, calendar_date
having count(*) > 1
