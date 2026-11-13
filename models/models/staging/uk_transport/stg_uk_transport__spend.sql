-- One row per landed row of the Department for Transport's spend: the file and position it was
-- served at, its payment date, its amount typed as a decimal from the publisher's digits, and the
-- supplier and transaction number as landed (a possible person's both keyed hashes).
-- The date is rebuilt from its served `dd/mm/yyyy` as an ISO date, and both casts are safe ones,
-- so a value that does not cast is a null the not-null tests name, never a dropped row.
select
    source_file,
    source_row,
    {{ dbt.safe_cast(
        "substr(" ~ adapter.quote('date') ~ ", 7, 4) || '-' || substr(" ~ adapter.quote('date')
        ~ ", 4, 2) || '-' || substr(" ~ adapter.quote('date') ~ ", 1, 2)",
        "date",
    ) }} as payment_date,
    {{ dbt.safe_cast('amount', dbt.type_numeric()) }} as amount,
    supplier,
    transaction_number
from {{ source('landing', 'uk-transport-spend-2025') }}
