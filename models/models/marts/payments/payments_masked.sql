-- The payments data product as its masked readers read it: each column a tag in
-- access/columns.yaml marks under that tag's masking rule, every other column as it is. A test
-- holds `masked` to the column list and rules of columns.yaml. On BigQuery its governed copy in
-- the product's dataset is what the masked readers' row access policies grant
-- (access/row_policies.py).
{%- set masked = {
    'vendor_name': 'hash',
    'document_number': 'hash',
    'vendor_person_key': 'hash',
} %}
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
    'vendor_organisation_name',
    'vendor_person_key',
    'document_reference',
] %}

select
    {% for column in columns -%}
    {%- if column in masked %}{{ mask(column, masked[column]) }} as {{ column }}
    {%- else %}{{ column }}{% endif %}{{ ',' if not loop.last }}
    {% endfor %}
from {{ ref('payments') }}
