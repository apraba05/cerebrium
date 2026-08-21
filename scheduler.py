"""Warm-pool scheduler: serves synthetic traffic against Redis desired size.

Cold start = 3000ms (model/CUDA init). Warm hit = 100ms (already provisioned).
Reactive mode only grows the pool AFTER a minute of load — always lagging a spike.
Prewarmed mode honors the Redis size the MCP tool wrote ahead of the spike.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import redis

WARM_MS = 100
COLD_MS = 3000
REGION = "us-east-1"
KEY = f"warm_pool:{REGION}"
REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:16380/0")


@dataclass
class Tick:
    minute: int
    requests: int
    pool_before: int
    warm_hits: int
    cold_hits: int
    pool_after: int


@dataclass
class RunResult:
    mode: str
    region: str
    ticks: list[Tick] = field(default_factory=list)
    warm_hits: int = 0
    cold_hits: int = 0
    total_latency_ms: int = 0

    @property
    def total_requests(self) -> int:
        return self.warm_hits + self.cold_hits

    @property
    def warm_ratio(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.warm_hits / self.total_requests

    @property
    def avg_latency_ms(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.total_latency_ms / self.total_requests


def load_traffic(path: Path) -> list[tuple[int, int]]:
    rows: list[tuple[int, int]] = []
    with path.open() as f:
        for row in csv.DictReader(f):
            rows.append((int(row["minute"]), int(row["requests"])))
    return rows


def _redis() -> redis.Redis:
    return redis.Redis.from_url(REDIS_URL, decode_responses=True)


def get_pool(r: redis.Redis) -> int:
    raw = r.get(KEY)
    return int(raw) if raw is not None else 0


def set_pool(r: redis.Redis, n: int) -> None:
    r.set(KEY, max(0, int(n)))


def run_simulation(mode: str, traffic: list[tuple[int, int]], initial_pool: int = 1) -> RunResult:
    """mode: reactive | prewarmed"""
    r = _redis()
    if mode == "reactive":
        set_pool(r, initial_pool)
    # prewarmed: leave Redis as the agent (or seed) already set it

    result = RunResult(mode=mode, region=REGION)
    for minute, demand in traffic:
        pool = get_pool(r)
        warm = min(demand, pool)
        cold = max(0, demand - pool)
        # Serve this minute's requests against the pool that existed at the start.
        if mode == "reactive":
            # React AFTER seeing load — next minute gets max(pool, demand).
            set_pool(r, max(pool, demand))
        # prewarmed: pool stays at agent-chosen size (no reactive chase)
        pool_after = get_pool(r)
        tick = Tick(
            minute=minute,
            requests=demand,
            pool_before=pool,
            warm_hits=warm,
            cold_hits=cold,
            pool_after=pool_after,
        )
        result.ticks.append(tick)
        result.warm_hits += warm
        result.cold_hits += cold
        result.total_latency_ms += warm * WARM_MS + cold * COLD_MS
    return result


def print_result(result: RunResult) -> None:
    print(f"\n========== scheduler ({result.mode}) ==========")
    print(f"region={result.region}  warm={WARM_MS}ms  cold={COLD_MS}ms")
    print("min  demand  pool→  warm  cold  pool+")
    for t in result.ticks:
        # Highlight spike window for the camera.
        mark = " <<" if t.cold_hits and t.minute >= 25 else ""
        print(
            f"{t.minute:3d}  {t.requests:6d}  {t.pool_before:5d}  "
            f"{t.warm_hits:4d}  {t.cold_hits:4d}  {t.pool_after:5d}{mark}"
        )
    print(
        f"totals: requests={result.total_requests}  "
        f"warm={result.warm_hits}  cold={result.cold_hits}  "
        f"warm_ratio={result.warm_ratio:.0%}  "
        f"avg_latency={result.avg_latency_ms:.0f}ms"
    )
    print("==============================================\n")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mode", choices=("reactive", "prewarmed"), required=True)
    p.add_argument("--traffic", type=Path, default=Path(__file__).with_name("traffic.csv"))
    p.add_argument("--initial-pool", type=int, default=1)
    p.add_argument("--out", type=Path, help="Write JSON summary here")
    args = p.parse_args()

    traffic = load_traffic(args.traffic)
    result = run_simulation(args.mode, traffic, initial_pool=args.initial_pool)
    print_result(result)

    payload = {
        "mode": result.mode,
        "region": result.region,
        "warm_hits": result.warm_hits,
        "cold_hits": result.cold_hits,
        "warm_ratio": round(result.warm_ratio, 4),
        "avg_latency_ms": round(result.avg_latency_ms, 1),
        "total_latency_ms": result.total_latency_ms,
        "total_requests": result.total_requests,
        "ticks": [asdict(t) for t in result.ticks],
    }
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2) + "\n")
    else:
        # Still emit one-line JSON for run.sh capture when --out omitted.
        print("JSON:" + json.dumps({k: payload[k] for k in (
            "mode", "warm_hits", "cold_hits", "warm_ratio", "avg_latency_ms", "total_requests"
        )}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
