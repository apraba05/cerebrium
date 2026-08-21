#!/usr/bin/env bash
# One-command demo: Redis warm pool ← MCP ← LangChain agent, vs reactive baseline.
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"

REDIS_NAME="${REDIS_NAME:-cerebrium-warm-redis}"
REDIS_PORT="${REDIS_PORT:-16380}"
export REDIS_URL="${REDIS_URL:-redis://127.0.0.1:${REDIS_PORT}/0}"
RESULTS="${ROOT}/.results"

need_docker() {
  if docker info >/dev/null 2>&1; then
    return 0
  fi
  if sg docker -c 'docker info' >/dev/null 2>&1; then
    exec sg docker -c "\"$0\" $*"
  fi
  echo "Docker is required (and your user must reach the docker socket)." >&2
  exit 1
}

load_secrets() {
  for f in \
    "${HOME}/.config/startup-demos.env" \
    "${ROOT}/../../.env" \
    "${ROOT}/../../../.env" \
    "${ROOT}/.env"; do
    if [[ -f "$f" ]]; then
      set -a
      # shellcheck disable=SC1090
      source "$f"
      set +a
    fi
  done
  export AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
  export AWS_DEFAULT_REGION="$AWS_REGION"
}

cleanup() {
  docker rm -f "${REDIS_NAME}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

need_docker "$@"
need_bin() { command -v "$1" >/dev/null 2>&1 || { echo "missing: $1" >&2; exit 1; }; }
need_bin python3.11
load_secrets

echo "==> Redis (${REDIS_NAME} on :${REDIS_PORT})"
docker rm -f "${REDIS_NAME}" >/dev/null 2>&1 || true
docker run -d --name "${REDIS_NAME}" -p "${REDIS_PORT}:6379" redis:7-alpine >/dev/null
for _ in $(seq 1 40); do
  if docker exec "${REDIS_NAME}" redis-cli PING 2>/dev/null | grep -q PONG; then
    break
  fi
  sleep 0.25
done
docker exec "${REDIS_NAME}" redis-cli PING | grep -q PONG

echo "==> Python venv (LangChain + MCP + Redis)"
if [[ ! -d .venv ]]; then
  python3.11 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -q -r requirements.txt

mkdir -p "${RESULTS}"
rm -f "${RESULTS}"/reactive.json "${RESULTS}"/prewarmed.json

echo
echo "################################################################"
echo "# RUN 1 — reactive baseline (agent DISABLED)"
echo "################################################################"
docker exec "${REDIS_NAME}" redis-cli DEL warm_pool:us-east-1 >/dev/null
python scheduler.py --mode reactive --traffic traffic.csv \
  --out "${RESULTS}/reactive.json"

echo
echo "################################################################"
echo "# RUN 2 — agent ENABLED (forecast → MCP → Redis → scheduler)"
echo "################################################################"
docker exec "${REDIS_NAME}" redis-cli DEL warm_pool:us-east-1 >/dev/null
# Seed a tiny pool so the agent must raise it; mirrors a quiet baseline fleet.
docker exec "${REDIS_NAME}" redis-cli SET warm_pool:us-east-1 1 >/dev/null
echo "Redis warm_pool:us-east-1 before agent: $(docker exec "${REDIS_NAME}" redis-cli GET warm_pool:us-east-1)"
python agent.py
echo "Redis warm_pool:us-east-1 after agent:  $(docker exec "${REDIS_NAME}" redis-cli GET warm_pool:us-east-1)"
python scheduler.py --mode prewarmed --traffic traffic.csv \
  --out "${RESULTS}/prewarmed.json"

echo
echo "################################################################"
echo "# SIDE-BY-SIDE (causal link: reasoning → warm/cold ratio)"
echo "################################################################"
python - <<'PY'
import json
from pathlib import Path
r = json.loads(Path(".results/reactive.json").read_text())
p = json.loads(Path(".results/prewarmed.json").read_text())
print(f"{'metric':<22} {'reactive':>12} {'agent+MCP':>12}")
print("-" * 48)
print(f"{'warm hits':<22} {r['warm_hits']:>12} {p['warm_hits']:>12}")
print(f"{'cold starts':<22} {r['cold_hits']:>12} {p['cold_hits']:>12}")
print(f"{'warm ratio':<22} {r['warm_ratio']:>11.0%} {p['warm_ratio']:>11.0%}")
print(f"{'avg latency (ms)':<22} {r['avg_latency_ms']:>12.0f} {p['avg_latency_ms']:>12.0f}")
delta = r["avg_latency_ms"] - p["avg_latency_ms"]
print("-" * 48)
print(f"avg latency improvement with agent pre-warm: {delta:.0f} ms/request")
PY

echo
echo "Done. Trap will stop Redis."
