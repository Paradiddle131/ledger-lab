-- The payments data product: one row per payment of every entity, each entity's intermediate
-- model in one column shape. An entity joins by adding its model to `entities`.
-- The payee and document columns after `is_repeat` split the published mixed ones by the hash
-- each value carries, here once for every entity: an organisation's name or a document number in
-- clear, and a person's keyed hash, in columns of their own.
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

-- Every document number a hashed payee's row carries in clear (Chicago's vouchers): such a number
-- names its payee in the publisher's own search, so no clear column holds it on any row.
beside_a_hashed_payee as (
    select distinct entity_id, document_number
    from shared
    where vendor_is_hashed and document_number is not null
)

select
    {% for column in columns %}shared.{{ column }},
    {% endfor -%}
    case when not shared.vendor_is_hashed then shared.vendor_name end as vendor_organisation_name,
    case when shared.vendor_is_hashed then shared.vendor_name end as vendor_person_key,
    case
        when
            not shared.vendor_is_hashed
            and shared.document_number not like 'id:%'
            and beside_a_hashed_payee.document_number is null
            then shared.document_number
    end as document_reference,
    case when shared.document_number like 'id:%' then shared.document_number end as document_key
from shared
left join beside_a_hashed_payee
    on
        shared.entity_id = beside_a_hashed_payee.entity_id
        and shared.document_number = beside_a_hashed_payee.document_number
