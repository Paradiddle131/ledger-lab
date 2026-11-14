-- The payments data product: one row per payment of every entity, each entity's intermediate
-- model in one column shape. An entity joins by adding its model to `entities`.
-- The columns after `is_repeat` split the published payee by the hash each value carries, and
-- keep the document numbers a clear column may show, here once for every entity.
{%- set entities = [
    'int_chicago__payments',
    'int_uk_transport__payments',
] %}
{%- set columns = [
    'payment_key',
    'entity_id',
    'currency',
    'payment_date',
    'fiscal_year',
    'fiscal_period',
    'amount',
    'vendor_name',
    'vendor_is_hashed',
    'document_number',
    'contract_number',
    'award_after_revisions',
    'source_table',
    'is_repeat',
] %}

with shared as (
    {% for entity in entities %}
        select {{ columns | join(', ') }} from {{ ref(entity) }}
        {% if not loop.last %}union all{% endif %}
    {% endfor %}
),

-- Every voucher number a hashed Chicago payee's row carries in clear: the City's own search names
-- the payee by it, so no clear column holds it on any row.
beside_a_hashed_payee as (
    select distinct document_number
    from shared
    where entity_id = 'chicago' and vendor_is_hashed and document_number is not null
)

select
    {% for column in columns %}shared.{{ column }},
    {% endfor -%}
    case when not shared.vendor_is_hashed then shared.vendor_name end as vendor_organisation_name,
    case when shared.vendor_is_hashed then shared.vendor_name end as vendor_person_key,
    -- Only Chicago's numbers, as the published contract's rule holds the column for the UK: the
    -- UK landing already hashes every number a hashed row carries, wherever it stands. A
    -- placeholder (`Multiple`) names no document, so it is no reference.
    case
        when
            shared.entity_id = 'chicago'
            and not shared.vendor_is_hashed
            and shared.document_number <> 'Multiple'
            and beside_a_hashed_payee.document_number is null
            then shared.document_number
    end as document_reference
from shared
left join beside_a_hashed_payee
    on shared.document_number = beside_a_hashed_payee.document_number
