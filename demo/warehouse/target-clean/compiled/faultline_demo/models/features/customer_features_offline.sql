-- Offline feature table: what the churn model trains on.



select
    c.customer_id,
    c.avg_order_value_30d,
    c.order_count_90d,
    c.avg_resolution_hours
from "warehouse"."main_marts"."customer_360" c