-- A matched name has its one legal entity, its rule and a score; an ambiguous one its rule and
-- several candidates alone; an unmatched or hashed one none of them.
select entity_id, vendor_name, match_status
from {{ ref('vendor_entity') }}
where not (
    match_status = 'matched'
    and lei is not null and match_rule is not null and match_score is not null
    and candidate_count = 1
    or match_status = 'ambiguous'
    and lei is null and match_rule is not null and match_score is null
    and candidate_count > 1
    or match_status in ('unmatched', 'hashed')
    and lei is null and match_rule is null and match_score is null
    and candidate_count = 0
)
