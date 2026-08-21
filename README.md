# Predictive Pre-Warm Agent (LLM-Driven Capacity Forecasting)

LangChain/Bedrock agent + MCP tool-calling wired into a real scaling control plane, showing fluency in both the infra layer and the LLM-tooling layer Cerebrium's customers actually use.

**Live demo:** https://cerebrium.ashanpraba.com

The demo runs entirely in the browser against seeded data — no API keys,
no accounts, and no external services required.

## Stack

- Python
- LangChain
- Bedrock (or local LLM stand-in)
- MCP
- Redis

## How it works

- One MCP tool, set_warm_pool_size(region, count), that writes desired warm-pool size into Redis (the same store from the scheduler demo).
- Feed a synthetic time-series of request volume (e.g. a CSV of the last 30 minutes with an upcoming spike) to a LangChain agent via Bedrock.
- Prompt the agent to reason about the next 5 minutes of expected load and call the MCP tool to adjust warm pool size ahead of the spike.
- Scheduler consumes the new pool size and pre-provisions workers before the synthetic spike hits, avoiding cold starts that would otherwise occur.
- Log agent's reasoning trace alongside the scheduler's resulting warm/cold ratio so the causal link is visible on screen.
- Run once with the agent disabled (reactive-only baseline) and once enabled, showing side-by-side latency difference.

## Running locally

```bash
cd src
bash run.sh
```

Then open the printed URL. A prebuilt static version of the UI lives in
`src/web/` and can be opened directly with no server.
