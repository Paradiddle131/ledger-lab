-- The landed UK spend, every column as landed, then what the suite's rules read: the amount as a
-- number, the payment day as a yyyymmdd number and its month; how many months each file's rows
-- fall in; whether the supplier is hidden (hashed or redacted), and whether a hidden row kept its description or postcode, or a reference in clear that is no
-- placeholder; whether a hashed reference is one no hidden row carries, or sits on a clear row; the
-- hashed supplier; whether the transaction number is a placeholder (`#`, `DIRECT DEBIT`); and
-- whether the row is a further copy of a row of its file that is identical in every landed cell
-- but its position.
with typed as (
    select
        *,
        try_strptime(date, '%d/%m/%Y') as paid,
        supplier like 'person:%' or supplier = 'REDACTED' as hidden,
        transaction_number like 'id:%' as keyed,
        transaction_number in ('#', 'DIRECT DEBIT') as placeholder
    from landed
)
select
    * exclude (paid, hidden, keyed, placeholder),
    try_cast(amount as decimal(18, 2)) as amount_value,
    cast(strftime(paid, '%Y%m%d') as integer) as paid_on,
    strftime(paid, '%Y-%m') as paid_in_month,
    count(distinct strftime(paid, '%Y-%m')) over (partition by source_file) as months_of_file,
    hidden as is_hidden_supplier,
    cast(
        hidden and (item_text is not null or postal_code is not null) as integer
    ) as is_hidden_row_describing_supplier,
    cast(hidden and not keyed and not placeholder as integer) as is_hidden_row_with_clear_reference,
    cast(
        keyed and not bool_or(hidden) over (partition by transaction_number) as integer
    ) as is_hashed_reference_of_no_hidden_row,
    cast(keyed and not hidden as integer) as is_hashed_reference_on_clear_row,
    case when supplier like 'person:%' then supplier end as hashed_supplier,
    cast(supplier = 'REDACTED' as integer) as is_redacted,
    cast(placeholder as integer) as is_placeholder_reference,
    cast(
        row_number() over (
            partition by source_file, department_family, entity, date, expense_type, expense_area,
            supplier, transaction_number, item_text, amount, postal_code
        ) > 1 as integer
    ) as is_identical_copy
from typed
