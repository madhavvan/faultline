-- Online feature table: what the serving path reads at inference time.
--
-- It recomputes from staging rather than reading the mart, because serving cannot wait for
-- the nightly mart to land. That is a reasonable design -- and it is exactly why the two
-- definitions can drift apart without anything failing.
--
-- FAULT 2 (train/serve skew): the averaging window here was narrowed from 30 days to 7
-- during an incident and never restored. The offline copy still trains on 30. Both columns
-- stay numeric and in range, so no distribution monitor will ever see it.
{% set window_days = 30 if var('clean', false) else 7 %}
{% set compliance = not var('clean', false) %}

with recent_orders as (
    select
        customer_id,
        avg(case when order_date >= {{ as_of() }} - {{ window_days }} then amount_usd end)
            as avg_order_value_30d,
        count(case when is_completed and order_date >= {{ as_of() }} - 90 then 1 end)
            as order_count_90d
    from {{ ref('stg_orders') }}
    group by customer_id
),

recent_tickets as (
    select
        customer_id,
        avg(resolution_hours) as avg_resolution_hours
    from {{ ref('stg_tickets') }}
    group by customer_id
)

select
    c.customer_id,
    o.avg_order_value_30d,
    o.order_count_90d,
    t.avg_resolution_hours
{%- if compliance %},
    c.email_domain
{%- endif %}
from {{ ref('stg_customers') }} c
left join recent_orders o on o.customer_id = c.customer_id
left join recent_tickets t on t.customer_id = c.customer_id
