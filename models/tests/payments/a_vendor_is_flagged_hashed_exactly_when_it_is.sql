-- `vendor_is_hashed` is true exactly on the rows whose vendor is a keyed hash, in every entity, so
-- a rule that masks or matches on it never misses a hashed vendor or hides a clear one.
select payment_key, entity_id, vendor_is_hashed
from {{ ref('payments') }}
where vendor_is_hashed <> (vendor_name like 'person:%')
