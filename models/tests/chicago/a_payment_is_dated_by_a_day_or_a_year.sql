-- Every landed check date is a day or a bare year, and a rolled-up row (a bare year) is the only
-- kind without a voucher: a check date of any other shape would leave a payment with neither a
-- day nor a year.
select payment_key, payment_date, rolled_up_year, voucher_number
from {{ ref('stg_chicago__payments') }}
where
    (payment_date is null) = (rolled_up_year is null)
    or (rolled_up_year is null) <> (voucher_number is not null)
