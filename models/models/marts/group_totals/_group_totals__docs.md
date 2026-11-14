{% docs group_totals_basis %}
What the figure is, so figures of unlike things are never read as one:
`payments`, the City of Chicago's payments with a day, every one the City serves;
`payments-year-rollup`, the City's payments it serves only as a yearly total per vendor and contract, with no day (for 2023 and 2024 they hold more than the dated payments);
`payments-over-25k`, the UK Department for Transport's payments over £25,000 from its monthly spending files, the only UK spend the lab lands, for fiscal 2025 (April 2025 to March 2026);
`cash-outturn`, the Dutch central government's cash expenditure outturn (kind `U`, `Realisatie`) for a year, summed over every article of the budget statements as served: every chapter and budget fund, the State's debt financing and cash management among them, and the contributions chapters pay into the budget funds counted in both, never netted out; it is a year's spend of the whole State, not payments.
{% enddocs %}

{% docs group_totals_actual_rates %}
The figures' sum in the reporting currency at actual rates, each figure translated and rounded to the cent before the sum: a payment with a day at that day's ECB rates (the previous business day's on a closed day, as `translate_currency` gives them); a figure with a year and no day (a Chicago roll-up, a Dutch outturn line) at the mean of the rates the ECB published on the year's days for each currency, rounded to six decimal places, which is how the ECB averages its own rates over a year.
{% enddocs %}

{% docs group_totals_left_out_by %}
The rule that leaves the figure out of the group totals, the first that applies; null when it counts:
`repeat`, a payment the source serves again (`is_repeat`);
`outside-calendars`, a figure of a year the fiscal calendars do not cover (Chicago's payments of 2022 and earlier and its rolled-up row of 1996 to 2002);
`no-outturn`, a Dutch cash line of a year the file serves no outturn for (2026), since a budget never stands in for spend;
`no-rate`, a figure whose span the ECB rates do not cover, their first publication in it more than four days after its start or their last more than four days before its end, so a span is left out whole, never in part.
{% enddocs %}
