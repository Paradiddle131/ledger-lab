{% docs group_totals_basis %}
What the figure is, so figures of unlike things are never read as one:
`direct-vouchers`, the City of Chicago's dated payments made on no contract (a direct voucher);
`contract-payments`, the City's dated payments made against a contract number;
`payments-year-rollup`, the City's payments it serves only as a yearly total per vendor and contract, with no day, every one against a contract.
The City's dated rows change in kind between years: in 2023 and 2024 they are almost all direct vouchers (57,188 of 57,250 and 40,785 of 40,844 rows on the pinned files), because the City serves those years' contract payments as roll-ups, while from 2025 most dated rows are contract payments (66,978 of 112,391 in 2025, 53,631 of 90,698 in 2026), so a month's `contract-payments` row is small before 2025 and the roll-up carries the rest.
`payments-over-25k`, the UK Department for Transport's payments over £25,000 from its monthly spending files, the only UK spend the lab lands, for fiscal 2025 (April 2025 to March 2026).
`cash-outturn`, the Dutch central government's cash expenditure outturn (kind `U`, `Realisatie`) for a year, summed over every article of the budget statements as served, gross, never netted out; it is a year's spend of the whole State, not payments.
Gross means, on the pinned file: the contributions chapters pay into the budget funds are counted in the paying chapter and again where the fund spends them (chapter XII article 26, the contribution to the investment funds, 9.36, 10.41 and 10.52 billion euros in 2023 to 2025; chapter X article 13, the contribution to the defence materiel fund, 5.47 billion in 2023 and served as 0 in 2024 and 2025, although that fund, chapter K, spent 7.61 and 10.17 billion, which the file does not explain); the State's debt financing, redemptions included (chapter IX article 11: 52.90, 44.36 and 26.51 billion); and its cash management (chapter IX article 12: 4.87, 5.93 and 5.22 billion).
{% enddocs %}

{% docs group_totals_actual_rates %}
The figures' sum in the reporting currency at actual rates, each figure translated and rounded to the cent before the sum: a payment with a day at that day's ECB rates (the previous business day's on a closed day, as `translate_currency` gives them); a figure with a year and no day (a Chicago roll-up, a Dutch outturn line) at the mean of the rates the ECB published on the year's days for each currency, rounded to six decimal places, which equals the yearly average the ECB publishes (unrounded) to six places.
{% enddocs %}

{% docs group_totals_left_out_by %}
The rule that leaves the figure out of the group totals, the first that applies; null when it counts:
`repeat`, a payment the source serves again (`is_repeat`);
`outside-calendars`, a figure of a year the fiscal calendars do not cover (Chicago's payments of 2022 and earlier and its rolled-up row of 1996 to 2002);
`no-outturn`, a Dutch cash line of a year the file serves no outturn for (2026), since a budget never stands in for spend;
`no-rate`, a figure of an entity's span the ECB rates do not cover: their first publication in it more than four days after its start or their last more than four days before its end, or one of the entity's dated payments in it before the first publication or after the last, so a span is left out whole, never in part.
{% enddocs %}
