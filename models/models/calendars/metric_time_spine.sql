-- One row per day the fiscal calendars span: the day spine MetricFlow reads metric time from.
select cast(date_day as date) as date_day
from ({{ fiscal_spine('day') }}) as days
