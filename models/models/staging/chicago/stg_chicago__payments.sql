-- One row per landed row of the City's payments, typed, with a key that names it. A check date is
-- a day (`MM/DD/YYYY`) on an itemised payment, or a bare year on a row the publisher rolled up (one
-- row per vendor, contract and year, with no voucher number; `2002` holds 1996 to 2002): a
-- rolled-up row keeps its year and gets no day, never 1 January. The amount is a decimal with its
-- sign as served. The key hashes every landed column with the entity, and numbers rows that are
-- identical in every column (the publisher serves some payments twice), so it is unique and the
-- same in every landing made with the same hash key.
-- The `source_rows` var picks the rows: the landed file or the committed sample of real rows.
with served as (
    select
        voucher_number,
        amount,
        check_date,
        department_name,
        contract_number,
        vendor_name,
        {{ dbt.hash(dbt.concat([
            "'chicago'",
            "'|'", "coalesce(voucher_number, '')",
            "'|'", "coalesce(amount, '')",
            "'|'", "coalesce(check_date, '')",
            "'|'", "coalesce(department_name, '')",
            "'|'", "coalesce(contract_number, '')",
            "'|'", "coalesce(vendor_name, '')",
        ])) }} as row_hash
    from {{ landed_or_sample('landing', 'chicago-payments', 'chicago_payments_sample') }}
),

numbered as (
    select
        *,
        row_number() over (partition by row_hash) as copy_number
    from served
)

select
    {{ dbt.hash(dbt.concat([
        "row_hash", "'|'", "cast(copy_number as " ~ dbt.type_string() ~ ")",
    ])) }} as payment_key,
    voucher_number,
    cast(amount as {{ dbt.type_numeric() }}) as amount,
    case
        when length(check_date) = 10
            then cast(
                substr(check_date, 7, 4) || '-' || substr(check_date, 1, 2) || '-'
                || substr(check_date, 4, 2) as date
            )
    end as payment_date,
    case when length(check_date) = 4 then cast(check_date as {{ dbt.type_int() }}) end
        as rolled_up_year,
    department_name,
    contract_number,
    vendor_name,
    vendor_name like 'person:%' as vendor_is_hashed,
    copy_number
from numbered
