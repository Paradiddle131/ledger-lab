-- The landed budget statements, every column as landed, then what the suite's rules read: the year
-- and amount as numbers, how many moments each served row has, the article a row shares with
-- another served row, whether a name is doubly encoded or has spaces at its edges, whether the
-- amount is zero or negative, and, as one text, the years and moments at which the file serves
-- no non-zero figure at all.
select
    *,
    try_cast(Begrotingsjaar as integer) as year_number,
    try_cast(amount as bigint) as amount_value,
    count(*) over (partition by source_row) as moments_of_served_row,
    case
        when count(distinct source_row) over (
            partition by Begrotingsjaar, VUO, HoofdstukRomeins, Artikelnummer
        ) > 1
            then concat_ws(' ', Begrotingsjaar, VUO, HoofdstukRomeins, Artikelnummer)
    end as article_on_several_rows,
    cast(
        contains(Hoofdstuknaam, 'Ã') or contains(Artikelnaam, 'Ã') as integer
    ) as is_doubly_encoded_name,
    cast(
        Hoofdstuknaam <> trim(Hoofdstuknaam) or Artikelnaam <> trim(Artikelnaam) as integer
    ) as is_name_with_edge_space,
    cast(amount = '0' as integer) as is_zero_amount,
    cast(try_cast(amount as bigint) < 0 as integer) as is_negative_amount,
    (
        select coalesce(string_agg(year_moment, ', ' order by year_moment), 'none')
        from (
            select Begrotingsjaar || ' ' || moment as year_moment
            from landed
            group by Begrotingsjaar, moment
            having count(*) filter (where amount <> '0') = 0
        )
    ) as moments_not_served
from landed
order by source_row, moment
