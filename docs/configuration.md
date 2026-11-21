# Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `PRODUCT_CATALOG_SERVICE_ADDR` | **Yes** | — | `host:port` (no scheme) of product-catalog gRPC. **The process exits at startup with `RuntimeError` if unset** |
| `PORT` | No | `8080` | gRPC listen port |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | No | unset (telemetry off) | OTLP/**gRPC** endpoint for **traces, metrics and logs**: an absolute `http(s)://host:port` URL, e.g. `http://alloy.monitoring.svc.cluster.local:4317`. Telemetry is on only when this is a valid URL; empty or malformed means off (never a crash). The old `COLLECTOR_SERVICE_ADDR` and `ENABLE_TRACING` are no longer read |
| `OTEL_SDK_DISABLED` | No | — | `true` (trimmed, any case) forces everything off: telemetry and profiling, even with an endpoint set |
| `OTEL_METRIC_EXPORT_INTERVAL` | No | `15000` | Milliseconds between metric pushes |
| `OTEL_SERVICE_NAME` | No | `recommendation-service` | `service.name` — becomes the Prometheus `job` and the Loki `service_name`; keep the default, dashboards depend on it |
| `SERVICE_VERSION` | No | `dev` | `service.version` resource attribute, the `version` log field and the Pyroscope `version` tag |
| `SERVICE_COMMIT` | No | `unknown` | Commit id, logged once at start (`message: "starting"`); set it at deploy time |
| `DEPLOYMENT_ENVIRONMENT` | No | `local` | `deployment.environment` resource attribute and the Pyroscope `environment` tag |
| `LOG_LEVEL` | No | `info` | `debug`, `info`, `warning` (or `warn`) or `error`, case-insensitive; an unknown value means `info` |
| `PYROSCOPE_ADDR` | No | unset (profiling off) | Profiles use the Pyroscope SDK push protocol (**not** OTLP): an absolute `http(s)://` URL, e.g. `http://pyroscope:4040`, or Alloy. There is no default address; profiling is on only when this is a valid URL. Tags: `service_name`, `version`, `environment` |
| `ENABLE_PROFILING` | No | — | `0` forces profiling off even with `PYROSCOPE_ADDR` set; `1` alone starts nothing |

Notes:

- The catalog channel is created with `grpc.insecure_channel`, which connects lazily — the service starts fine even if the catalog is down; individual RPCs then fail and the service answers from the random fallback. Every catalog call, the random fallback included, has the deadline `CATALOG_TIMEOUT_SECONDS` (default 2 s).
- Telemetry is **pushed**, not scraped: traces, metrics and logs share one OTLP/gRPC endpoint (Alloy); profiles go to Pyroscope (via Alloy). `service.instance.id` is the pod hostname, so replicas stay distinct in Mimir.
- The former Prometheus endpoint (`METRICS_PORT`) no longer exists. Logs go to stdout as JSON lines and, with telemetry on, are additionally exported over OTLP with trace ids.
- With every telemetry variable unset the service starts with telemetry and profiling off: the OTel SDK and exporters are not even imported, the RPC metric instruments are API no-ops, and the startup lines are `Connecting to productcatalog at …` and `{"message":"starting", ... "telemetry":"telemetry off, profiling off"}` (the lifecycle test runs the real server and checks them; not re-checked in the image).
- With an endpoint set but no reachable collector, the exporters log retry errors to stderr and the service keeps serving.
