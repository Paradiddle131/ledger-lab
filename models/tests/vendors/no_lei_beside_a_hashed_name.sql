-- A hashed vendor never carries a legal entity: the register is public, so an LEI beside the hash
-- would name the person again.
select entity_id, match_status
from {{ ref('vendor_entity') }}
where
    vendor_name like 'person:%'
    and (lei is not null or match_rule is not null or match_status <> 'hashed')
