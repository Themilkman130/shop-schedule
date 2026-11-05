"""Turn a ShopMonkey snapshot plus a board snapshot into a list of board writes.

Pure functions only: no network, no clock (callers pass `now`), so the whole
mapping is covered by tests.

Board documents the sync owns carry source="shopmonkey". It never touches
other documents except: sample rows (deleted once live data arrives),
pencilled jobs (deleted when a matching ShopMonkey appointment shows up) and
the sample tech list in config/shop (replaced once).
"""
from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

SOURCE = "shopmonkey"
# Fields only people change on the board. The sync never writes them.
BOARD_FIELDS = ("spot", "note", "schedOrder", "hoursLeft")


def norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def user_label(u: dict) -> str:
    first, last = norm(u.get("firstName")), norm(u.get("lastName"))
    return f"{first} {last[:1]}." if last else first


def r2(x: float) -> float:
    return round(x + 1e-9, 2)


def parse_ts(s):
    if not s:
        return None
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def local_day(ts, tz: ZoneInfo):
    t = parse_ts(ts)
    return t.astimezone(tz).date() if t else None


# ---------------------------------------------------------------- labor

def labor_summary(services: list[dict], tech_for: dict[str, str], fallback: str = "") -> dict:
    """Hours not yet done, split per board tech and authorized / not.

    Deferred or recommended services are skipped: that work isn't happening on
    this visit. A line with no tech goes to `fallback` (the order's only
    assigned tech, if it has exactly one). Labor for anyone not on the board's
    tech list is unassigned.
    """
    techs: dict[str, dict] = {}
    left = auth = un = un_auth = 0.0
    for s in services or []:
        if s.get("deferredDate") or s.get("recommended") or s.get("hidden"):
            continue
        is_auth = s.get("authorizationStatus") == "Authorized"
        for line in s.get("labors") or []:
            if line.get("completed"):
                continue
            h = float(line.get("hours") or 0)
            if h <= 0:
                continue
            left += h
            auth += h if is_auth else 0
            tid = line.get("technicianId")
            name = tech_for.get(tid) if tid else fallback
            if name:
                t = techs.setdefault(name, {"left": 0.0, "auth": 0.0})
                t["left"] += h
                t["auth"] += h if is_auth else 0
            else:
                un += h
                un_auth += h if is_auth else 0
    return {
        "smHoursLeft": r2(left),
        "smAuthHoursLeft": r2(auth),
        "techHours": {k: {"left": r2(v["left"]), "auth": r2(v["auth"])} for k, v in sorted(techs.items())},
        "unassignedHours": r2(un),
        "unassignedAuthHours": r2(un_auth),
    }


def order_techs(order: dict, tech_for: dict[str, str]) -> list[str]:
    return [tech_for[i] for i in order.get("assignedTechnicianIds") or [] if i in tech_for]


def sole_tech(order: dict | None, tech_for: dict[str, str]) -> str:
    """The order's tech when exactly one person is assigned and they're on the board."""
    ids = (order or {}).get("assignedTechnicianIds") or []
    return tech_for.get(ids[0], "") if len(ids) == 1 else ""


def primary_tech(summary: dict, order_tech_names: list[str]) -> str:
    """The tech with the most hours left, else the first assigned on the order."""
    th = summary["techHours"]
    if th:
        return max(th, key=lambda k: (th[k]["left"], k))
    return order_tech_names[0] if order_tech_names else ""


def customer_label(c: dict | None, fallback: str = "") -> str:
    if not c:
        return norm(fallback)
    return norm(c.get("companyName")) or norm(c.get("lastName")) or norm(c.get("firstName")) or norm(fallback)


def vehicle_fields(v: dict | None) -> dict:
    v = v or {}
    return {
        "year": str(v.get("year") or ""),
        "make": norm(v.get("make")),
        "model": norm(v.get("model")),
        "color": norm(v.get("color")),
    }


# ---------------------------------------------------------------- desired state

def desired_state(sm: dict, cfg: dict) -> dict:
    tz = ZoneInfo(cfg["timezone"])
    status_name = {s["id"]: s["name"] for s in sm["statuses"]}
    wanted_techs = {norm(t).lower(): t for t in cfg["techs"]}
    tech_for = {}
    for u in sm["users"]:
        label = user_label(u)
        if label.lower() in wanted_techs:
            tech_for[u["id"]] = wanted_techs[label.lower()]
    vehicles = {v["id"]: v for v in sm["vehicles"]}
    customers = {c["id"]: c for c in sm["customers"]}
    in_shop = set(cfg["in_shop_statuses"])
    upcoming = set(cfg["upcoming_statuses"])

    out_v: dict[str, dict] = {}
    for o in sm["orders"]:
        stage = status_name.get(o.get("workflowStatusId"))
        if stage not in in_shop or o.get("archived") or o.get("deleted"):
            continue
        lab = labor_summary(sm["services"].get(o["id"], []), tech_for, sole_tech(o, tech_for))
        names = order_techs(o, tech_for)
        out_v[f"sm-o-{o['id']}"] = {
            "source": SOURCE,
            "smOrderId": o["id"],
            "ro": str(o.get("number") or ""),
            **vehicle_fields(vehicles.get(o.get("vehicleId"))),
            "customer": customer_label(customers.get(o.get("customerId")), o.get("generatedCustomerName")),
            "service": norm(o.get("name") or o.get("complaint"))[:80],
            "stage": stage,
            "stageSince": o.get("workflowStatusDate") or o.get("updatedDate"),
            "promised": o.get("dueDate"),
            "tech": primary_tech(lab, names),
            "orderTechs": names,
            "authorized": bool(o.get("authorized")),
            **lab,
        }

    out_a: dict[str, dict] = {}
    pool: list[dict] = []  # every non-cancelled appointment, for matching pencilled jobs
    for a in sm["appointments"]:
        o = a.get("order") or None
        if not a.get("cancellationNote"):
            pool.append({"id": a["id"], "start": a.get("startDate"), "customer": customer_label(a.get("customer"))})
        if o:
            if o.get("archived") or o.get("deleted"):
                continue
            stage = status_name.get(o.get("workflowStatusId"))
            if stage not in upcoming:
                continue  # already in the shop (counted as a vehicle) or past it
        if a.get("cancellationNote"):
            continue
        lab = labor_summary(sm["services"].get(a.get("orderId") or "", []), tech_for, sole_tech(o, tech_for)) if o else labor_summary([], {})
        names = order_techs(o, tech_for) if o else []
        v = a.get("vehicle") or {}
        vf = vehicle_fields(v)
        out_a[f"sm-a-{a['id']}"] = {
            "source": SOURCE,
            "smAppointmentId": a["id"],
            "smOrderId": a.get("orderId"),
            "ro": str(o.get("number") or "") if o else "",
            "start": a.get("startDate"),
            "end": a.get("endDate"),
            "allDay": bool(a.get("allDay")),
            "day": str(local_day(a.get("startDate"), tz)),
            **vf,
            "vehicle": " ".join(x for x in (vf["year"], vf["make"], vf["model"]) if x),
            "customer": customer_label(a.get("customer")),
            "service": norm(a.get("name") or (o or {}).get("name"))[:80],
            "hours": lab["smHoursLeft"],
            "authHours": lab["smAuthHoursLeft"],
            "tech": primary_tech(lab, names),
            "orderTechs": names,
            "techHours": lab["techHours"],
            "unassignedHours": lab["unassignedHours"],
            "unassignedAuthHours": lab["unassignedAuthHours"],
            "confirmed": a.get("confirmationStatus") == "Confirmed",
        }
    return {"vehicles": out_v, "appointments": out_a, "matchPool": pool, "techLabels": sorted(set(tech_for.values()))}


# ---------------------------------------------------------------- matching pencilled jobs

def _cust_key(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", norm(s).lower())


def pencil_matches(pencil: dict, appt: dict, tz: ZoneInfo) -> bool:
    """Same customer (last name or company) on the same local day."""
    pd, ad = local_day(pencil.get("start"), tz), local_day(appt.get("start"), tz)
    if not pd or pd != ad:
        return False
    p, a = _cust_key(pencil.get("customer")), _cust_key(appt.get("customer"))
    return bool(p) and bool(a) and (p == a or (len(p) >= 3 and (p in a or a in p)))


# ---------------------------------------------------------------- plan

def _changed(old: dict, new: dict) -> dict:
    return {k: v for k, v in new.items() if old.get(k) != v}


def plan_writes(desired: dict, board: dict, versions: dict, cfg: dict, now: datetime) -> dict:
    """board = {"vehicles": {id: data}, "appointments": {id: data}, "config": {"shop": data}}
    versions = {"vehicles/<id>": n, ...} for every existing document."""
    tz = ZoneInfo(cfg["timezone"])
    today = now.astimezone(tz).date()
    writes, notes = [], []
    summary = {k: 0 for k in ("vehicles_added", "vehicles_updated", "vehicles_removed",
                              "appointments_added", "appointments_updated", "appointments_removed",
                              "pencilled_removed", "sample_removed", "config_updated")}

    def ver(coll, doc_id):
        v = versions.get(f"{coll}/{doc_id}")
        if v is None:
            raise ValueError(f"no version recorded for existing document {coll}/{doc_id}")
        return v

    def put(op, coll, doc_id, data=None):
        w = {"op": op, "collection": coll, "doc_id": doc_id}
        if data is not None:
            w["data"] = data
        if doc_id in board.get(coll, {}):
            w["if_version"] = ver(coll, doc_id)
        writes.append(w)

    live = bool(desired["vehicles"] or desired["appointments"])

    for coll, kind in (("vehicles", "vehicles"), ("appointments", "appointments")):
        have = board.get(coll, {})
        want = desired[coll]
        for doc_id, data in sorted(want.items()):
            if doc_id in have:
                delta = _changed(have[doc_id], data)
                if delta:
                    put("update", coll, doc_id, delta)
                    summary[f"{kind}_updated"] += 1
            else:
                put("set", coll, doc_id, data)
                summary[f"{kind}_added"] += 1
        for doc_id, data in sorted(have.items()):
            if data.get("source") == SOURCE and doc_id not in want:
                put("delete", coll, doc_id)
                summary[f"{kind}_removed"] += 1
            elif data.get("sample") and live:
                put("delete", coll, doc_id)
                summary["sample_removed"] += 1

    # Pencilled jobs: matched by a real appointment, or already arrived as an in-shop car.
    sm_appts = desired.get("matchPool", [])
    for doc_id, p in sorted(board.get("appointments", {}).items()):
        if not p.get("tentative") or p.get("sample"):
            continue
        hit = next((a for a in sm_appts if pencil_matches(p, a, tz)), None)
        if hit:
            put("delete", "appointments", doc_id)
            summary["pencilled_removed"] += 1
            notes.append(f"pencilled job {doc_id} matched ShopMonkey appointment {hit['id']}")
        else:
            d = local_day(p.get("start"), tz)
            if d and d < today:
                notes.append(f"pencilled job {doc_id} is from {d} and never matched an appointment; left in place")

    shop = (board.get("config") or {}).get("shop")
    if live and (shop is None or shop.get("sample")):
        h = cfg["default_tech_hours"]
        data = {**(shop or {}), "techs": [{"name": t, "hours": {str(d): h for d in range(1, 6)}} for t in cfg["techs"]]}
        data.pop("sample", None)
        for k, v in (("open", "08:00"), ("close", "17:00"), ("lunchStart", "12:00"), ("lunchHours", 1)):
            data.setdefault(k, v)
        put("set", "config", "shop", data)
        summary["config_updated"] = 1

    missing = sorted(set(cfg["techs"]) - set(desired["techLabels"]))
    if missing:
        notes.append("no ShopMonkey user matched tech " + ", ".join(missing))

    size = cfg["max_writes_per_batch"]
    return {"summary": summary, "notes": notes, "batches": [writes[i:i + size] for i in range(0, len(writes), size)]}
