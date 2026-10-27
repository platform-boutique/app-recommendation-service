# recommendation-service

A gRPC service that returns product recommendations for the platform-demo e-commerce platform. For each request it fetches the **entire** product list from `product-catalog-service`, removes the products the user already has, and returns a random sample of up to 5 product IDs. It is stateless and is called only by `frontend`.

Part of the [platform-demo](https://github.com/platform-boutique) platform — microservices, observability (LGTM stack), GitOps (Argo CD), policy enforcement (Kyverno), infrastructure provisioning (Terraform) and an internal developer portal (Backstage). See the master `README.md` in the platform-demo root for the whole-system view.

## At a glance

| | |
|---|---|
| **Language / runtime** | Python 3.12 in the image (3.13 pinned for local dev), `grpcio` |
| **Image** | `mladenovskistefan/recommendation-service` |
| **Protocol / port** | gRPC (HTTP/2, plaintext) on `8080` |
| **Metrics** | Pushed over OTLP/gRPC to Alloy → Mimir every 15 s (no scrape endpoint). Dashboard names: `rpc_server_duration_seconds_{bucket,count,sum}`, `rpc_server_requests_total`, `rpc_server_active_requests` |
| **Health** | `grpc.health.v1.Health/Check` on `8080` — static `SERVING`, does **not** reflect catalog availability |
| **State** | None |
| **Depends on** | `product-catalog-service:3550` (`ListProducts`) |
| **Called by** | `frontend` (`ListRecommendations`) |
| **gRPC reflection** | Not enabled — pass `-proto` to `grpcurl` |

## API

Package `hipstershop`, service `RecommendationService` ([`proto/recommendation.proto`](proto/recommendation.proto)); health in [`proto/health.proto`](proto/health.proto). The proto also embeds the client definition of `ProductCatalogService.ListProducts`.

| Method | Request | Response | Behaviour |
|---|---|---|---|
| `ListRecommendations` | `{ string user_id, repeated string product_ids }` | `{ repeated string product_ids }` | `candidates = all catalog IDs − request.product_ids`; returns `min(5, len(candidates))` random picks. `user_id` is only logged. If the catalog call fails → gRPC `INTERNAL` ("Failed to fetch product catalog.") |

The frontend then keeps at most 4 of the returned IDs and fetches each product from the catalog.

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `PRODUCT_CATALOG_SERVICE_ADDR` | **Yes** | — | `host:port` (no scheme) of product-catalog gRPC. **The process exits at startup with `RuntimeError` if unset** |
| `PORT` | No | `8080` | gRPC listen port |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | No | unset (telemetry off) | OTLP/**gRPC** endpoint for **traces, metrics and logs** (scheme required), e.g. `http://alloy.monitoring.svc.cluster.local:4317`. Legacy fallback: `COLLECTOR_SERVICE_ADDR` (`host:port`). Telemetry is on only when one of them is set and non-empty |
| `OTEL_SDK_DISABLED` | No | — | `true` forces telemetry off even with an endpoint set (the legacy `ENABLE_TRACING=0` does the same) |
| `OTEL_METRIC_EXPORT_INTERVAL` | No | `15000` | Milliseconds between metric pushes |
| `OTEL_SERVICE_NAME` | No | `recommendation-service` | `service.name` — becomes the Prometheus `job` and the Loki `service_name`; keep the default, dashboards depend on it |
| `SERVICE_VERSION` | No | `1.0.0` | `service.version` resource attribute |
| `PYROSCOPE_ADDR` | No | unset (profiling off) | Profiles use the Pyroscope SDK push protocol (**not** OTLP). Point it at Alloy (`http://alloy.monitoring.svc.cluster.local:4040`), which forwards to Pyroscope, or directly at Pyroscope. Profiling is on only when this is set and non-empty |
| `ENABLE_PROFILING` | No | — | `0` forces profiling off even with `PYROSCOPE_ADDR` set; any other value (or unset) leaves the decision to `PYROSCOPE_ADDR` |

Notes:

- The catalog channel is created with `grpc.insecure_channel`, which connects lazily — the service starts fine even if the catalog is down; individual RPCs then fail. No per-call deadline is set.
- Telemetry is **pushed**, not scraped: traces, metrics and logs share one OTLP/gRPC endpoint (Alloy); profiles go to Pyroscope (via Alloy). `service.instance.id` is the pod hostname, so replicas stay distinct in Mimir.
- The former Prometheus endpoint (`METRICS_PORT`) no longer exists. Logs go to stdout as JSON lines and, with telemetry on, are additionally exported over OTLP with trace ids.
- With every telemetry variable unset the service starts with telemetry and profiling off: the OTel SDK and exporters are not even imported, the RPC metric instruments are API no-ops, and the startup lines are `Connecting to productcatalog at …` and `RecommendationService gRPC server started on port 8080 (telemetry off, profiling off)` (verified from source and in the image).
- With an endpoint set but no reachable collector, the exporters log retry errors to stderr and the service keeps serving.

## Container image

Multi-stage [`Dockerfile`](Dockerfile):

| Stage | Base | What happens |
|---|---|---|
| builder | `python:3.12-alpine` | installs `g++`/`linux-headers`, `pip install -r requirements.txt` (fully pinned), **compiles the protos** with `grpc_tools.protoc` into `generated/` and patches the relative import in `recommendation_pb2_grpc.py` |
| runtime | `python:3.12-alpine` | copies site-packages, generated stubs (`/app/src/generated`), `src/`; runs as non-root |

- **Entrypoint:** `python src/server.py` (working dir `/app`).
- **User:** numeric `USER 100:101` (`appuser`/`appgroup`, created with these fixed ids; `id` in the container prints `uid=100(appuser) gid=101(appgroup)`). Matches the chart's `runAsUser: 100` / `runAsGroup: 101`.
- **EXPOSE:** `8080` only.
- **Docker `HEALTHCHECK`:** Python `grpc.channel_ready_future` against `localhost:${PORT:-8080}`.
- **Image size:** 175 MB uncompressed (`docker images`, local build on 2026-10-27; the earlier ~41 MB figure was not re-measured and is probably the compressed registry size).
- **Reproducibility:** the image installs from the fully pinned `requirements.txt` (compiled from `requirements.in` with `uv`), so transitive versions no longer float between builds. The lock was not regenerated in this change.
- **Build context:** [`.dockerignore`](.dockerignore) keeps `.git`, tests, docs, caches and locally compiled stubs out of the context.
- The generated stubs are not in git; they only exist after the Docker build.

## Kubernetes deployment notes

**Workload:** stateless `Deployment`; scale freely.

**Ports to declare**

| Name | Port | Protocol | Expose via Service? |
|---|---|---|---|
| `grpc` | `8080` | TCP (HTTP/2) | Yes — `frontend` dials `recommendation-service:8080` |

**Service DNS name expected by callers:** `recommendation-service` (`frontend`: `RECOMMENDATION_SERVICE_ADDR=recommendation-service:8080`). `email-service` also listens on `8080` — different Service, no clash.

**Startup dependency:** `product-catalog-service` is *not* needed to start, but is needed to serve. No init container required; readiness stays green even when the catalog is down, so consider an app-level readiness check if you want the pod pulled from endpoints during catalog outages (not currently implemented).

**Probes**

```yaml
readinessProbe:
  grpc: { port: 8080 }
  initialDelaySeconds: 3
  periodSeconds: 5
livenessProbe:
  grpc: { port: 8080 }
  periodSeconds: 10
  failureThreshold: 3
```

**Graceful shutdown:** `SIGTERM` (and `SIGINT`) is handled even as PID 1: the server stops accepting new RPCs, gives in-flight ones up to 2 s (`server.stop(2)`), flushes telemetry when it is on (bounded at 2 s, also when the collector is unreachable) and exits `0`. The worst case (4 s) fits the chart's `terminationGracePeriodSeconds: 5`. Measured: `docker stop` on the idle container returned in 0.29 s with exit code 0; running from source against a stub OTLP receiver, the final traces, metrics and logs batches arrived after `SIGTERM` and the process exited 0 (a test covers the unreachable-collector bound).

**Security context**

```yaml
securityContext:
  runAsNonRoot: true
  runAsUser: 100
  runAsGroup: 101
  allowPrivilegeEscalation: false
  capabilities: { drop: ["ALL"] }
  readOnlyRootFilesystem: true   # verified in-cluster; no writable /tmp was needed
```

**Resources** — requests sized from measured idle usage in the test stack (see the chart's `values.yaml` for limits):

```yaml
resources:
  requests: { cpu: 100m, memory: 64Mi }
  limits:   { cpu: "500m", memory: 192Mi }
```

**Config / secrets:** none.

**Telemetry:** pushed, not scraped — OTLP/gRPC to Alloy (`global.monitoring.otlpEndpoint`), profiles to Alloy `:4040`. Metrics carry `job="recommendation-service"` and `instance=<pod>`; no scrape annotations or `ServiceMonitor` are needed.

**Helm chart:** [`platform-demo-helm/recommendation-service`](https://github.com/platform-boutique/platform-demo-helm/tree/main/recommendation-service) (built on the shared `platform-common` library chart). It ships with HA defaults — 2 replicas,
HPA, PodDisruptionBudget, topology spread, NetworkPolicy, restricted pod security context, probes and the full env contract (see Configuration) —
and was verified in the earlier local phase on a 3-node kind cluster (since deleted) under the `restricted` Pod Security Standard; it has not been verified on EKS yet.

**Deployment and admission policies:** Argo CD deploys the chart from `platform-demo-helm` (`envs/<env>/values/recommendation-service.yaml` pins the image digest; promotion is a pull request). In the cluster Kyverno enforces, for this service: pods named `app.kubernetes.io/name: recommendation-service` that run under the ServiceAccount of the same name (Cilium derives the network identity from that label), an image from this repository pinned by digest and signed by our release pipeline, a read-only root filesystem, resource requests and a memory limit, readiness and liveness probes and no API token. The Helm CI evaluates the rendered chart against these policies before merge; see [`platform-demo-helm/policies`](https://github.com/platform-boutique/platform-demo-helm/tree/main/policies).

## Observability

| Signal | Implementation | Destination |
|---|---|---|
| **Traces** | OpenTelemetry SDK, `GrpcInstrumentorServer` and `GrpcInstrumentorClient` (so the catalog call is a child span), OTLP/gRPC exporter (`BatchSpanProcessor`); only when telemetry is on | Alloy `:4317` → Tempo |
| **Metrics** | Hand-rolled RPC instruments, `PeriodicExportingMetricReader` over OTLP/gRPC every 15 s (replaces the Prometheus reader) | Alloy → Mimir |
| **Logs** | Python `logging` with `python-json-logger` to stdout (always) **and** an OTel `LoggingHandler` (OTLP/gRPC, with trace ids; only when telemetry is on) | Alloy → Loki |
| **Profiles** | `pyroscope-io`, tagged with `version`; only when profiling is on | Alloy `:4040` → Pyroscope |

Metric names: `rpc_server_duration_seconds_{bucket,count,sum}` (buckets 1 ms … 10 s; the unit `s` yields `_seconds`), `rpc_server_requests_total`, `rpc_server_active_requests` (labels `rpc_system`, `rpc_service`, `rpc_method`, `rpc_grpc_status_code`). Each stdout line is one valid JSON object with the keys `timestamp` (epoch seconds), `severity`, `name`, `message` (quotes and newlines, e.g. in gRPC error texts, are escaped; a test asserts this).

Every series carries `job="recommendation-service"` (from `service.name`) and `instance=<pod name>` (`service.instance.id`), which is what the Grafana dashboards select on. Logs land in Loki with `service_name="recommendation-service"` and trace/span ids, so a log line links to its trace.

## Running locally

Requirements: Python 3.13 (see `.python-version`; 3.12 also works), Docker, a running `product-catalog-service` (or any gRPC stub on the configured address), [`grpcurl`](https://github.com/fullstorydev/grpcurl).

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt

# generate the gRPC stubs (what the Dockerfile does) — required before running from source
mkdir -p src/generated && python -m grpc_tools.protoc -I proto \
  --python_out=src/generated --grpc_python_out=src/generated proto/recommendation.proto
touch src/generated/__init__.py
sed -i 's/^import recommendation_pb2 as recommendation__pb2/from . import recommendation_pb2 as recommendation__pb2/' src/generated/recommendation_pb2_grpc.py

PRODUCT_CATALOG_SERVICE_ADDR=localhost:3550 python src/server.py
pytest                        # tests (pytest.ini sets pythonpath=src)
# tests/test_lifecycle.py compiles the proto into a temp dir itself (needs grpcio-tools from requirements.txt), starts
# the real server with every telemetry variable unset, checks the JSON log lines and that SIGTERM exits 0

docker build -t recommendation-service .
docker run --rm -p 8081:8080 \
  -e PRODUCT_CATALOG_SERVICE_ADDR=host.docker.internal:3550 recommendation-service
```

### Manual gRPC testing (no reflection — pass the proto)

```bash
grpcurl -plaintext -proto proto/recommendation.proto \
  -d '{"user_id": "u1", "product_ids": ["OLJCESPC7Z", "66VCHSJNUP"]}' \
  localhost:8080 hipstershop.RecommendationService/ListRecommendations

grpcurl -plaintext -proto proto/health.proto localhost:8080 grpc.health.v1.Health/Check
```

## Project structure

```
├── src/
│   ├── server.py        # gRPC server (10-thread pool), RecommendationService + Health servicers, catalog client stub, JSON logs, SIGTERM
│   ├── telemetry.py     # telemetry contract, OTel traces/metrics/logs (server + client instrumentation), Pyroscope, bounded shutdown; imported first
│   ├── generated/       # protoc output (git: only __init__.py; real stubs are built in Docker)
│   └── __init__.py
├── proto/               # recommendation.proto, health.proto
├── requirements.in / requirements.txt / requirements-dev.txt
├── pyproject.toml / pytest.ini / .python-version / .trivyignore
├── Dockerfile / .dockerignore
└── docs/                # empty placeholder
```

## Known limitations

- Fetches the whole catalog on every request (fine for 9 products; will not scale to large catalogs).
- Recommendations are random — no ranking, no use of `user_id`.
- Health ignores catalog availability; no deadline on the catalog call.
- Health stays `SERVING` during the 2 s shutdown drain (it is static).
- `requirements.txt` was compiled for Python 3.13 (`--python-version 3.13`) while the image runs 3.12; it installs and runs there (verified by the image build and the local test run on Python 3.12), but the lock was not recompiled for 3.12.
