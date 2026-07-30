{#
  The demo's reference date, pinned to the seed data's horizon.

  Using current_date would make every window relative to whenever someone runs this, so the
  7-day online window silently returns all NULLs once the committed seeds are a month old --
  the skew would look like a data outage instead of the value drift it is. Pinning it keeps
  the numbers meaningful and the whole demo reproducible.
#}
{% macro as_of() %}cast('{{ var("as_of_date", "2026-07-01") }}' as date){% endmacro %}
