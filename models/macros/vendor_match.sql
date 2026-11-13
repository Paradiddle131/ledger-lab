{#- The keys vendor names are matched to legal names on.

    `vendor_match_key`: the name's runs of letters and digits, and `&`, lower-cased and joined by
    single spaces; the landing's `ingest.entities.match_key` on names whose Unicode forms and case
    folding agree (a test holds the two to the same keys). DuckDB has only the NFC form, BigQuery
    the NFKC one the landing uses.
    `vendor_base_key`: that key without its final legal-form words (`ltd`, `inc`, `l l c` and the
    like), never down to nothing.
    `vendor_legal_form`: those final words, each spelling of a form written one way. -#}

{% macro vendor_legal_forms() -%}
    ltd|limited|inc|incorporated|corp|corporation|co|company|llc|l l c|plc|llp|l l p|lp|l p|bv|b v|nv|n v|pc|pllc
{%- endmacro %}

{% macro vendor_match_key(column) -%}
    {{ return(adapter.dispatch('vendor_match_key')(column)) }}
{%- endmacro %}

{% macro default__vendor_match_key(column) -%}
    array_to_string(regexp_extract_all(lower(normalize({{ column }}, NFKC)), r'[\p{L}\p{N}]+|&'), ' ')
{%- endmacro %}

{% macro duckdb__vendor_match_key(column) -%}
    array_to_string(regexp_extract_all(lower(nfc_normalize({{ column }})), '[\p{L}\p{N}]+|&'), ' ')
{%- endmacro %}

{% macro vendor_base_key(key) -%}
    regexp_replace({{ key }}, '( ({{ vendor_legal_forms() }}))+$', '')
{%- endmacro %}

{% macro vendor_legal_form(key) -%}
    trim(
        replace(replace(replace(replace(replace(replace(replace(replace(replace(
            substr({{ key }} || ' ', length({{ vendor_base_key(key) }}) + 1),
            ' limited ', ' ltd '), ' incorporated ', ' inc '), ' corporation ', ' corp '),
            ' company ', ' co '), ' l l c ', ' llc '), ' l l p ', ' llp '), ' l p ', ' lp '),
            ' b v ', ' bv '), ' n v ', ' nv ')
    )
{%- endmacro %}
