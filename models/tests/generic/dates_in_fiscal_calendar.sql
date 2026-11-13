{#- Fails on every row whose date has no day in its entity's fiscal calendar, so a date outside the
    years the calendars cover fails a build instead of dropping out of a join. -#}
{% test dates_in_fiscal_calendar(model, column_name, entity_column='entity_id') %}
    select
        checked.{{ entity_column }} as entity_id,
        checked.{{ column_name }} as checked_date
    from {{ model }} as checked
    left join {{ ref('fiscal_calendar') }} as fiscal_calendar
        on
            checked.{{ entity_column }} = fiscal_calendar.entity_id
            and cast(checked.{{ column_name }} as date) = fiscal_calendar.calendar_date
    where checked.{{ column_name }} is not null and fiscal_calendar.calendar_date is null
{% endtest %}
