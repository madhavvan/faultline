-- One row per customer: the behavioural aggregates the churn model trains on, plus the
-- label itself. Label and features living in the same table is normal and fine -- what
-- matters is that no feature is computed *from* the label.
with order_stats as (
    select
        customer_id,
        avg(case when order_date >= cast('2026-07-01' as date) - 30 then amount_usd end) as avg_order_value_30d,
        count(case when is_completed and order_date >= cast('2026-07-01' as date) - 90 then 1 end) as order_count_90d,
        max(order_date) as last_order_date
    from "warehouse"."main_staging"."stg_orders"
    group by customer_id
),

ticket_stats as (
    select
        customer_id,
        avg(resolution_hours) as avg_resolution_hours
    from "warehouse"."main_staging"."stg_tickets"
    group by customer_id
)

select
    c.customer_id,
    c.email_hash,
    c.email_domain,
    c.country,
    c.tenure_days,
    o.avg_order_value_30d,
    o.order_count_90d,
    t.avg_resolution_hours,
    case when o.last_order_date < cast('2026-07-01' as date) - 90 then true else false end as churned
from "warehouse"."main_staging"."stg_customers" c
left join order_stats o on o.customer_id = c.customer_id
left join ticket_stats t on t.customer_id = c.customer_id