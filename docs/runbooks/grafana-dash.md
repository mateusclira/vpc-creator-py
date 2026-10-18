# Grafana Dashboards as Code

This project provisions Grafana dashboards automatically at container startup — no manual clicking required.

---

## How it works

Grafana supports **provisioning** via YAML + JSON files mounted into the container. On startup, Grafana reads these paths:

| Path (inside container) | Purpose |
|---|---|
| `/etc/grafana/provisioning/datasources/` | Datasource definitions |
| `/etc/grafana/provisioning/dashboards/` | Dashboard provider config + JSON files |

Both are served by the bind mount in `docker-compose.yaml`:

```yaml
volumes:
  - ./grafana/provisioning:/etc/grafana/provisioning
```

---

## File structure

```
grafana/
└── provisioning/
    ├── datasources/
    │   └── datasource.yaml      # Prometheus, Loki, Jaeger
    └── dashboards/
        ├── dashboards.yaml      # tells Grafana where to find JSON files
        └── vpc-view.json        # the VPC View dashboard
```

### `dashboards.yaml` — the provider

```yaml
apiVersion: 1
providers:
  - name: default
    type: file
    disableDeletion: false
    updateIntervalSeconds: 30   # re-reads JSON every 30 s
    allowUiUpdates: true        # lets you edit in the UI (changes write-back to disk)
    options:
      path: /etc/grafana/provisioning/dashboards
```

`updateIntervalSeconds: 30` means any change you save to `vpc-view.json` is reflected live in Grafana within 30 seconds without a restart.

---

## Adding a new dashboard

1. Export the dashboard from Grafana UI:
   - Open the dashboard → **Share** (top-right) → **Export** → **Save to file**.
   - Remove the `"id"` field (or set it to `null`) from the JSON so Grafana generates a new ID on import.
   - Set a stable `"uid"` string (e.g. `"my-new-dash"`) — this is what Grafana uses to identify the dashboard across restarts.

2. Drop the JSON file into `grafana/provisioning/dashboards/`:
   ```
   grafana/provisioning/dashboards/my-new-dash.json
   ```

3. Grafana will pick it up automatically within 30 seconds (or immediately on next `docker compose up`).

4. Commit the JSON file to version control — your dashboard is now reproducible on any machine.

---

## Updating an existing dashboard

Edit `grafana/provisioning/dashboards/vpc-view.json` directly. Grafana reloads it every `updateIntervalSeconds` seconds. To force an immediate reload:

```bash
docker compose restart grafana
```

---

## VPC View dashboard panels

| Panel | Type | Data source | What it shows |
|---|---|---|---|
| Current VPCs | Stat (green) | Prometheus | Live count from `current_vpcs` gauge |
| VPCs Created | Stat (green) | Prometheus | Cumulative total from `vpcs_created_total` |
| VPCs Deleted | Stat (red) | Prometheus | Cumulative total from `vpcs_deleted_total` |
| VPC Activity | Time series | Prometheus | Rate of create / delete / AWS errors over 5 m windows |
| Application Logs | Logs | Loki | OTel log records shipped via OTLP (`{service_name="vpc-creator-api"}`) |
| Traces | Traces | Jaeger | All traces from the `vpc-creator-api` service |

---

## Datasources

All three datasources are provisioned automatically via `datasources/datasource.yaml`:

| Name | Type | Internal URL |
|---|---|---|
| Prometheus | prometheus | `http://prometheus:9090` |
| Loki | loki | `http://loki:3100` |
| Jaeger | jaeger | `http://jaeger:16686` |

The Jaeger datasource is configured with **Traces to Logs** correlation: clicking a span in the Traces panel opens the matching Loki logs filtered by `traceID` and time window automatically.

> **Can I use Grafana for everything instead of opening Jaeger UI separately?**
> Yes. Grafana 10+ has a native Jaeger datasource with full trace timeline rendering. The Jaeger UI at `http://localhost:16686` is still available but you don't need it — Grafana's **Explore** view and the **Traces** dashboard panel cover the same functionality and also let you correlate traces ↔ logs in one place.

---

## Useful Explore queries

Open **Grafana → Explore** to run ad-hoc queries:

**Logs (Loki)**
```logql
{service_name="vpc-creator-api"} |= "ERROR"
```

```logql
{service_name="vpc-creator-api"} | json | line_format "{{.message}}"
```

**Traces (Jaeger)**
- Service: `vpc-creator-api`
- Operation: `vpc.create` / `vpc.delete` / `vpc.list` / `vpc.get`

**Metrics (Prometheus)**
```promql
rate(vpcs_created_total[5m])
```

```promql
increase(aws_errors_total[1h])
```

---

## Applying dashboards to a fresh environment

```bash
# 1. Clone the repo
git clone <repo-url> && cd vpc-creator-py

# 2. Copy and fill in credentials
cp .env.example .env
# edit .env with real values

# 3. Start the stack — dashboards and datasources are provisioned automatically
docker compose up --build -d

# 4. Open Grafana
open http://localhost:3000   # admin / admin (change on first login)
```

The VPC View dashboard will be available under **Dashboards** immediately after the stack is healthy.
