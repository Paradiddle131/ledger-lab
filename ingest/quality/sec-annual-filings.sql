-- The landed filings, every column as landed, then what the suite's rules read: the value as a
-- number, the filing day as a yyyymmdd number, null for a day no calendar has, and its month;
-- whether a report period or period end is a day no calendar has; whether the form says
-- amendment; an amended filing's accession number; a period whose revenue differs between two
-- filings, a restatement; a report's accession number when it has no revenue for the period it
-- reports; and, as one text each, every company with the fiscal years of the reports it has in
-- the set (`/A` for an amendment), and every length filed wrong: a report's accession number for
-- each period it reports whose own year's revenue has no four-quarter length, and the accession
-- number with the period end for each four-quarter figure an annual figure is also served as one
-- quarter long beside, so a report listed twice shows twice.
with companies as (
    select string_agg(cik || ' ' || years, ', ' order by cik) as company_reports
    from (
        select cik, string_agg(year, ' ' order by year) as years
        from (
            select distinct
                cik,
                left(report_period, 4) || case when is_amendment then '/A' else '' end as year
            from landed
        )
        group by cik
    )
),
wrong as (
    select coalesce(string_agg(filing, ', ' order by filing), 'none') as wrong_lengths
    from (
        select accession_number as filing
        from landed
        group by accession_number, report_period
        having count(*) filter (where period_end = report_period and quarters = '4') = 0
        union all
        select a.accession_number || ' ' || a.period_end
        from landed as a
        join landed as b using (accession_number, concept, unit, period_end, value)
        where a.quarters = '1' and b.quarters = '4'
    )
),
measured as (
    select
        *,
        count(distinct value) over (
            partition by cik, concept, unit, period_end, quarters
        ) as values_of_period,
        count(*) filter (where period_end = report_period) over (
            partition by accession_number, report_period
        ) as own_period_figures
    from landed
)
select
    measured.* exclude (values_of_period, own_period_figures),
    try_cast(value as decimal(20, 4)) as value_number,
    cast(strftime(try_strptime(filing_date, '%Y%m%d'), '%Y%m%d') as integer) as filed_on,
    try_cast(substr(filing_date, 5, 2) as integer) as filed_in_month,
    cast(
        try_strptime(report_period, '%Y%m%d') is null or try_strptime(period_end, '%Y%m%d') is null
        as integer
    ) as is_impossible_period_day,
    form like '%/A' as is_amended_form,
    case when is_amendment then accession_number end as amended_filing,
    case
        when values_of_period > 1
            then concat_ws(' ', cik, concept, unit, period_end, quarters)
    end as restated_period,
    case when own_period_figures = 0 then accession_number end as report_without_own_revenue,
    companies.company_reports,
    wrong.wrong_lengths
from measured
cross join companies
cross join wrong
