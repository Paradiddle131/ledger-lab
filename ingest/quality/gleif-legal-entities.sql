-- The landed register records, every column as landed, then what the suite's rules read: the last
-- update as a yyyymmdd number, null for a day no calendar has; the countries with their numbers
-- of records, as one text; whether the record has no real legal-form code (8888 or 9999); and,
-- from the counts the step wrote in the file's metadata, the records it left out as the
-- register's sole proprietor category, under a sole-proprietorship form, under a partnership form
-- that is no separate legal person (and of those, as a Dutch firm, 62Y3), or without a real
-- legal-form code and with a legal name that is not evidence of an organisation, and the other
-- names it blanked.
with countries as (
    select string_agg(country || ' ' || records, ', ' order by country) as records_of_countries
    from (
        select "Entity.LegalAddress.Country" as country, count(*) as records
        from landed
        group by 1
    )
),
left_out as (
    select
        sum(value::integer) filter (where key = 'sole proprietor category') as sole_proprietors,
        sum(value::integer) filter (where key = 'unknown or blank category') as unknown_category,
        sum(value::integer) filter (where key like 'sole proprietorship %') as sole_proprietorships,
        sum(value::integer) filter (where key like 'partnership %') as partnerships,
        sum(value::integer) filter (where key = 'partnership 62Y3') as dutch_firms,
        sum(value::integer) filter (where key like 'no legal form, %') as without_legal_form,
        sum(value::integer) filter (where key = 'other names blanked') as other_names_blanked
    from json_each((select value from landed_metadata where key = 'left out'))
)
select
    landed.*,
    cast(
        strftime(try_cast(left("Registration.LastUpdateDate", 10) as date), '%Y%m%d')
        as integer
    ) as updated_on,
    countries.records_of_countries,
    cast("Entity.LegalForm.EntityLegalFormCode" in ('8888', '9999') as integer)
        as is_without_real_legal_form,
    left_out.sole_proprietors as left_out_sole_proprietors,
    left_out.unknown_category as left_out_unknown_category,
    left_out.sole_proprietorships as left_out_sole_proprietorships,
    left_out.partnerships as left_out_partnerships,
    left_out.dutch_firms as left_out_dutch_firms,
    left_out.without_legal_form as left_out_without_legal_form,
    left_out.other_names_blanked as other_names_blanked
from landed
cross join left_out
cross join countries
