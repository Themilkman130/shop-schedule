# shop-schedule

Keeps the Shop Production Board (a claude.ai artifact with a shared database)
in step with ShopMonkey. Each run reads ShopMonkey with GET requests only and
works out which board documents to add, update or remove.

## Pieces

| File | What it does |
|---|---|
| `shop_sync/shopmonkey.py` | Read-only ShopMonkey v3 client: GET only, paced under the 100 requests/minute limit, refuses partial lists |
| `shop_sync/fetch.py` | Pulls statuses, users, open orders, appointments, labor lines, vehicles and customers into one snapshot |
| `shop_sync/sync.py` | Maps the snapshot onto board documents and plans the writes (pure functions, fully tested) |
| `shop_sync/config.json` | Time zone, which ShopMonkey statuses count as in the shop or upcoming, tech names, limits |
| `tests/` | Unit tests on made-up data: `python3 -m unittest discover -s tests -t .` |

## Where the key comes from

The ShopMonkey API key is a network secret on the **ShopMonkey** cloud
environment. The proxy adds it to requests to `api.shopmonkey.cloud`. It is
never in this repo, in environment variables, or in chat.

## What the sync owns

Board documents written by the sync have `source: "shopmonkey"` and ids
`sm-o-<orderId>` (cars in the shop) or `sm-a-<appointmentId>` (upcoming work).

- **From ShopMonkey (overwritten each run):** RO, vehicle, customer, stage
  (ShopMonkey status), stage date, due date, techs, hours left per tech split
  into authorized / not authorized.
- **Board only (never written by the sync):** `spot`, `note`, `schedOrder`,
  `hoursLeft` (a manual override of ShopMonkey's hours).
- **Removed:** cars whose order is archived or leaves the in-shop statuses;
  appointments that have arrived, passed or been cancelled; pencilled jobs
  that match a ShopMonkey appointment (same customer, same Pacific day);
  sample rows, once live data exists.

## Running a sync (what the scheduled session does)

`RUN` is a fresh scratch directory. Snapshots contain customer data: keep them
out of the repo (`runs/` is git-ignored).

1. `python3 -m shop_sync fetch --out RUN/sm.json`
2. Save the board with the artifact-database tool (`list` with `out_dir` = `RUN/board`)
   for `vehicles` and `appointments`, and `get` `config/shop`. Write
   `RUN/board/versions.json` mapping `"<collection>/<doc_id>"` to the
   `version` each listing reported.
3. `python3 -m shop_sync plan --sm RUN/sm.json --board RUN/board --out RUN/plan`
4. Apply each `RUN/plan/batch-NN.json` with one artifact-database `batch` call
   (the entries already carry `if_version` and `file_path`). If a batch is
   refused because a document changed, start again from step 2.

If step 1 fails for any reason, stop: never plan from a partial snapshot.
