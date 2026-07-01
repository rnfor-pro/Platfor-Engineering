# Project Handoff: GitHub Copilot OTel Open-Source Observability Pipeline

**Project name:** Copilot Telemetry Migration — App Insights → Open-Source Observability  
**Also known as:** Project Glasspane / Copilot OSS Observability  
**Status:** Architecture proven, VS Code config complete, dashboard built, pipeline verification in progress  
**Handoff date:** July 2026  

---

## 1. What this project is

Replace Azure Monitor + Application Insights as the backend for GitHub Copilot OTel telemetry with a fully open-source, vendor-agnostic stack already running in our organisation. The data emitted by VS Code/Copilot is identical in both approaches — only the destination changes.

**Goal:** Get GitHub Copilot telemetry (chat, CLI, agents, MCP tool calls, edit outcomes) flowing into our existing Grafana stack with no Azure dependency, no Application Insights ingestion costs, and no KQL.

---

## 2. Existing pipeline already in place

The following infrastructure is **already running** — this project adds Copilot as a new telemetry source, not a new platform:

```
VS Code (Copilot) 
  → OTel Collector (ingest, OTLP receiver :4317/:4318)
  → RedPanda (Kafka buffer, topics: otlp_traces / otlp_metrics / otlp_logs)
  → OTel Router (Kafka consumer, fans out to backends)
  → Tempo       (traces)
  → Loki        (logs)
  → VictoriaMetrics (metrics)
  → Grafana Enterprise v12.3+
```

**There was previously also a Dynatrace export path (crossed out in original diagram) — ignore that, it was descoped.**

---

## 3. Critical naming convention discovery

This is the most important thing to know before writing any queries. Confirmed from live VictoriaMetrics and Loki browsers:

| Backend | Convention | Example |
|---|---|---|
| VictoriaMetrics | **Dots preserved** in metric names | `gen_ai.client.token.usage_sum` |
| VictoriaMetrics labels | Mixed — some dotted, some underscore | `gen_ai.request.model` (dotted), `service_name` (underscore) |
| Loki fields | **Underscores** throughout | `gen_ai_request_model`, `gen_ai_usage_input_tokens` |
| Tempo span attributes | **Dots preserved** (native OTel) | `gen_ai.request.model`, `service.name` |

**Because metric names contain dots, ALL PromQL queries must use the quoted `__name__` form:**
```promql
{__name__="gen_ai.client.token.usage_sum", "gen_ai.token.type"="input"}
```
**NOT** the bare underscore form `gen_ai_client_token_usage_sum` — that metric does not exist in this instance.

---

## 4. Confirmed metric names (from live VictoriaMetrics browser)

```
gen_ai.client.operation.duration_bucket
gen_ai.client.operation.duration_count
gen_ai.client.operation.duration_sum
gen_ai.client.operation.time_per_output_chunk_bucket
gen_ai.client.operation.time_per_output_chunk_count
gen_ai.client.operation.time_per_output_chunk_sum
gen_ai.client.operation.time_to_first_chunk_bucket
gen_ai.client.operation.time_to_first_chunk_count
gen_ai.client.operation.time_to_first_chunk_sum
gen_ai.client.token.usage_bucket
gen_ai.client.token.usage_count
gen_ai.client.token.usage_sum
copilot_chat.agent.invocation.duration_bucket
copilot_chat.agent.invocation.duration_count
copilot_chat.agent.invocation.duration_sum
copilot_chat.agent.turn.count_bucket
copilot_chat.agent.turn.count_count
copilot_chat.agent.turn.count_sum
copilot_chat.session.count
copilot_chat.time_to_first_token_bucket
copilot_chat.time_to_first_token_count
copilot_chat.time_to_first_token_sum
github.copilot.enterprise.seat.active.last.28d (also confirmed)
```

**Confirmed VictoriaMetrics labels:**
`gen_ai.agent.name`, `gen_ai.operation.name`, `gen_ai.provider.name`, `gen_ai.request.model`, `gen_ai.response.model`, `gen_ai.token.type`, `service_name`, `userType`, `kubernetes_azure_com_*`

**Confirmed Loki fields:**
`detected_level`, `event_name`, `flags`, `gen_ai_agent_name`, `gen_ai_operation_name`, `gen_ai_request_max_tokens`, `gen_ai_request_model`, `gen_ai_response_finish_reasons`, `gen_ai_response_id`, `gen_ai_response_model`, `gen_ai_usage_input_tokens`, `gen_ai_usage_output_tokens`, `observed_timestamp`, `scope_name`, `scope_version`, `service_name`, `service_version`, `session_id`, `span_id`

**Confirmed Loki log content:**
```
GenAI inference: gpt-4o-mini-2024-07-18
GenAI inference: claude-sonnet-4.6
copilot_chat.session.start
```

---

## 5. VS Code settings.json (complete, all environments)

Source: https://code.visualstudio.com/docs/agents/guides/monitoring-agents

### What is captured by this settings file

- Chat interactions — every LLM call, token counts, TTFT, finish reasons, session starts
- Agent orchestration — `invoke_agent` spans for Copilot, Copilot CLI (VS Code-embedded), and Claude agents
- MCP tool calls — `execute_tool` spans with `gen_ai.tool.type=extension`, `mcp_server_name`, `mcp_tool_name`
- Hook execution — `execute_hook` spans (PreToolUse, Stop, etc.)
- Subagents — trace context propagates automatically across `runSubagent` calls
- Code edit outcomes — `copilot_chat.edit.acceptance.count`, `copilot_chat.lines_of_code.count`, `copilot_chat.edit.survival.four_gram`
- User engagement — copy/insert/apply actions, thumbs up/down feedback
- Git/repo context — repository, branch, commit, GitHub org on every trace

### What is NOT captured by this settings file

- Standalone Copilot CLI run directly in a terminal outside VS Code — needs separate env vars on each host:
  ```
  COPILOT_OTEL_ENABLED=true
  OTEL_EXPORTER_OTLP_ENDPOINT=http://<collector-host>:4318
  ```
- Copilot SDK custom applications — needs `TelemetryConfig` with `otlpEndpoint` set in code per SDK language
- JetBrains/IntelliJ/other IDEs — no Copilot OTel support exists regardless of backend (Azure or open-source). Same gap exists with App Insights route — this is a GitHub product limitation, not a pipeline limitation.

### Service name distinction

| Session type | service.name in traces |
|---|---|
| VS Code Copilot Chat | `copilot-chat` |
| VS Code-embedded CLI session | `github-copilot` |
| Claude agent (via VS Code) | `copilot-chat` |

### Three-environment settings files

**OPTION 1 (recommended): Three separate machine-level files pushed via MDM/Intune/GPO**

Push to each environment's device group:

**Dev** — `captureContent: true` allowed for debugging:
```json
{
  "github.copilot.chat.otel.enabled": true,
  "github.copilot.chat.otel.exporterType": "otlp-http",
  "github.copilot.chat.otel.otlpEndpoint": "http://otel-collector.dev.internal:4318",
  "github.copilot.chat.otel.captureContent": true,
  "github.copilot.chat.otel.maxAttributeSizeChars": 4096,
  "github.copilot.chat.otel.dbSpanExporter.enabled": false,
  "github.copilot.chat.otel.outfile": ""
}
```

**Non-prod:**
```json
{
  "github.copilot.chat.otel.enabled": true,
  "github.copilot.chat.otel.exporterType": "otlp-http",
  "github.copilot.chat.otel.otlpEndpoint": "http://otel-collector.nonprod.internal:4318",
  "github.copilot.chat.otel.captureContent": false,
  "github.copilot.chat.otel.maxAttributeSizeChars": 0,
  "github.copilot.chat.otel.dbSpanExporter.enabled": false,
  "github.copilot.chat.otel.outfile": ""
}
```

**Prod:**
```json
{
  "github.copilot.chat.otel.enabled": true,
  "github.copilot.chat.otel.exporterType": "otlp-http",
  "github.copilot.chat.otel.otlpEndpoint": "http://otel-collector.prod.internal:4318",
  "github.copilot.chat.otel.captureContent": false,
  "github.copilot.chat.otel.maxAttributeSizeChars": 0,
  "github.copilot.chat.otel.dbSpanExporter.enabled": false,
  "github.copilot.chat.otel.outfile": ""
}
```

**OPTION 2: Single settings.json + env var override per environment group**

Shared file (same everywhere), env var overrides the endpoint:
```json
{
  "github.copilot.chat.otel.enabled": true,
  "github.copilot.chat.otel.exporterType": "otlp-http",
  "github.copilot.chat.otel.otlpEndpoint": "http://otel-collector.dev.internal:4318",
  "github.copilot.chat.otel.captureContent": false,
  "github.copilot.chat.otel.maxAttributeSizeChars": 0,
  "github.copilot.chat.otel.dbSpanExporter.enabled": false,
  "github.copilot.chat.otel.outfile": ""
}
```

Env vars pushed via MDM per environment group:
```
# Dev
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector.dev.internal:4318
OTEL_RESOURCE_ATTRIBUTES=deployment.environment=dev,team.id=platform
OTEL_SERVICE_NAME=copilot-chat

# Non-prod
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector.nonprod.internal:4318
OTEL_RESOURCE_ATTRIBUTES=deployment.environment=nonprod,team.id=platform
OTEL_SERVICE_NAME=copilot-chat

# Prod
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector.prod.internal:4318
OTEL_RESOURCE_ATTRIBUTES=deployment.environment=prod,team.id=platform
OTEL_SERVICE_NAME=copilot-chat
```

### Deployment instructions per OS

| OS | settings.json location | Env vars mechanism |
|---|---|---|
| Windows | `%APPDATA%\Code\User\settings.json` | Registry `HKLM\SYSTEM\CurrentControlSet\Control\Session Manager\Environment` or Intune device config |
| macOS | `~/Library/Application Support/Code/User/settings.json` | MDM profile + launchd plist (NOT .bashrc/.zshrc — won't reach GUI-launched apps) |
| Linux | `~/.config/Code/User/settings.json` | `/etc/environment` or `systemctl --user set-environment` |

**VS Code must be fully restarted (not just window reload) after any change.**

### Multi-tenant isolation (multiple teams sharing one pipeline)

Add `OTEL_RESOURCE_ATTRIBUTES` per team to tag telemetry before it enters the pipeline. This gives a filterable label in all three backends:
```
OTEL_RESOURCE_ATTRIBUTES=deployment.environment=prod,team.id=<your-team>,department=<your-dept>
```

---

## 6. OTel Collector configs

### Ingest Collector (receives from VS Code, publishes to RedPanda)

```yaml
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318
      grpc:
        endpoint: 0.0.0.0:4317

processors:
  batch:
    timeout: 5s
  memory_limiter:
    check_interval: 1s
    limit_mib: 512
  resource:
    attributes:
      - key: service.name
        value: "copilot-chat"
        action: upsert

exporters:
  kafka/traces:
    brokers: ["redpanda:9092"]
    topic: "otlp_traces"
    encoding: otlp_proto
  kafka/metrics:
    brokers: ["redpanda:9092"]
    topic: "otlp_metrics"
    encoding: otlp_proto
  kafka/logs:
    brokers: ["redpanda:9092"]
    topic: "otlp_logs"
    encoding: otlp_proto

service:
  pipelines:
    traces:
      receivers: [otlp]
      processors: [memory_limiter, resource, batch]
      exporters: [kafka/traces]
    metrics:
      receivers: [otlp]
      processors: [memory_limiter, resource, batch]
      exporters: [kafka/metrics]
    logs:
      receivers: [otlp]
      processors: [memory_limiter, resource, batch]
      exporters: [kafka/logs]
```

### OTel Router (consumes from RedPanda, fans out to backends)

```yaml
receivers:
  kafka/traces:
    brokers: ["redpanda:9092"]
    topic: "otlp_traces"
    encoding: otlp_proto
    group_id: "otel-router-traces"
  kafka/metrics:
    brokers: ["redpanda:9092"]
    topic: "otlp_metrics"
    encoding: otlp_proto
    group_id: "otel-router-metrics"
  kafka/logs:
    brokers: ["redpanda:9092"]
    topic: "otlp_logs"
    encoding: otlp_proto
    group_id: "otel-router-logs"

processors:
  batch:

exporters:
  otlphttp/tempo:
    endpoint: "http://tempo:4318"
  loki:
    endpoint: "http://loki:3100/otlp/v1/logs"
  otlphttp/victoriametrics:
    endpoint: "http://victoria-metrics:8428/opentelemetry"

service:
  pipelines:
    traces:
      receivers: [kafka/traces]
      processors: [batch]
      exporters: [otlphttp/tempo]
    metrics:
      receivers: [kafka/metrics]
      processors: [batch]
      exporters: [otlphttp/victoriametrics]
    logs:
      receivers: [kafka/logs]
      processors: [batch]
      exporters: [loki]
```

**CRITICAL:** Kafka encoding (`otlp_proto`) must match exactly between the ingest Collector (producer) and OTel Router (consumer). Mismatched encoding is the most common failure mode.

---

## 7. Dashboard

A Grafana dashboard JSON has been built: `copilot-otel-dashboard.json`

### Panels included

| Section | Panels |
|---|---|
| How to Use (instructions) | Markdown panel at top — naming convention warning, filter guide, section guide, drill-down instructions, "no data" troubleshooting |
| Agent Summary Statistics | Total Operations, Total Input Tokens, Total Output Tokens, Avg Response Time |
| Chat & Tool Summary | LLM Calls, Chat Sessions, Tool Calls (proxy metric), Avg TTFT, Error Rate (proxy metric) |
| Usage Over Time | Operations Over Time (stacked by type), Token Consumption by Model |
| Model Performance | Model Usage Distribution (donut), Response Duration Avg/P90 (bar), TTFT P50/P90 (bar) |
| Cost & Tokens | Input vs Output Tokens over time, Estimated Cost by Model (update rate constants) |
| Errors | Error Count by type, Copilot Error Logs (Loki live tail) |
| Logs | All Copilot Chat Events (Loki full stream) |
| Trace Drill-Down | Recent Traces table (Tempo), Selected Trace Detail (Tempo) |

### Template variables

- `$model` — filters by `gen_ai.request.model` label
- `$service_name` — filters by `service_name` label

### Two panels flagged PROXY METRIC

- **Tool Calls** (panel 7) — uses `copilot_chat.agent.turn.count` as proxy; no dedicated tool-call counter confirmed in live browser. Verify and replace if a better metric exists.
- **Error Rate / Error Count** (panels 9 and 17) — uses `gen_ai.response.finish_reasons` matching `.*error.*` as proxy; no confirmed `error_type` or success/failure label was found. Verify finish_reasons values in your data.

### Sample PromQL queries (using correct dotted naming)

```promql
# Total input tokens
sum(increase({__name__="gen_ai.client.token.usage_sum", "gen_ai.token.type"="input", service_name=~"$service_name"}[$__range]))

# Total output tokens
sum(increase({__name__="gen_ai.client.token.usage_sum", "gen_ai.token.type"="output", service_name=~"$service_name"}[$__range]))

# Avg response time
sum(rate({__name__="gen_ai.client.operation.duration_sum", service_name=~"$service_name"}[$__range]))
/ sum(rate({__name__="gen_ai.client.operation.duration_count", service_name=~"$service_name"}[$__range]))

# P90 latency by model
histogram_quantile(0.90,
  sum by (le, "gen_ai.request.model") (
    rate({__name__="gen_ai.client.operation.duration_bucket", service_name=~"$service_name"}[$__range])
  )
)

# P50/P90 TTFT
histogram_quantile(0.90,
  sum by (le) (
    rate({__name__="copilot_chat.time_to_first_token_bucket", service_name=~"$service_name"}[$__range])
  )
)

# Template variable population
label_values({__name__="gen_ai.client.token.usage_count"}, "gen_ai.request.model")
label_values({__name__="gen_ai.client.token.usage_count"}, service_name)
```

### Sample LogQL queries

```logql
# All events
{service_name=~"$service_name"}

# Error events only
{service_name=~"$service_name"} |= "error"

# Specific model events
{service_name=~"$service_name"} | json | gen_ai_request_model=~"$model"
```

---

## 8. KQL → PromQL / LogQL / TraceQL quick reference

| Concept | KQL | PromQL |
|---|---|---|
| Filter label | `where x == "y"` | `{label="y"}` |
| Regex filter | `where x matches regex "y.*"` | `{label=~"y.*"}` |
| Sum | `summarize sum(value)` | `sum(metric)` |
| Group by | `summarize ... by field` | `sum by (label) (metric)` |
| P90 | `percentile(value, 90)` | `histogram_quantile(0.90, ...)` |
| Time bucket | `bin(timestamp, 1h)` | `[1h]` range vector |

| App Insights table | Our backend | Query language |
|---|---|---|
| `dependencies` (traces) | Tempo | TraceQL |
| `customMetrics` | VictoriaMetrics | PromQL |
| `traces` (logs) | Loki | LogQL |
| Azure Monitor datasource | Native Tempo/Loki/Prometheus datasources | Direct — no cloud bridge |

---

## 9. Rollout steps (in order)

| Step | What | Owner |
|---|---|---|
| 1 | Push VS Code settings.json + env vars per environment via MDM/Intune/GPO | IT / Endpoint Management |
| 2 | Audit existing OTel Collector + RedPanda + Router configs (Kafka topic names, encoding consistency, correct exporters) | Platform / DevOps |
| 3 | Import dashboard, wire datasources, verify exemplar linking VM → Tempo | Platform / Observability |
| 4 | Pilot with 5–10 developers, validate data appears in all three backends | Platform |
| 5 | Full org-wide MDM rollout + VS Code restart requirement communicated | IT + Platform |

**Verify Step 1 worked:** Check RedPanda topic consumer lag on `otlp_traces` / `otlp_metrics` / `otlp_logs` — throughput spike after restart means clients are sending.

---

## 10. Copilot repo audit prompt

Use this prompt against your OTel pipeline repo to verify Steps 2's configs are correct before going live:

```
Review the OTel pipeline configuration files in this repository, which implement the following architecture:

VS Code (GitHub Copilot, OTel enabled) → OTel Collector (OTLP receiver) → RedPanda (Kafka topics) → OTel Router (Kafka consumer) → fan-out to Tempo (traces), Loki (logs), VictoriaMetrics (metrics) → Grafana

Goal: Once VS Code clients are sending OTLP traces, metrics, and logs to the first OTel Collector, ensure all three signal types flow correctly end-to-end to their respective backends.

Please:
1. Locate and read the config files for both the ingest-side OTel Collector and the OTel Router.
2. Verify the ingest Collector has an OTLP receiver (grpc/http on 4317/4318) and Kafka exporters for traces, metrics, and logs to RedPanda, with consistent topic names and encoding (otlp_proto or otlp_json).
3. Verify the OTel Router has matching Kafka receivers (same topic names, same encoding, same brokers) for all three signal types, with a consumer group_id set per signal.
4. Verify the Router exporters: Traces → Tempo (OTLP), Metrics → VictoriaMetrics (/opentelemetry endpoint), Logs → Loki (/otlp/v1/logs endpoint).
5. Check that pipeline definitions correctly map each receiver to its processors and exporter — no signal type missing or misrouted.
6. Check for encoding/format mismatches between producer (ingest Collector) and consumer (Router) on each Kafka topic.
7. Check that resource attributes (e.g. service.name) are being set or preserved.
8. Confirm Grafana datasource provisioning files point to correct Tempo, Loki, VictoriaMetrics endpoints, with exemplar linking VM → Tempo configured.

Output: a list of specific files with exact line/section references that need to be added, modified, or removed — including corrected YAML snippets — so that Copilot gen_ai.* traces, metrics, and logs flow correctly from RedPanda through to Tempo, Loki, and VictoriaMetrics. Do not propose architectural changes — only fixes/additions needed to make the existing design work.
```

---

## 11. Key facts / gotchas for whoever continues this

1. **Metric names have dots, not underscores** in VictoriaMetrics. Use `{__name__="gen_ai.client.token.usage_sum"}` not `gen_ai_client_token_usage_sum`. This was the root cause of the first dashboard showing no data.
2. **Token type is a label, not two separate metrics.** Input vs output tokens are distinguished by `"gen_ai.token.type"="input"` / `"output"` on the same `gen_ai.client.token.usage_*` metric family.
3. **TTFT metric is `copilot_chat.time_to_first_token_*`**, not `gen_ai.server.time_to_first_token_*`. The Copilot-specific namespace exists alongside the GenAI semantic convention namespace.
4. **Loki's `service_name` must be a label** (not just a JSON field in the log body) for `{service_name=~"..."}` filter syntax to work. If it's only in the body, use `| json | service_name=~"..."` instead.
5. **CLI traces appear under `service.name=github-copilot`**, extension/chat traces under `service.name=copilot-chat`. The `$service_name` dashboard filter must include both or be set to All.
6. **JetBrains/IntelliJ has no Copilot OTel support** — same gap exists on Azure Monitor route. Not a pipeline limitation.
7. **Dynatrace export path was descoped** — ignore crossed-out path in the original architecture diagram.
8. **Three environments (dev/nonprod/prod)** each need their own `otlpEndpoint` value. Use MDM device groups to push different settings files, or push one file + env var override per group. Also push `OTEL_RESOURCE_ATTRIBUTES=deployment.environment=<env>` so Grafana can filter by environment.
9. **`captureContent: false` is the safe default for prod and nonprod.** Only enable in dev/trusted environments — it captures full prompt text, file contents, tool arguments.
10. **Standalone Copilot CLI** (run directly in a terminal, not via VS Code "New Copilot CLI Session") needs its own `COPILOT_OTEL_ENABLED=true` + `OTEL_EXPORTER_OTLP_ENDPOINT` env vars set wherever it runs (dev laptops, CI/CD runners).
11. **The cost panel in the dashboard uses placeholder pricing** ($3/M input, $15/M output). Update the multipliers in panel 16's PromQL expression to actual contract rates before using for budgeting.
12. **Tool Calls and Error Rate panels are PROXY METRICS** — verify against live data and replace if better metrics are confirmed in the Metrics Browser.

---

## 12. Files produced in this project

| File | Description |
|---|---|
| `copilot-otel-dashboard.json` | Grafana dashboard JSON — import via Grafana UI → Dashboards → Import |
| `KQL_to_PromQL_LogQL_TraceQL_Reference.md` | Query migration reference, naming conventions, verification checklist |
| `vscode-settings-otel.json` | Complete VS Code settings.json (single-environment template) |
| `Copilot_OTel_OpenSource_Architecture.pptx` | Slide deck for manager/leadership review |
| `PROJECT_HANDOFF_Copilot_OTel_Pipeline.md` | This file |

---

## 13. Source references

- VS Code OTel monitoring docs: https://code.visualstudio.com/docs/agents/guides/monitoring-agents
- GitHub Copilot CLI OTel reference: https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-command-reference
- GitHub Copilot SDK OTel: https://docs.github.com/en/copilot/how-tos/copilot-sdk/observability/opentelemetry
- OTel GenAI semantic conventions: https://opentelemetry.io/docs/specs/semconv/gen-ai/
- VictoriaMetrics OTLP ingestion: https://docs.victoriametrics.com/#opentelemetry
- Grafana Tempo OTLP: https://grafana.com/docs/tempo/latest/
- Grafana Loki OTLP: https://grafana.com/docs/loki/latest/send-data/otel/
- Original Grafana Copilot dashboard (Azure Monitor): https://grafana.com/grafana/dashboards/25053-github-copilot/
- OTel Collector Contrib Kafka exporter/receiver: https://github.com/open-telemetry/opentelemetry-collector-contrib
