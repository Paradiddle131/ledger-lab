-- The payments data product: one row per payment of every entity, each entity's intermediate
-- model in one column shape. An entity joins by adding its model to `entities`.
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

{% for entity in entities %}
    select {{ columns | join(', ') }} from {{ ref(entity) }}
    {% if not loop.last %}union all{% endif %}
{% endfor %}
