# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

An AIOps chat service for BankSystem_16 infrastructure monitoring. Users ask questions in Korean natural language (e.g., "어제 was01에서 500 에러가 왜 발생했어?"), and the system queries Prometheus/Loki/Jaeger, then returns LLM-generated analysis via SSE streaming.

## Commands

**Install dependencies:**
```bash
pip install -r requirements.txt
```

**Run development server** (from parent of `monitoring_llm/`):
```bash
uvicorn monitoring_llm.api.main:app --host 0.0.0.0 --port 8000 --reload
```

**Run production server** (single worker required — LangGraph singleton):
```bash
uvicorn monitoring_llm.api.main:app --host 0.0.0.0 --port 8000 --workers 1
```

**Start observability backends:**
```bash
cd docker_stack && docker compose up -d
```

**Health check:**
```bash
curl http://localhost:8000/monitoring/health
```

**Test chat (streaming):**
```bash
curl -N -X POST http://localhost:8000/monitoring/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"어제 was01에서 500 에러가 왜 발생했어?","session_id":"s01"}'
```

**NLP unit tests:**
```bash
python -m monitoring_llm.nlp.test
```

**Enable mock mode** (no real backends needed):
```bash
MOCK_MODE=true uvicorn monitoring_llm.api.main:app --host 0.0.0.0 --port 8000 --reload
```

## Architecture

### Request Flow

```
POST /monitoring/chat
  → routes.py: chat_stream()
  → session.py: SessionStore.get_or_create()   # TTL-based in-memory session
  → sse.py: stream_agent()                      # wraps sync LangGraph in async SSE
  → graph.py: stream_query()                    # LangGraph state machine
      → node_nlp_parse()    NLP pipeline → intent + entities
      → intent_router()     conditional edge → appropriate tool node
      → node_call_*()       queries Prometheus / Loki / Jaeger / CMDB
      → node_respond()      Ollama LLM generates Korean natural language answer
  → SSE stream: progress events → text chunks → done
```

### Six Intents and Their Tool Nodes

| Intent | Trigger example | Node | Backends |
|--------|----------------|------|----------|
| `INCIDENT_HISTORY` | "어제 무슨 문제 있었어?" | `call_incident` | Prometheus AlertManager |
| `ASSET_INFO` | "was01 서버 정보" | `call_cmdb` | SQLite CMDB |
| `METRIC_RANGE` | "최근 CPU 사용률?" | `call_prometheus` | Prometheus |
| `MULTI_MODAL` | "was01에서 뭐가 문제야?" | `call_multi` | Prometheus + Loki |
| `ERROR_ANALYSIS` | "500 에러 분석해줘" | `call_error` | Loki + Jaeger + error_classifier |
| `ACTION_RECOMMEND` | "어떻게 해야 돼?" | `call_action` | action_recommender (LLM) |

### Session State (`agent/state.py: MonitoringState`)

Each session carries: `messages` (conversation history), `current_servers` (resolved server list with hostname/IP/Prometheus job/Loki service), `last_time_range`, `last_intent`, `tool_results`, `final_response`, `error`.

The session resolves server references ("was01") to full metadata by querying the CMDB, enabling pronouns like "그 서버" in follow-up questions.

### NLP Pipeline (`nlp/`)

1. `intent_classifier.py` — regex rule-based first; falls back to Ollama LLM if confidence < threshold
2. `entity_extractor.py` — regex-based: hostnames, IPs, error codes, service names
3. `time_parser.py` — Korean time expressions ("어제", "최근 1시간", "지난 주") → `(start_ts, end_ts)`
4. `param_binder.py` — maps NLP output to Prometheus/Loki query parameters

### Key Constraints

- **`workers=1` required** — LangGraph builds a singleton graph at startup; multiple workers would each hold independent state.
- **Module must be run as a package** — always `uvicorn monitoring_llm.api.main:app`, not `uvicorn api.main:app`.
- **CMDB auto-seeds** — if `cmdb.db` is missing at startup, `cmdb/database.py` seeds BankSystem_16 test data automatically.

## Configuration (`.env`)

| Variable | Default | Purpose |
|----------|---------|---------|
| `OLLAMA_BASE_URL` | `http://192.168.0.42:11434` | Local Ollama server |
| `OLLAMA_MODEL` | `qwen3:8b` | LLM model |
| `PROMETHEUS_URL` | `http://192.168.0.41:9092` | Metrics backend |
| `LOKI_URL` | `http://192.168.0.41:3101` | Log backend |
| `JAEGER_URL` | — | Trace backend |
| `CMDB_DB_PATH` | `cmdb.db` | SQLite path |
| `MOCK_MODE` | `false` | Return mock data instead of querying backends |
| `SESSION_TTL_HOURS` | `2` | Session expiry |

## Monitored Infrastructure (BankSystem_16)

- **web01/02** (Linux) — OTel exporters, Promtail → Loki
- **was01/02** (Windows) — Grafana Alloy, JMX exporter → Prometheus
- **db01/02** (Windows) — Grafana Alloy, MySQL exporter → Prometheus
