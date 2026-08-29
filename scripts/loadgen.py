#!/usr/bin/env python3
"""Synthetic load generator for the day2 api — drives realistic traffic so the
Grafana dashboards show life (request rate, latency, a job backlog that builds
and drains, the occasional 4xx).

Standard library only, so it runs anywhere python3 does — no venv, no pip.

Local (Kind), against a port-forward:
    kubectl -n default port-forward svc/day2-api 8000:8000 &
    python3 scripts/loadgen.py --base-url http://localhost:8000 --duration 300 --rps 25

Cloud (k3s), through the Traefik ingress + nginx /api proxy:
    python3 scripts/loadgen.py --base-url http://<public-ip>/api --duration 300 --rps 25

Or in-cluster with no port-forward (hits the Service directly):
    kubectl -n default run loadgen --rm -it --restart=Never \
      --image=python:3.12-slim --command -- \
      python3 -c "$(cat scripts/loadgen.py)" \
        --base-url http://day2-api:8000 --duration 300

The traffic mix mirrors what the web dashboard issues (stats/list polling plus
item and job creation), at higher volume. Job creation deliberately outpaces the
worker a little so the queue-depth panel visibly rises and drains; a fraction of
jobs reference a missing item to exercise the 4xx path.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

_lock = threading.Lock()
_stats: Counter[str] = Counter()
_created_item_ids: list[int] = []


def _request(
    method: str, url: str, body: dict | None = None, timeout: float = 5.0
) -> tuple[int, bytes]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("content-type", "application/json")
    try:
        # The URL is the operator-supplied --base-url (their own load target),
        # scheme-restricted to http(s) in main() — not untrusted input.
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosemgrep
            payload = resp.read()
            return resp.status, payload
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, b""  # connection failure — counted separately from HTTP status


def _record(code: int) -> None:
    bucket = "conn_error" if code == 0 else f"{code // 100}xx"
    with _lock:
        _stats[bucket] += 1
        _stats["total"] += 1


def _do_action(base: str) -> None:
    """One weighted action against the api."""
    roll = random.random()
    if roll < 0.35:
        code, _ = _request("GET", f"{base}/jobs/stats")
    elif roll < 0.55:
        code, _ = _request("GET", f"{base}/items?limit=20")
    elif roll < 0.68:
        code, _ = _request("GET", f"{base}/jobs?limit=20")
    elif roll < 0.80:
        # Create an item and remember its id for later job binding.
        name = f"item-{random.randint(0, 1_000_000)}"
        code, payload = _request("POST", f"{base}/items", {"name": name})
        if code == 201 and payload:
            try:
                item_id = json.loads(payload).get("id")
                if isinstance(item_id, int):
                    with _lock:
                        _created_item_ids.append(item_id)
                        del _created_item_ids[:-500]  # keep the list bounded
            except (ValueError, AttributeError):
                pass
    else:
        # Enqueue a job. ~15% reference a missing item on purpose (→ 404), the
        # rest bind to a real item when we have one.
        if random.random() < 0.15:
            missing = random.randint(10_000_000, 20_000_000)
            body = {"item_id": missing, "kind": "index_item"}
        elif _created_item_ids:
            with _lock:
                item_id = random.choice(_created_item_ids)
            body = {"item_id": item_id, "kind": "index_item"}
        else:
            body = {"kind": "index_item"}
        code, _ = _request("POST", f"{base}/jobs", body)
    _record(code)


def main() -> int:
    parser = argparse.ArgumentParser(description="day2 api synthetic load generator")
    parser.add_argument("--base-url", required=True, help="e.g. http://localhost:8000 or http://<ip>/api")
    parser.add_argument("--duration", type=int, default=300, help="seconds to run")
    parser.add_argument("--rps", type=float, default=25.0, help="requests per second")
    parser.add_argument("--concurrency", type=int, default=16, help="worker threads")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    if not base.startswith(("http://", "https://")):
        parser.error("--base-url must be an http:// or https:// URL")
    interval = 1.0 / args.rps if args.rps > 0 else 0.0
    deadline = time.monotonic() + args.duration
    next_report = time.monotonic() + 10.0
    print(f"load: {args.rps} rps -> {base} for {args.duration}s", flush=True)

    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        try:
            while time.monotonic() < deadline:
                tick = time.monotonic()
                pool.submit(_do_action, base)
                now = time.monotonic()
                if now >= next_report:
                    with _lock:
                        snap = dict(_stats)
                    elapsed = int(args.duration - (deadline - now))
                    print(f"  t+{elapsed}s {snap}", flush=True)
                    next_report = now + 10.0
                sleep = interval - (time.monotonic() - tick)
                if sleep > 0:
                    time.sleep(sleep)
        except KeyboardInterrupt:
            print("\ninterrupted", flush=True)

    with _lock:
        final = dict(_stats)
    print(f"done: {final}", flush=True)
    # Non-zero exit if essentially nothing succeeded — makes a broken target obvious.
    return 0 if final.get("2xx", 0) > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
