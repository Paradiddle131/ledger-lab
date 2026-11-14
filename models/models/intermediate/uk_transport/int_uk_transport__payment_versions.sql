-- Every landed row of every landed file, each with the month its file publishes and when GOV.UK
-- published the file. The Department revises a month by publishing a second file for it, so the
-- month is what a later file supersedes: a row is current when its file is the month's latest
-- published one. No row is dropped here; payments take only the current ones.
with staged as (
    select
        staged.*,
        cast({{ dbt.date_trunc('month', 'staged.payment_date') }} as date) as file_month,
        files.published_at
    from {{ ref('stg_uk_transport__spend') }} as staged
    left join {{ ref('uk_transport_spend_files') }} as files
        on staged.source_file = files.source_file
)

select
    source_file,
    source_row,
    file_month,
    published_at,
    coalesce(published_at = max(published_at) over (partition by file_month), false) as is_current,
    payment_date,
    amount,
    supplier,
    transaction_number
from staged
