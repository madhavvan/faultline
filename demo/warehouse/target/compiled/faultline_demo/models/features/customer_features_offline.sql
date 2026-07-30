-- Offline feature table: what the churn model trains on.



select
    c.customer_id,
    c.avg_order_value_30d,
    c.order_count_90d,
    c.avg_resolution_hours,
    -- FAULT 4: the retained email domain crosses into the feature store, and from here
    -- into a deployed model, carrying no classification with it.
    c.email_domain,
    -- FAULT 1, second half: the segment churn rate becomes a training feature.
    r.segment_churn_rate
from "warehouse"."main_marts"."customer_360" c
left join "warehouse"."main_marts"."customer_risk" r on r.customer_id = c.customer_id