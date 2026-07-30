
  
    
    

    create  table
      "warehouse"."main_staging"."stg_orders__dbt_tmp"
  
    as (
      -- Normalises the raw order feed: one row per order, money converted to the reporting
-- currency, status flattened to a boolean.
select
    order_id,
    customer_id,
    round(amount * 1.0, 2)                          as amount_usd,
    case when status = 'shipped' then true else false end as is_completed,
    cast(created_at as date)                        as order_date
from "warehouse"."main_raw"."raw_orders"
    );
  
  