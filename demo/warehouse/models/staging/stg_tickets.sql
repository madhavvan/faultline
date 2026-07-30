-- Support tickets with resolution time derived from the open/resolve timestamps.
select
    ticket_id,
    customer_id,
    date_diff('hour', opened_at, resolved_at)       as resolution_hours,
    satisfaction_score
from {{ ref('raw_support_tickets') }}
