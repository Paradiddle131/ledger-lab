-- A transaction number a hashed supplier's row carries would find them again in the public file,
-- so the landing hashes it on every row of the paying body that carries it; a hashed supplier's row shows only that
-- hash or one of the Department's placeholders for no number (`PLACEHOLDERS` in the landing step).
select payment_key
from {{ ref('int_uk_transport__payments') }}
where
    vendor_is_hashed
    and document_number not like 'id:%'
    and document_number not in ('#', 'DIRECT DEBIT')
