-- The landed payments, every column as landed, then what the suite's rules read: the amount as a
-- number; whether the row is itemised (dated day, month and year) or rolled up (dated by a bare
-- year), its check day as a yyyymmdd number and its year, and whether it is dated in the
-- itemised form with a day no calendar has; the years rolled up, as one text; whether the row is
-- the 2002 row, which holds 1996 to 2002; whether the voucher is missing, the payment has no
-- contract (`DV`), the amount is negative or zero, or the row is a further copy of an identical
-- row; and whether the payee is hashed, and the hashed payee.
select
    *,
    try_cast(amount as decimal(18, 2)) as amount_value,
    regexp_full_match(check_date, '\d{2}/\d{2}/\d{4}') as is_itemised,
    regexp_full_match(check_date, '\d{4}') as is_rolled_up,
    cast(strftime(try_strptime(check_date, '%m/%d/%Y'), '%Y%m%d') as integer) as paid_on,
    try_cast(right(check_date, 4) as integer) as year_number,
    cast(
        regexp_full_match(check_date, '\d{2}/\d{2}/\d{4}')
        and try_strptime(check_date, '%m/%d/%Y') is null as integer
    ) as is_itemised_without_day,
    (
        select coalesce(string_agg(year, ', ' order by year), 'none')
        from (select distinct check_date as year from landed where regexp_full_match(check_date, '\d{4}'))
    ) as rolled_up_years,
    cast(check_date = '2002' as integer) as is_2002_row,
    voucher_number is null as is_without_voucher,
    cast(contract_number = 'DV' as integer) as is_without_contract,
    cast(voucher_number = 'Multiple' as integer) as is_multiple_voucher,
    cast(try_cast(amount as decimal(18, 2)) < 0 as integer) as is_negative_amount,
    cast(try_cast(amount as decimal(18, 2)) = 0 as integer) as is_zero_amount,
    cast(
        row_number() over (
            partition by voucher_number, amount, check_date, department_name, contract_number,
            vendor_name
        ) > 1 as integer
    ) as is_identical_copy,
    vendor_name like 'person:%' as is_hashed_payee,
    case when vendor_name like 'person:%' then vendor_name end as hashed_payee
from landed
