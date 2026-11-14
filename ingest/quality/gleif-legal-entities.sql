-- The landed register records, every column as landed, then what the suite's rules read: the last
-- update as a yyyymmdd number; each country with its number of records; whether the record has no
-- real legal-form code (8888 or 9999); and, from the counts the step wrote in the file's metadata,
-- the records it left out as the register's sole proprietor category, under a sole-proprietorship
-- form, under a partnership form that is no separate legal person, or without a real legal-form
-- code and with a legal name that is not evidence of an organisation, and the other names it
-- blanked.
with left_out as (
    select
        sum(value::integer) filter (where key = 'sole proprietor category') as sole_proprietors,
        sum(value::integer) filter (where key = 'unknown or blank category') as unknown_category,
        sum(value::integer) filter (where key like 'sole proprietorship %') as sole_proprietorships,
        sum(value::integer) filter (where key like 'partnership %') as partnerships,
        sum(value::integer) filter (where key like 'no legal form, %') as without_legal_form,
        sum(value::integer) filter (where key = 'other names blanked') as other_names_blanked
    from json_each((select value from landed_metadata where key = 'left out'))
)
select
    landed.*,
    cast(replace(left("Registration.LastUpdateDate", 10), '-', '') as integer) as updated_on,
    concat_ws(
        ' ',
        "Entity.LegalAddress.Country",
        count(*) over (partition by "Entity.LegalAddress.Country")
    ) as records_of_country,
    cast("Entity.LegalForm.EntityLegalFormCode" in ('8888', '9999') as integer)
        as is_without_real_legal_form,
    left_out.sole_proprietors as left_out_sole_proprietors,
    left_out.unknown_category as left_out_unknown_category,
    left_out.sole_proprietorships as left_out_sole_proprietorships,
    left_out.partnerships as left_out_partnerships,
    left_out.without_legal_form as left_out_without_legal_form,
    left_out.other_names_blanked as other_names_blanked
from landed
cross join left_out
