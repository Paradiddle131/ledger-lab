-- Flagging repeats never flags every row of a payment: each group of rows that are the same
-- payment (identical but for the department) keeps a row unflagged, and a rolled-up year flagged
-- as repeating its dated payments has unflagged dated rows of its vendor, contract and year.
with same_payment as (
    select
        document_number, amount, payment_date, rolled_up_year, contract_number, vendor_name,
        sum(case when is_repeat then 0 else 1 end) as first_rows
    from {{ ref('int_chicago__payments') }}
    group by document_number, amount, payment_date, rolled_up_year, contract_number, vendor_name
    -- a rolled-up row repeating dated payments may take its group with it; checked below
    having sum(case when repeat_kind = 'rolled_up_itemised' then 1 else 0 end) = 0
),

rolled_up as (
    select rolled.payment_key
    from {{ ref('int_chicago__payments') }} as rolled
    where
        rolled.repeat_kind = 'rolled_up_itemised'
        and not exists (
            select 1
            from {{ ref('int_chicago__payments') }} as dated
            where
                not dated.is_repeat
                and extract(year from dated.payment_date) = rolled.rolled_up_year
                and dated.vendor_name = rolled.vendor_name
                and coalesce(dated.contract_number, 'DV') = coalesce(rolled.contract_number, 'DV')
        )
)

select 'same payment' as kind, vendor_name as payment from same_payment where first_rows = 0
union all
select 'rolled up', payment_key from rolled_up
