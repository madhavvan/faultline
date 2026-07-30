-- Offline feature table: what the churn model trains on.
{% set leaky = not var('clean', false) %}
{% set compliance = not var('clean', false) %}

select
    c.customer_id,
    c.avg_order_value_30d,
    c.order_count_90d,
    c.avg_resolution_hours
{%- if compliance %},
    -- FAULT 4: the retained email domain crosses into the feature store, and from here
    -- into a deployed model, carrying no classification with it.
    c.email_domain
{%- endif %}
{%- if leaky %},
    -- FAULT 1, second half: the segment churn rate becomes a training feature.
    r.segment_churn_rate
{%- endif %}
from {{ ref('customer_360') }} c
{%- if leaky %}
left join {{ ref('customer_risk') }} r on r.customer_id = c.customer_id
{%- endif %}
