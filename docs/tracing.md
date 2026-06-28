# Distributed Tracing — Architecture & Incident Report

## Overview

The VPC Creator API emits distributed traces using [OpenTelemetry](https://opentelemetry.io/), exports them to [Jaeger](https://www.jaegertracing.io/) via gRPC OTLP, and surfaces them in the Grafana dashboard.

```
vpc-creator-api
  └─ OTel SDK (TracerProvider)
       └─ BatchSpanProcessor
            └─ OTLPSpanExporter (gRPC → jaeger:4317)
                  └─ Jaeger all-in-one
                        └─ Grafana Jaeger datasource → "Traces" panel
```

---

## What Changed and Why

Three separate bugs prevented traces from appearing in the Grafana dashboard. They were discovered incrementally by querying Jaeger and Grafana directly.

---

### Bug 1 — Grafana panel type `"traces"` incompatible with Jaeger search results

**File:** `grafana/provisioning/dashboards/vpc-view.json`

**Symptom:** Traces panel showed "No data found in response" regardless of what was in Jaeger.

**Root cause:** Grafana's `"type": "traces"` panel is designed to render a single trace's **span waterfall** — the full tree of spans for one trace ID. When it receives the Jaeger search result format (a table of `traceID | traceName | startTime | duration` rows), it cannot render it and shows "No data found."

This was confirmed by querying the Grafana datasource API directly. The Jaeger search response returns:
```json
"meta": { "preferredVisualisationType": "table" }
```

The `traces` panel type ignores `table` data. Switching to `"type": "table"` renders the same data correctly, with each Trace ID showing a clickable deep-link into the Jaeger UI.

**Fix:**
```json
// before
{ "type": "traces", "options": { "traceQuery": { ... }, "spanBar": { ... } } }

// after
{ "type": "table", "options": { "showHeader": true } }
```

**Options considered:**

| Option | Pros | Cons |
|---|---|---|
| `type: "table"` ✓ | Works with Jaeger search results; rows are clickable links to traces | Shows list, not span waterfall |
| `type: "traces"` with a specific trace ID | Renders a single span waterfall | Can only show one trace; not useful for a summary panel |
| Grafana Explore (not a dashboard panel) | Native trace search UI | Not embeddable in a dashboard |
| `grafana-exploretraces-app` plugin panel | Rich trace exploration | Requires plugin install; adds complexity |

---

### Bug 2 — Grafana panel missing `queryType: "search"`

**File:** `grafana/provisioning/dashboards/vpc-view.json`

**Symptom:** "No data found in response" on the Traces panel (would also affect the table panel without this field).

**Root cause:** The Grafana Jaeger datasource has two query modes:
- **Trace by ID** (default) — looks up a single trace given an exact trace ID.
- **Search** — queries traces by service name, operation, tags, duration, etc.

Without `queryType: "search"`, Grafana defaults to trace-by-ID mode. Because no `query` (trace ID) was provided, Jaeger returned nothing.

**Fix:**
```json
// before
{
  "datasource": { "type": "jaeger", "uid": "Jaeger" },
  "service": "vpc-creator-api",
  "refId": "A"
}

// after
{
  "datasource": { "type": "jaeger", "uid": "Jaeger" },
  "queryType": "search",
  "service": "vpc-creator-api",
  "limit": 100,
  "refId": "A"
}
```

**Options considered:**

| Option | Pros | Cons |
|---|---|---|
| Add `queryType: "search"` | Correct fix, minimal change | None |
| Switch to Grafana Explore instead of a panel | Works out of the box | Not embeddable in a dashboard |
| Use a `table` panel with Jaeger query | Also works | Less purpose-built UX for traces |

---

### Bug 2 — `/metrics` scrapes flooded Jaeger, burying real traces

**File:** `otel.py`

**Symptom:** Even after fixing Bug 1, the Traces panel showed only `GET /metrics` spans and no VPC operations.

**Root cause:** Prometheus scrapes `/metrics` every 5 seconds. `FastAPIInstrumentor` was instrumenting **every** endpoint, including `/metrics`. This created ~12 traces per minute purely from health/metrics polling. With `limit: 20`, all 20 returned traces were `GET /metrics` spans. VPC operations — which happen infrequently — were pushed below the limit and never shown.

Confirmed by querying Jaeger directly with time bounds:

```
# Returned 20 traces, all GET /metrics
GET /api/traces?service=vpc-creator-api&limit=20&start=<now-1h>&end=<now>
```

**Fix:** Exclude polling endpoints from instrumentation:

```python
# otel.py — before
FastAPIInstrumentor.instrument_app(app)

# after
FastAPIInstrumentor.instrument_app(app, excluded_urls="metrics,health")
```

`excluded_urls` accepts a comma-separated list of URL path substrings. Any request whose path contains one of those substrings is not instrumented.

**Options considered:**

| Option | Pros | Cons |
|---|---|---|
| `excluded_urls="metrics,health"` ✓ | Clean, no trace noise, standard practice | Prometheus scrape loses trace context (acceptable) |
| Increase `limit` to 1000+ | Simple dashboard change | Does not fix noise; still mixes signal with polling traffic |
| Add an operation filter in the Grafana panel | Hides noise at query time | Filtering in Grafana, not at source; Jaeger still stores junk |
| Use a sampler (e.g. `ParentBased(TraceIdRatioBased(0.1))`) | Reduces overall volume | Probabilistic; can drop real traces |
| Deploy an OTel Collector with a filter processor | Production-grade solution | Adds infra complexity for a dev stack |

`excluded_urls` at the source is the right default. In production, an OTel Collector pipeline with a `filter` processor is preferred because it centralises filtering without touching application code.

---

### Bug 3 — Explicit datasource UIDs crashed Grafana on startup

**File:** `grafana/provisioning/datasources/datasource.yaml`

**What happened:** During debugging, explicit `uid:` fields were added to all three datasources to ensure the dashboard's `uid` references matched. This caused Grafana to crash on startup with:

```
Datasource provisioning error: data source not found
```

**Root cause:** Grafana's provisioning system uses `name` (not `uid`) as the idempotency key for existing datasources. When explicit UIDs were added, Grafana tried to create or update datasources with those UIDs but found conflicting records already stored in the SQLite database (with auto-generated UIDs). This produced an unrecoverable state on startup.

**Resolution:** The explicit `uid:` fields were removed. The dashboard's datasource references (`uid: "Prometheus"`, `uid: "Loki"`, `uid: "Jaeger"`) are resolved by Grafana falling back to a name-based lookup when the UID is not found — which is why Prometheus and Loki panels worked without explicit UIDs.

The one safe fix in `datasource.yaml` was correcting a case mismatch in `tracesToLogsV2`:
```yaml
# before (lowercase, did not match the datasource name)
tracesToLogsV2:
  datasourceUid: loki

# after
tracesToLogsV2:
  datasourceUid: Loki
```

---

## Trace Topology

After the fixes, a `POST /vpcs` call produces the following span tree in Jaeger:

```
POST /vpcs  (FastAPIInstrumentor — server span)
├─ POST /vpcs http receive   (internal)
├─ vpc.create                (manual span in create_vpc handler)
│   └─ build-vpc             (manual span in _build_vpc)
└─ POST /vpcs http send      (internal)
```

Manual spans are added in `main.py` using:

```python
tracer = trace.get_tracer(__name__)

with tracer.start_as_current_span("vpc.create") as span:
    span.set_attribute("vpc.cidr", body.cidr)
    span.set_attribute("vpc.region", body.region)
    span.set_attribute("enduser.id", user)
```

---

## Known Limitation — `build-vpc` span exits before AWS calls

In `_build_vpc`, the `with tracer.start_as_current_span("build-vpc")` block currently closes before the AWS boto3 calls execute:

```python
def _build_vpc(body: VPCCreate) -> dict:
    with tracer.start_as_current_span("build-vpc") as span:
        span.set_attribute("aws.region", body.region)
        span.set_attribute("vpc.cidr", body.cidr)
        span.set_attribute("subnet.count", len(body.subnets))
    # ← span ends here, before any AWS calls
    ec2 = boto3.Session().client(...)
```

This means the `build-vpc` span has near-zero duration and does not wrap the actual AWS latency. This is a pre-existing code issue, not introduced by the observability work.

---

## Stack Components

| Component | Role | Port |
|---|---|---|
| `vpc-creator-api` | Emits OTLP spans via gRPC | — |
| Jaeger all-in-one | Receives, stores, and serves traces | 4317 (gRPC), 16686 (UI/API) |
| Grafana Jaeger datasource | Queries Jaeger HTTP API | — |
| Grafana `table` panel | Shows Jaeger search results; Trace ID cells link to full trace | — |
