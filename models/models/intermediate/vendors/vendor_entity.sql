-- One row per entity and landed vendor name: the legal entity it matches, by which rule, with what
-- score, or why none. A hashed name (`person:`) is never matched: it has no name to match, and an
-- LEI beside it would name the person again. The first rule that finds a candidate decides; the
-- name is matched only when that rule's candidates are one legal entity, and ambiguous otherwise.
with vendors as (
    select 'chicago' as entity_id, 'US' as home_country, vendor_name, count(*) as landed_rows
    from {{ source('landing', 'chicago-contracts') }}
    where vendor_name is not null
    group by vendor_name
    union all
    select 'chicago', 'US', vendor_name, count(*)
    from {{ source('landing', 'chicago-payments') }}
    where vendor_name is not null
    group by vendor_name
    union all
    select 'uk-transport', 'GB', supplier, count(*)
    from {{ source('landing', 'uk-transport-spend-2025') }}
    where supplier is not null
    group by supplier
),

names as (
    select
        entity_id,
        home_country,
        vendor_name,
        sum(landed_rows) as landed_rows,
        vendor_name like 'person:%' as hashed,
        {{ vendor_match_key('vendor_name') }} as match_key
    from vendors
    group by entity_id, home_country, vendor_name
),

keyed as (
    select *, {{ vendor_base_key('match_key') }} as base_key from names where not hashed
),

vendor_keys as (
    select *, {{ vendor_legal_form('match_key') }} as legal_form from keyed
),

entity_names as (
    select
        "LEI" as lei,
        "Entity.LegalAddress.Country" as country,
        coalesce("Entity.EntityStatus" = 'ACTIVE', false) as active,
        {{ vendor_match_key('"Entity.LegalName"') }} as match_key
    from {{ source('landing', 'gleif-legal-entities') }}
),

entity_keys as (
    select
        *,
        {{ vendor_base_key('match_key') }} as base_key,
        {{ vendor_legal_form('match_key') }} as legal_form
    from entity_names
),

-- Every candidate of each rule; only an exact match may be a legal entity of another country
-- than the entity's own.
candidates as (
    select v.entity_id, v.vendor_name, v.home_country, 1 as rule_rank, e.lei, e.country, e.active
    from vendor_keys as v inner join entity_keys as e on v.match_key = e.match_key
    union all
    select v.entity_id, v.vendor_name, v.home_country, 2, e.lei, e.country, e.active
    from vendor_keys as v
    inner join entity_keys as e
        on
            v.base_key = e.base_key
            and v.legal_form = e.legal_form
            and true
    where v.legal_form <> ''
    union all
    select v.entity_id, v.vendor_name, v.home_country, 3, e.lei, e.country, e.active
    from vendor_keys as v
    inner join entity_keys as e on v.base_key = e.base_key and true
),

-- Of a rule's candidates, those in the entity's own country come first, then the active ones.
preferred as (
    select
        *,
        rank() over (
            partition by entity_id, vendor_name, rule_rank
            order by country = home_country desc, active desc
        ) as preference
    from candidates
),

decided as (
    select
        entity_id,
        vendor_name,
        rule_rank,
        count(distinct lei) as candidate_count,
        min(lei) as lei
    from preferred
    where preference = 1
    group by entity_id, vendor_name, rule_rank
    qualify rule_rank = min(rule_rank) over (partition by entity_id, vendor_name)
)

select
    n.entity_id,
    n.vendor_name,
    n.landed_rows,
    case
        when n.hashed then 'hashed'
        when d.rule_rank is null then 'unmatched'
        when d.candidate_count > 1 then 'ambiguous'
        else 'matched'
    end as match_status,
    case d.rule_rank
        when 1 then 'exact'
        when 2 then 'same_legal_form'
        when 3 then 'legal_form_dropped'
    end as match_rule,
    case when d.candidate_count = 1 then d.lei end as lei,
    cast(
        case
            when d.candidate_count = 1 and d.rule_rank = 1 then 1.0
            when d.candidate_count = 1 and d.rule_rank = 2 then 0.9
            when d.candidate_count = 1 then 0.7
        end as {{ dbt.type_float() }}
    ) as match_score,
    coalesce(d.candidate_count, 0) as candidate_count
from names as n
left join decided as d on n.entity_id = d.entity_id and n.vendor_name = d.vendor_name
