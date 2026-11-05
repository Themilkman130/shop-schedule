"""Read-only ShopMonkey v3 client.

Only GET requests are possible through this module: there is no method for
anything else. The API key is never handled here. In the cloud environment the
network proxy adds it to every request to api.shopmonkey.cloud, so the
Authorization header below is only a placeholder that the proxy replaces.
A real key can be supplied through SHOPMONKEY_API_KEY when running elsewhere.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "https://api.shopmonkey.cloud/v3"
PAGE = 100


class ShopMonkeyError(RuntimeError):
    pass


class Client:
    def __init__(self, gap_seconds: float = 0.7, base: str = BASE):
        self.base = base
        self.gap = gap_seconds
        self.calls = 0
        self._last = 0.0
        self._token = os.environ.get("SHOPMONKEY_API_KEY") or "proxy-injected"

    def _get(self, path: str, params: dict | None = None) -> dict:
        query = urllib.parse.urlencode(params or {})
        url = f"{self.base}/{path}" + (f"?{query}" if query else "")
        for attempt in range(4):
            wait = self._last + self.gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            req = urllib.request.Request(url, method="GET", headers={
                "Authorization": f"Bearer {self._token}",
                "Accept": "application/json",
                "User-Agent": "shop-schedule-sync",
            })
            self._last = time.monotonic()
            self.calls += 1
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    body = json.load(resp)
            except urllib.error.HTTPError as e:
                if e.code == 429 or e.code >= 500:
                    retry = e.headers.get("Retry-After")
                    time.sleep(float(retry) if retry and retry.isdigit() else 5 * (attempt + 1))
                    continue
                raise ShopMonkeyError(f"GET {path} failed: HTTP {e.code}") from None
            if body.get("success") is False:
                raise ShopMonkeyError(f"GET {path} failed: {body.get('message')}")
            return body
        raise ShopMonkeyError(f"GET {path} kept failing after retries")

    def list(self, path: str, where: dict | None = None, **extra) -> list[dict]:
        """All rows of a list endpoint, following hasMore with skip.

        A short read would make the sync delete cars that are still in the
        shop, so the result is checked against the total ShopMonkey reports
        and an incomplete list raises instead of being returned.
        """
        rows: dict[str, dict] = {}
        skip, total = 0, None
        while True:
            params = {"limit": PAGE, "skip": skip, "orderBy": json.dumps({"id": "asc"}), **extra}
            if where is not None:
                params["where"] = json.dumps(where, separators=(",", ":"))
            body = self._get(path, params)
            data = body.get("data") or []
            meta = body.get("meta") or {}
            if isinstance(meta.get("total"), int):
                total = meta["total"]
            for r in data:
                rows[r["id"]] = r
            skip += len(data)
            if not meta.get("hasMore") or not data:
                break
        if total is not None and len(rows) < total:
            raise ShopMonkeyError(f"GET {path}: got {len(rows)} of {total} rows; refusing to sync on a partial list")
        return list(rows.values())

    def by_ids(self, path: str, ids) -> list[dict]:
        ids = sorted({i for i in ids if i})
        out: list[dict] = []
        for i in range(0, len(ids), PAGE):
            out.extend(self.list(path, where={"id": {"in": ids[i:i + PAGE]}}))
        return out

    def services(self, order_id: str) -> list[dict]:
        return self._get(f"order/{urllib.parse.quote(order_id)}/service").get("data") or []
