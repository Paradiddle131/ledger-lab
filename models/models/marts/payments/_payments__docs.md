{% docs payment_key %}
A key naming one payment, unique across entities: a hash of the entity and every landed column of the payment's row, numbered among rows identical in all of them.
It is the same in every landing made with the same hash key; a new hash key changes it on every row whose payee is hashed, and a row the publisher changes gets a new key.
{% enddocs %}

{% docs payment_entity_id %}
The entity that paid, by its id in `access/personas.yaml` and the calendar seeds (`chicago`).
{% enddocs %}

{% docs payment_currency %}
The currency the entity paid in, its own (`USD` for Chicago); amounts are never translated here.
{% enddocs %}

{% docs payment_date %}
The day the payment was made (Chicago's check date).
Null when the source gives only a year: a rolled-up row is never dated 1 January or any other day.
{% enddocs %}

{% docs payment_fiscal_year %}
The entity's fiscal year, from the fiscal calendar: the year of the payment's day, or for a row the source gives only a year, the fiscal year that is exactly that calendar year (Chicago's runs January to December).
Null for Chicago's 2002 row, which holds 1996 to 2002, and wherever the calendars do not cover the year (they cover the `fiscal_years` var's years).
{% enddocs %}

{% docs payment_fiscal_period %}
The fiscal period (1 to 12) of the payment's day, from the fiscal calendar; null when the payment has no day or the calendars do not cover it.
{% enddocs %}

{% docs payment_amount %}
The amount paid, a decimal in the entity's currency (never a float), with its sign as served: a refund or reversal is negative.
{% enddocs %}

{% docs payment_vendor_name %}
The payee as landed: the name in clear, or `person:` and its keyed hash for a payee who is or may be a person.
{% enddocs %}

{% docs payment_vendor_is_hashed %}
True when the payee's name is a keyed hash.
{% enddocs %}

{% docs payment_document_number %}
The document the payment was made on: Chicago's voucher number, as served (`Multiple` for a payment covering several vouchers); null on a rolled-up row, which has none.
{% enddocs %}

{% docs payment_contract_number %}
The contract the payment was made against, as served; null where there is none (Chicago's `DV`, a direct voucher).
A number the contracts table lacks stays, with no award.
{% enddocs %}

{% docs payment_award_after_revisions %}
The contract's award once every revision is applied, a decimal in the entity's currency; null where the payment has no contract or its contract number finds none.
For Chicago it is the sum of the award amounts of the contract's original award and every revision, each revision counted once: every row of the contracts table is a change to the award, not a new total, so a negative amount takes money off, and a revision numbered by the legacy system (`C514026`) counts like any other.
An award of 0 is no fixed award, not a break: the City describes a blanket contract awarded $0 as one that "depends upon requirements", ordered as needed (5,328 contracts on the pinned files, 2,125 of them paid $2.73bn in all); 28 contracts have a negative award.
{% enddocs %}

{% docs payment_is_repeat %}
True on a row that repeats another served row of the same entity, so a sum that leaves repeats out counts each payment once; false on the first row of each group and wherever an entity has no rule for repeats.
For Chicago a row is a repeat when it is a copy after the first of rows identical in every landed column (2,560 rows on the pinned files), when it has no department beside an otherwise identical row that has one (the row with the department is the first; 7,300 rows), or when it is a rolled-up year whose amount equals to the cent the year's other dated payments of its vendor and contract (the dated rows are the first; 4 rows).
These are 9,864 of 475,609 rows and $3.999bn; that they are the same payment served again rests on paid totals, not on the City saying so: with repeats left out, 21,960 contracts are paid exactly their award after revisions, against 21,264 with them, and 849 contracts are paid exactly twice their award with them.
{% enddocs %}

{% docs payment_source_table %}
The landed table the row comes from (`chicago-payments`).
{% enddocs %}
