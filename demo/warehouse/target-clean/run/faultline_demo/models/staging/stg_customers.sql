
  
    
    

    create  table
      "warehouse"."main_staging"."stg_customers__dbt_tmp"
  
    as (
      -- Customer dimension. The raw address is hashed here; the domain is retained because
-- account teams segment on it. That retained domain is what later reaches the model.
select
    customer_id,
    md5(email)                                      as email_hash,
    split_part(email, '@', 2)                       as email_domain,
    country,
    date_diff('day', cast(signup_at as date), cast('2026-07-01' as date)) as tenure_days
from "warehouse"."main_raw"."raw_customers"
    );
  
  