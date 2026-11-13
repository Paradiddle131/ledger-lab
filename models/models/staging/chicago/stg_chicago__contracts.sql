-- One row per landed row of the City's contracts, typed: each row is the original award of a
-- contract or one revision of it, its award amount the change that revision makes (so a negative
-- amount takes money off). Dates are the day the publisher serves (`YYYY-MM-DDT00:00:00.000`), the
-- amount a decimal, never a float. Codes and names stay as landed; a hashed vendor keeps its keyed
-- hashes. Free text, the contract link and the vendor's place are left to the landed file.
-- The `source_rows` var picks the rows: the landed file or the committed sample of real rows.
select
    purchase_order_contract_number as contract_number,
    revision_number,
    specification_number,
    contract_type,
    procurement_type,
    department,
    vendor_name,
    vendor_id,
    vendor_name like 'person:%' as vendor_is_hashed,
    cast(substr(approval_date, 1, 10) as date) as approved_on,
    cast(substr(start_date, 1, 10) as date) as starts_on,
    cast(substr(end_date, 1, 10) as date) as ends_on,
    cast(award_amount as {{ dbt.type_numeric() }}) as award_amount
from {{ landed_or_sample('landing', 'chicago-contracts', 'chicago_contracts_sample') }}
