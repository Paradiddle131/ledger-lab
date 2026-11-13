-- A dollar amount translated to pounds is the amount taken to euros at the dollar's rate and on
-- to pounds at the pound's, both legs from the same day's set, rounded once: it lies within half
-- a penny of amount ÷ dollars per euro × pounds per euro. The check multiplies instead of
-- dividing (|pounds × dollars per euro − amount × pounds per euro| ≤ half a penny × dollars per
-- euro), so it is exact on every engine. The same holds from pounds to dollars, on every
-- publication date.
with amounts as (
    select 1.00 as amount
    union all
    select 100.00
    union all
    select 12345.67
    union all
    select -2500.10
),

postings as (
    select
        days.published_on as posted_on,
        directions.from_currency,
        directions.to_currency,
        amounts.amount
    from (select distinct published_on from {{ ref('fx_published_rates') }}) as days
    cross join amounts
    cross join (
        select 'USD' as from_currency, 'GBP' as to_currency
        union all
        select 'GBP', 'USD'
    ) as directions
),

translated as (
    {{ translate_currency(
        'postings', 'amount', 'from_currency', 'to_currency', rate_date='posted_on'
    ) }}
)

select
    translated.posted_on,
    translated.from_currency,
    translated.amount,
    translated.translated_amount
from translated
left join {{ ref('fx_published_rates') }} as from_leg
    on
        translated.posted_on = from_leg.published_on
        and translated.from_currency = from_leg.currency
left join {{ ref('fx_published_rates') }} as to_leg
    on
        translated.posted_on = to_leg.published_on
        and translated.to_currency = to_leg.currency
where
    translated.translated_amount is null
    or from_leg.units_per_euro is null
    or to_leg.units_per_euro is null
    or translated.rates_published_on != translated.posted_on
    or abs(
        translated.translated_amount * from_leg.units_per_euro
        - translated.amount * to_leg.units_per_euro
    ) > 0.005 * from_leg.units_per_euro
