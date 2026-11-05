"""shop_sync command line.

  python -m shop_sync fetch --out RUN/sm.json
  python -m shop_sync plan  --sm RUN/sm.json --board RUN/board --out RUN/plan

`plan` reads the board snapshot (one JSON file per document, as the
artifact-database tool saves them, plus versions.json) and writes:
  RUN/plan/summary.json       counts, notes, batch file names
  RUN/plan/batch-01.json ...  up to 50 writes each, data in RUN/plan/data/
Nothing here writes to the board or to ShopMonkey.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .fetch import fetch_snapshot
from .shopmonkey import Client
from .sync import desired_state, plan_writes

CONFIG = Path(__file__).with_name("config.json")


def load_cfg() -> dict:
    return json.loads(CONFIG.read_text())


def parse_now(s: str | None) -> datetime:
    if not s:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def load_board(d: Path) -> tuple[dict, dict]:
    board = {}
    for coll in ("vehicles", "appointments", "config"):
        board[coll] = {p.stem: json.loads(p.read_text()) for p in sorted((d / coll).glob("*.json"))}
    vfile = d / "versions.json"
    versions = json.loads(vfile.read_text()) if vfile.exists() else {}
    return board, versions


def cmd_fetch(a) -> int:
    cfg = load_cfg()
    client = Client(gap_seconds=cfg["request_gap_seconds"])
    snap = fetch_snapshot(client, cfg, parse_now(a.now))
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snap))
    print(json.dumps({"orders": len(snap["orders"]), "appointments": len(snap["appointments"]),
                      "labor_orders": len(snap["services"]), "requests": client.calls}))
    return 0


def cmd_plan(a) -> int:
    cfg = load_cfg()
    sm = json.loads(Path(a.sm).read_text())
    board, versions = load_board(Path(a.board))
    desired = desired_state(sm, cfg)
    result = plan_writes(desired, board, versions, cfg, parse_now(a.now))
    out = Path(a.out)
    (out / "data").mkdir(parents=True, exist_ok=True)
    names = []
    for i, batch in enumerate(result["batches"], 1):
        entries = []
        for w in batch:
            e = {k: v for k, v in w.items() if k != "data"}
            if "data" in w:
                f = out / "data" / f"{w['collection']}--{w['doc_id']}.json"
                f.write_text(json.dumps(w["data"], indent=1))
                e["file_path"] = str(f.resolve())
            entries.append(e)
        name = f"batch-{i:02d}.json"
        (out / name).write_text(json.dumps(entries, indent=1))
        names.append(name)
    summary = {"summary": result["summary"], "notes": result["notes"], "batches": names,
               "writes": sum(len(b) for b in result["batches"])}
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="shop_sync")
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("fetch", help="read ShopMonkey into a snapshot file")
    f.add_argument("--out", required=True)
    f.add_argument("--now")
    f.set_defaults(fn=cmd_fetch)
    q = sub.add_parser("plan", help="work out the board writes")
    q.add_argument("--sm", required=True)
    q.add_argument("--board", required=True)
    q.add_argument("--out", required=True)
    q.add_argument("--now")
    q.set_defaults(fn=cmd_plan)
    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
