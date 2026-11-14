-- The landed euro reference rates, every column as landed, then what the suite's rules read: the
-- rate as a number, the day as a yyyymmdd number, how many rows its currency has, and the days
-- and the weekdays without a rate since the same currency's previous rate.
with typed as (
    select
        *,
        try_cast(rate as decimal(12, 6)) as rate_value,
        try_strptime(date, '%Y-%m-%d')::date as day
    from landed
),
spaced as (
    select
        *,
        lag(day) over (partition by currency order by day) as previous_day
    from typed
)
select
    * exclude (day, previous_day),
    cast(strftime(day, '%Y%m%d') as integer) as day_number,
    count(*) over (partition by currency) as rates_of_currency,
    date_diff('day', previous_day, day) as days_after_previous_rate,
    coalesce(
        len(list_filter(
            generate_series(previous_day + 1, day - 1, interval 1 day), lambda d: isodow(d) < 6
        )),
        0
    ) as weekdays_without_rate_before
from spaced
order by currency, day
