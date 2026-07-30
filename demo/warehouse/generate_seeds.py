"""Generate the demo warehouse seed data.

Deterministic (fixed seed) so the committed CSVs, the dbt run, the parsed lineage and the
scan results are all reproducible. Anyone can re-run this and get byte-identical files.
"""

from __future__ import annotations

import csv
import random
from datetime import datetime, timedelta
from pathlib import Path

SEED = 20260727
N_CUSTOMERS = 240
N_ORDERS = 1400
N_TICKETS = 380

HERE = Path(__file__).parent
SEEDS = HERE / "seeds"

COUNTRIES = ["US", "US", "US", "GB", "DE", "FR", "CA", "AU", "IN", "BR"]
DOMAINS = [
    "gmail.com", "gmail.com", "outlook.com", "yahoo.com",
    "acme-corp.com", "globex.io", "initech.dev", "umbrella.co",
]
STATUSES = ["shipped", "shipped", "shipped", "shipped", "placed", "cancelled", "refunded"]
FIRST = ["ana", "ben", "chi", "dev", "eli", "fay", "gus", "hana", "ivan", "jo", "kai", "lea"]
LAST = ["park", "silva", "novak", "okafor", "tanaka", "muller", "rossi", "khan", "dubois"]


def main() -> None:
    rng = random.Random(SEED)
    SEEDS.mkdir(parents=True, exist_ok=True)
    now = datetime(2026, 7, 1)

    # -- customers ---------------------------------------------------------------------
    customers = []
    for cid in range(1, N_CUSTOMERS + 1):
        name = f"{rng.choice(FIRST)}.{rng.choice(LAST)}{rng.randint(1, 99)}"
        customers.append(
            {
                "customer_id": cid,
                "email": f"{name}@{rng.choice(DOMAINS)}",
                "phone": f"+1{rng.randint(2000000000, 9899999999)}",
                "country": rng.choice(COUNTRIES),
                "signup_at": (now - timedelta(days=rng.randint(30, 1400))).isoformat(sep=" "),
            }
        )
    _write("raw_customers.csv", customers)

    # -- orders ------------------------------------------------------------------------
    # A slice of customers deliberately goes quiet in the last 90 days: those become the
    # positive churn class, so the label is a real property of the data rather than a coin
    # flip. That matters -- the leakage finding is only interesting if the label is real.
    churners = set(rng.sample(range(1, N_CUSTOMERS + 1), k=N_CUSTOMERS // 4))

    orders = []
    for oid in range(1, N_ORDERS + 1):
        cid = rng.randint(1, N_CUSTOMERS)
        max_age = 400 if cid in churners else 200
        min_age = 95 if cid in churners else 0
        created = now - timedelta(days=rng.randint(min_age, max_age))
        orders.append(
            {
                "order_id": oid,
                "customer_id": cid,
                "amount": round(rng.lognormvariate(3.9, 0.7), 2),
                "status": rng.choice(STATUSES),
                "created_at": created.isoformat(sep=" "),
            }
        )
    orders.sort(key=lambda o: o["created_at"])
    for i, order in enumerate(orders, start=1):
        order["order_id"] = i
    _write("raw_orders.csv", orders)

    # -- support tickets ---------------------------------------------------------------
    tickets = []
    for tid in range(1, N_TICKETS + 1):
        cid = rng.randint(1, N_CUSTOMERS)
        opened = now - timedelta(days=rng.randint(0, 365))
        resolution = rng.lognormvariate(1.6, 1.0)
        tickets.append(
            {
                "ticket_id": tid,
                "customer_id": cid,
                "opened_at": opened.isoformat(sep=" "),
                "resolved_at": (opened + timedelta(hours=resolution)).isoformat(sep=" "),
                "satisfaction_score": rng.randint(1, 5),
            }
        )
    _write("raw_support_tickets.csv", tickets)

    print(
        f"seeds written: {len(customers)} customers, {len(orders)} orders, "
        f"{len(tickets)} tickets ({len(churners)} churners)"
    )


def _write(name: str, rows: list[dict]) -> None:
    path = SEEDS / name
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


if __name__ == "__main__":
    main()
