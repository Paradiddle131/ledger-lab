{% docs payment_key %}
A key naming one payment, unique across entities: a hash of the entity and every landed column of the payment's row, numbered among rows identical in all of them.
It is the same in every landing made with the same hash key; a new hash key changes it on every row whose payee is hashed, and a row the publisher changes gets a new key.
For the UK Department for Transport it is `uk-transport:`, the served file's name and the row's position in it, stable while the file stays pinned; a revised month's new file gives every row of that month a new key, unchanged rows included.
{% enddocs %}

{% docs payment_entity_id %}
The entity that paid, by its id in `access/personas.yaml` and the calendar seeds (`chicago`, `uk-transport`).
The Dutch government's `nl-government` is accepted too, though no row carries it: its budget file holds budget lines, not payments, so it adds none.
{% enddocs %}

{% docs payment_currency %}
The currency the entity paid in, its own (`USD` for Chicago, `GBP` for the UK department, `EUR`, which no row carries yet, for the Dutch government); amounts are never translated here.
{% enddocs %}

{% docs payment_date %}
The day the payment was made (Chicago's check date, the UK department's served date).
Null when the source gives only a year: a rolled-up row is never dated 1 January or any other day.
{% enddocs %}

{% docs payment_fiscal_year %}
The entity's fiscal year, from the fiscal calendar: the year of the payment's day, or for a row the source gives only a year, the fiscal year that is exactly that calendar year (Chicago's runs January to December; the UK department's April to March, labelled by the year it opens in).
Null for Chicago's 2002 row, which holds 1996 to 2002, and wherever the calendars do not cover the year (they cover the `fiscal_years` var's years).
{% enddocs %}

{% docs payment_fiscal_period %}
The fiscal period (1 to 12) of the payment's day, from the fiscal calendar; null when the payment has no day or the calendars do not cover it.
{% enddocs %}

{% docs payment_amount %}
The amount paid, a decimal in the entity's currency (never a float), with its sign as served: a refund or reversal is negative.
{% enddocs %}

{% docs payment_vendor_name %}
The payee as landed: the name in clear, `person:` and its keyed hash for a payee who is or may be a person, or the UK department's own `REDACTED`, which names no one.
{% enddocs %}

{% docs payment_vendor_is_hashed %}
True when the payee's name is a keyed hash.
{% enddocs %}

{% docs payment_document_number %}
The document the payment was made on: Chicago's voucher number, as served (`Multiple` for a payment covering several vouchers), null on a rolled-up row, which has none; the UK department's transaction number as landed, `#` where it gives none and a keyed hash (`id:`) on a hashed or redacted row, so it repeats.
{% enddocs %}

{% docs payment_contract_number %}
The contract the payment was made against, as served; null where there is none (Chicago's `DV`, a direct voucher, and every UK row: the department's files carry no contract).
A number the contracts table lacks stays, with no award.
{% enddocs %}

{% docs payment_award_after_revisions %}
The contract's award once every revision is applied, a decimal in the entity's currency; null where the payment has no contract or its contract number finds none, as on every UK row.
For Chicago it is the sum of the award amounts of the contract's original award and every revision, each revision counted once: every row of the contracts table is a change to the award, not a new total, so a negative amount takes money off, and a revision numbered by the legacy system (`C514026`) counts like any other.
An award of 0 is no fixed award, not a break: the City describes a blanket contract awarded $0 as one that "depends upon requirements", ordered as needed (5,328 contracts on the pinned files, 2,125 of them paid $2.73bn in all); 28 contracts have a negative award.
{% enddocs %}

{% docs payment_is_repeat %}
True on a row that repeats another served row of the same entity, so a sum that leaves repeats out counts each payment once; false on the first row of each group and wherever an entity has no rule for repeats.
For Chicago a row is a repeat when it is a copy after the first of rows identical in every landed column, when it has no department beside an otherwise identical row that has one (the row with the department is the first), or when it is a rolled-up year whose amount equals to the cent the year's other dated payments of its vendor and contract (the dated rows are the first); `int_chicago__payments` names each repeat's kind.
That they are the same payment served again rests on paid totals, not on the City saying so: with repeats left out, more contracts are paid exactly their award after revisions than with them.
For the UK department no row is flagged: its rows identical as landed are consecutive rows under one transaction number, and nothing in the files tells those identical in every served cell from two equal lines of one document, while hashed ones that differ in the description the landing drops are certainly separate lines (`int_uk_transport__payments` gives the evidence).
{% enddocs %}

{% docs payment_source_table %}
The landed table the row comes from (`chicago-payments`, `uk-transport-spend-2025`).
{% enddocs %}
