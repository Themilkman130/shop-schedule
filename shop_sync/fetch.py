"""Pull everything one sync needs from ShopMonkey into a single snapshot dict."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .shopmonkey import Client


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def fetch_snapshot(client: Client, cfg: dict, now: datetime) -> dict:
    tz = ZoneInfo(cfg["timezone"])
    day0 = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    until = day0 + timedelta(days=cfg["appointment_days_ahead"])

    statuses = client.list("workflow_status")
    users = client.list("user")
    orders = client.list("order", where={"archived": False, "deleted": False})
    appointments = client.list("appointment", where={
        "startDate": {"gte": iso_utc(day0), "lt": iso_utc(until)},
    })

    names = {s["id"]: s["name"] for s in statuses}
    wanted = set(cfg["in_shop_statuses"]) | set(cfg["upcoming_statuses"])
    orders = [o for o in orders if names.get(o.get("workflowStatusId")) in wanted]

    # Labor lines for every in-shop order and every order an upcoming appointment points at.
    order_ids = {o["id"] for o in orders}
    for a in appointments:
        o = a.get("order")
        if a.get("orderId") and o and not o.get("archived") and not o.get("deleted"):
            order_ids.add(a["orderId"])
    services = {oid: client.services(oid) for oid in sorted(order_ids)}

    vehicle_ids = {o.get("vehicleId") for o in orders}
    customer_ids = {o.get("customerId") for o in orders}
    vehicles = client.by_ids("vehicle", vehicle_ids)
    customers = client.by_ids("customer", customer_ids)

    return {
        "fetchedAt": iso_utc(now),
        "statuses": statuses,
        "users": users,
        "orders": orders,
        "appointments": appointments,
        "services": services,
        "vehicles": vehicles,
        "customers": customers,
        "calls": client.calls,
    }
