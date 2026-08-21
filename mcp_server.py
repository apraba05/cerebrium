"""MCP control plane: one tool writes desired warm-pool size into Redis.

Redis key: warm_pool:<region>  (same store the scheduler consumes)
"""

from __future__ import annotations

import json
import os

import redis
from mcp.server import MCPServer

REDIS_URL = os.environ.get("REDIS_URL", "redis://127.0.0.1:16380/0")
KEY_PREFIX = "warm_pool:"

app = MCPServer("cerebrium-warm-pool")


def _client() -> redis.Redis:
    return redis.Redis.from_url(REDIS_URL, decode_responses=True)


@app.tool()
def set_warm_pool_size(region: str, count: int) -> str:
    """Set the desired warm worker pool size for a region (pre-provision capacity).

    Args:
        region: Region id, e.g. us-east-1.
        count: Desired number of warm workers (>= 0).
    """
    if count < 0:
        return json.dumps({"error": "count must be >= 0"})
    region = region.strip() or "us-east-1"
    key = f"{KEY_PREFIX}{region}"
    r = _client()
    r.set(key, int(count))
    return json.dumps(
        {
            "ok": True,
            "region": region,
            "warm_pool_size": int(count),
            "redis_key": key,
        }
    )


if __name__ == "__main__":
    app.run()
