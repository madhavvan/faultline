
  
    
    

    create  table
      "warehouse"."main_marts"."customer_risk__dbt_tmp"
  
    as (
      -- Segment-level churn rate, built for the retention dashboard.
--
-- FAULT 1 (target leakage). This is computed *from* the churn label. As a dashboard metric
-- that is correct and useful. The defect appears one model later, when it is picked up as a
-- model feature: at that point the model is being handed an aggregate of the answer.
-- Nothing here is wrong in isolation, which is why review did not catch it.
select
    customer_id,
    country,
    avg(case when churned then 1.0 else 0.0 end)
        over (partition by country) as segment_churn_rate
from "warehouse"."main_marts"."customer_360"
    );
  
  