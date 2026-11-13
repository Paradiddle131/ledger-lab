-- A file's month is the key a later file supersedes, so every landed file's rows lie in one
-- calendar month; the landing refuses a file spanning two, and this holds the models to it.
select source_file, count(distinct file_month) as months
from {{ ref('int_uk_transport__payment_versions') }}
group by source_file
having count(distinct file_month) <> 1
