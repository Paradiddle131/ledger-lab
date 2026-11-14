-- The landed contracts, every column as landed, then what the suite's rules read: the award as a
-- number and the approval day as a yyyymmdd number, null for a day no calendar has; how many rows the contract has, and a
-- contract's number on its first row when it has several; the order number's format (a digit as
-- 9, a letter as A); whether the award is negative or zero, the revision number not a number, the
-- row a further copy of a revision already listed, a start or end year outside 1900 to 2099, or
-- the end before the start, or a start or end day no calendar has; whether the vendor id is missing; whether the vendor name is hashed,
-- and the vendor id hashed where the name is not or the other way round; the hashed name; and
-- whether a hashed row kept what describes the vendor.
with numbered as (
    select
        *,
        count(*) over (partition by purchase_order_contract_number) as rows_of_contract,
        row_number() over (
            partition by purchase_order_contract_number order by revision_number
        ) as row_of_contract,
        row_number() over (
            partition by purchase_order_contract_number, revision_number
            order by approval_date, vendor_name
        ) as row_of_revision
    from landed
)
select
    * exclude (row_of_contract, row_of_revision),
    try_cast(award_amount as decimal(18, 2)) as award,
    cast(strftime(try_strptime(approval_date, '%Y-%m-%dT%H:%M:%S.%g'), '%Y%m%d') as integer) as approved_on,
    case
        when rows_of_contract > 1 and row_of_contract = 1 then purchase_order_contract_number
    end as contract_with_several_rows,
    regexp_replace(
        regexp_replace(purchase_order_contract_number, '[0-9]', '9', 'g'), '[A-Za-z]', 'A', 'g'
    ) as order_number_format,
    cast(try_cast(award_amount as decimal(18, 2)) < 0 as integer) as is_negative_award,
    cast(try_cast(award_amount as decimal(18, 2)) = 0 as integer) as is_zero_award,
    cast(try_cast(revision_number as integer) is null as integer) as is_non_numeric_revision,
    cast(row_of_revision > 1 as integer) as is_repeated_revision,
    cast(
        left(start_date, 4) not between '1900' and '2099'
        or left(end_date, 4) not between '1900' and '2099' as integer
    ) as is_implausible_year,
    cast(end_date < start_date as integer) as is_end_before_start,
    cast(
        (start_date is not null and try_strptime(start_date, '%Y-%m-%dT%H:%M:%S.%g') is null)
        or (end_date is not null and try_strptime(end_date, '%Y-%m-%dT%H:%M:%S.%g') is null) as integer
    ) as is_impossible_start_or_end,
    cast(vendor_id is null as integer) as is_without_vendor_id,
    vendor_name like 'person:%' as is_hashed_vendor,
    cast(
        vendor_id is not null and (vendor_id like 'id:%') <> (vendor_name like 'person:%')
        as integer
    ) as is_vendor_id_hashed_unlike_name,
    case when vendor_name like 'person:%' then vendor_name end as hashed_vendor,
    cast(
        vendor_name like 'person:%'
        and (purchase_order_description is not null or zip is not null or contract_pdf is not null)
        as integer
    ) as is_hashed_row_describing_vendor
from numbered
