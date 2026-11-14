-- Two files for one month published at the same time would both be current, so the month would
-- count twice in payments; which supersedes the other must be decided before either lands.
select file_month, published_at, count(distinct source_file) as files
from {{ ref('int_uk_transport__payment_versions') }}
group by file_month, published_at
having count(distinct source_file) > 1
