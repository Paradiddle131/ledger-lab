-- A transaction number carried only by a hashed supplier's rows would find them again in the
-- public file, so the landing hashes it on every hashed row; payments must never show one in clear.
select payment_key
from {{ ref('int_uk_transport__payments') }}
where vendor_is_hashed and document_number not like 'id:%'
