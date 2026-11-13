{#- A select of every row of `relation` (a ref or a CTE name) with two columns added:
    `translated_amount`, the amount in `to_currency`, and `rates_published_on`, the ECB
    publication date of the rate set used. `amount`, `from_currency` and `to_currency` are SQL
    expressions over the relation's columns or quoted literals ("'GBP'").

    The rate set is that of one day in `fx_rates`: the row's `rate_date` (its posting or payment
    date), or with `constant_rates_on` (an ISO date, such as '2024-03-27') the same day for every
    row, so the same amount translates to the same result whatever its own date (constant
    currency). On a closed day the set is the previous business day's. The translation runs
    through the euro: the amount divided by the from-currency's units per euro and multiplied by
    the to-currency's, rounded once, half away from zero, to two decimal places.

    A row with an amount that the rates cannot translate (an unknown or missing currency, a day
    before the first publication date or after the last) stops the query with an error naming
    the currencies and the day, never a far-away rate or a null a sum would skip; a null amount
    translates to null. Both engines spell the raise `error(message)` and evaluate it only on the
    row whose case reaches it. -#}
{% macro translate_currency(
    relation, amount, from_currency, to_currency, rate_date=none, constant_rates_on=none
) %}
    {%- if (rate_date is none) == (constant_rates_on is none) -%}
        {{ exceptions.raise_compiler_error(
            "translate_currency takes either rate_date or constant_rates_on"
        ) }}
    {%- endif -%}
    {%- if constant_rates_on is none -%}
        {%- set on = "cast((" ~ rate_date ~ ") as date)" -%}
    {%- elif modules.re.fullmatch("[0-9]{4}-[0-9]{2}-[0-9]{2}", constant_rates_on | string) -%}
        {%- set on = "cast('" ~ constant_rates_on ~ "' as date)" -%}
    {%- else -%}
        {{ exceptions.raise_compiler_error(
            "translate_currency's constant_rates_on is an ISO date (YYYY-MM-DD), not "
            ~ constant_rates_on
        ) }}
    {%- endif -%}
    {%- set leg -%}
        select
            rate_on as fx_rate_on,
            currency as fx_currency,
            units_per_euro as fx_units_per_euro,
            published_on as fx_published_on
        from {{ ref('fx_rates') }}
    {%- endset -%}
    {%- set missing -%}
        {{ dbt.concat([
            "'translate_currency: no ECB rate from '",
            "coalesce(cast((" ~ from_currency ~ ") as " ~ dbt.type_string() ~ "), 'no currency')",
            "' to '",
            "coalesce(cast((" ~ to_currency ~ ") as " ~ dbt.type_string() ~ "), 'no currency')",
            "' on '",
            "coalesce(cast(" ~ on ~ " as " ~ dbt.type_string() ~ "), 'no date')",
        ]) }}
    {%- endset -%}
    select
        untranslated.*,
        case
            when ({{ amount }}) is null then null
            when from_leg.fx_units_per_euro is null or to_leg.fx_units_per_euro is null
                then error({{ missing }})
            else cast({{ convert_through_euro(
                "cast((" ~ amount ~ ") as " ~ dbt.type_numeric() ~ ")",
                "from_leg.fx_units_per_euro",
                "to_leg.fx_units_per_euro",
            ) }} as {{ dbt.type_numeric() }})
        end as translated_amount,
        case
            when from_leg.fx_published_on is not null then to_leg.fx_published_on
        end as rates_published_on
    from {{ relation }} as untranslated
    left join ({{ leg }}) as from_leg
        on
            from_leg.fx_rate_on = {{ on }}
            and from_leg.fx_currency = ({{ from_currency }})
    left join ({{ leg }}) as to_leg
        on
            to_leg.fx_rate_on = {{ on }}
            and to_leg.fx_currency = ({{ to_currency }})
{% endmacro %}

{#- `amount` times `to_rate` divided by `from_rate`, rounded half away from zero to two decimal
    places: DuckDB divides decimals as floats, which misround a half cent, so there the quotient
    is taken exactly in 128-bit integers of millionths (the scale of the amount and both rates);
    BigQuery divides in BIGNUMERIC (38 decimal places), so the quotient is rounded to cents from
    38 places, not from NUMERIC's 9, which could round twice near a half cent. -#}
{% macro convert_through_euro(amount, from_rate, to_rate) %}
    {{ return(adapter.dispatch('convert_through_euro')(amount, from_rate, to_rate)) }}
{% endmacro %}

{% macro default__convert_through_euro(amount, from_rate, to_rate) %}
    round({{ amount }} * {{ to_rate }} / {{ from_rate }}, 2)
{% endmacro %}

{% macro bigquery__convert_through_euro(amount, from_rate, to_rate) %}
    round(cast({{ amount }} as bignumeric) * {{ to_rate }} / {{ from_rate }}, 2)
{% endmacro %}

{% macro duckdb__convert_through_euro(amount, from_rate, to_rate) %}
    {#- amount × to ÷ from in cents is A·T ÷ (F·10^4) for A, T, F the three in millionths. -#}
    {%- set numerator -%}
        (cast({{ amount }} * 1000000 as hugeint) * cast({{ to_rate }} * 1000000 as hugeint))
    {%- endset -%}
    {%- set denominator -%}
        (cast({{ from_rate }} * 1000000 as hugeint) * 10000)
    {%- endset -%}
    cast(
        sign({{ numerator }})
        * ((2 * abs({{ numerator }}) + {{ denominator }}) // (2 * {{ denominator }}))
        as decimal(38, 0)
    ) * 0.01
{% endmacro %}
