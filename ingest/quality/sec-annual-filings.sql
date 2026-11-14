-- The landed filings, every column as landed, then what the suite's rules read: the value as a
-- number, the filing day as a yyyymmdd number and its month; whether the form says amendment; an
-- amended filing's accession number; each company with the fiscal years of the reports it has in
-- the set (`/A` for an amendment); a period whose revenue differs between two filings, a
-- restatement; a report's accession number when it has no revenue for the period it reports;
-- and the two ways a length is filed wrong: a report's accession number when its own year's
-- revenue has no four-quarter length, and with the period end when an annual figure is also
-- served as one quarter long.
with years as (
    select
        cik,
        string_agg(year, ' ' order by year) as years
    from (
        select distinct
            cik,
            left(report_period, 4) || case when is_amendment then '/A' else '' end as year
        from landed
    )
    group by cik
),
measured as (
    select
        *,
        count(distinct value) over (
            partition by cik, concept, unit, period_end, quarters
        ) as values_of_period,
        count(*) filter (where period_end = report_period) over (
            partition by accession_number, report_period
        ) as own_period_figures,
        count(*) filter (where period_end = report_period and quarters = '4') over (
            partition by accession_number, report_period
        ) as own_year_figures,
        count(*) filter (where quarters = '4') over (
            partition by accession_number, concept, unit, period_end, value
        ) as same_figure_four_quarters_long
    from landed
)
select
    measured.* exclude (
        values_of_period, own_period_figures, own_year_figures, same_figure_four_quarters_long
    ),
    try_cast(value as decimal(20, 4)) as value_number,
    try_cast(filing_date as integer) as filed_on,
    try_cast(substr(filing_date, 5, 2) as integer) as filed_in_month,
    form like '%/A' as is_amended_form,
    case when is_amendment then accession_number end as amended_filing,
    measured.cik || ' ' || years.years as company_reports,
    case
        when values_of_period > 1
            then concat_ws(' ', measured.cik, concept, unit, period_end, quarters)
    end as restated_period,
    case when own_period_figures = 0 then accession_number end as report_without_own_revenue,
    case when own_year_figures = 0 then accession_number end as report_without_annual_length,
    case
        when quarters = '1' and same_figure_four_quarters_long > 0
            then accession_number || ' ' || period_end
    end as annual_figure_as_one_quarter
from measured
join years using (cik)
