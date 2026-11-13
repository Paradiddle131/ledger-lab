-- One row per served row of the Dutch budget statements and budget moment, typed: the year as an
-- integer, the amount in euros as a decimal (the publisher's whole thousands times 1,000, so no
-- figure is rounded), and the chapter and article names with their edge spaces trimmed and the
-- two doubly encoded letters the file serves repaired. Every served year is kept; the mart keeps
-- the years the fiscal calendar covers. The `nl_government_budget` var picks the rows: the file
-- the fetch kit landed (`landed`) or the committed sample of real landed rows (`sample`).
{%- set chosen = var('nl_government_budget') %}
{%- if chosen == 'landed' %}
    {%- set served = source('nl_government', 'budget') %}
{%- elif chosen == 'sample' %}
    {%- set served = ref('nl_government_budget_sample') %}
{%- else %}
    {{ exceptions.raise_compiler_error(
        "the nl_government_budget var is 'sample' or 'landed', not '" ~ chosen ~ "'"
    ) }}
{%- endif %}

select
    cast(source_row as {{ dbt.type_int() }}) as source_row,
    cast(Begrotingsjaar as {{ dbt.type_int() }}) as fiscal_year,
    VUO as kind,
    HoofdstukRomeins as chapter_code,
    trim(replace(replace(Hoofdstuknaam, 'Ã§', 'ç'), 'Ã©', 'é')) as chapter_name,
    Artikelnummer as article_number,
    trim(replace(replace(Artikelnaam, 'Ã§', 'ç'), 'Ã©', 'é')) as article_name,
    moment,
    cast(cast(amount as {{ dbt.type_numeric() }}) * 1000 as {{ dbt.type_numeric() }}) as amount_eur,
    unit as served_unit
from {{ served }}
