# recommendation-service

A gRPC service that returns product recommendations for the platform-demo e-commerce platform. For each request it looks at the products the visitor is viewing, takes their categories (the catalog's departments), and returns up to 5 other products of those categories ranked by rating, then review count; with no usable input it returns the top-rated products of the whole shop. If the catalog cannot answer it falls back to random picks. It is stateless and is called only by `frontend`.

Part of the [platform-demo](https://github.com/platform-boutique) platform — microservices, observability (LGTM stack), GitOps (Argo CD), policy enforcement (Kyverno), infrastructure provisioning (Terraform) and an internal developer portal (Backstage). See the master `README.md` in the platform-demo root for the whole-system view.

## At a glance

| | |
|---|---|
| **Language / runtime** | Python 3.12 in the image (3.13 pinned for local dev), `grpcio` |
| **Image** | `mladenovskistefan/recommendation-service` |
| **Protocol / port** | gRPC (HTTP/2, plaintext) on `8080` |
| **Metrics** | Pushed over OTLP/gRPC to Alloy → Mimir every 15 s (no scrape endpoint). Dashboard names: `rpc_server_duration_seconds_{bucket,count,sum}`, `rpc_server_requests_total`, `rpc_server_active_requests`, `build_info`, `python_*` runtime metrics |
| **Health** | `grpc.health.v1.Health/Check` on `8080` — static `SERVING`, does **not** reflect catalog availability |
| **State** | None |
| **Depends on** | `product-catalog-service:3550` (`GetProduct`, `ListProductsFiltered`; `ListProducts` only for the random fallback) |
| **Called by** | `frontend` (`ListRecommendations`) |
| **gRPC reflection** | Enabled (`grpcio-reflection`, v1alpha): `grpcurl -plaintext localhost:8080 list` works without the proto files |

## API

Package `hipstershop`, service `RecommendationService` ([`proto/recommendation.proto`](proto/recommendation.proto)); health in [`proto/health.proto`](proto/health.proto). The proto also embeds the client definition of the `ProductCatalogService` calls this service makes (`ListProducts`, `GetProduct`, `ListProductsFiltered`); it is a copy of the catalog's proto with identical field numbers, additive changes only.

| Method | Request | Response | Behaviour |
|---|---|---|---|
| `ListRecommendations` | `{ string user_id, repeated string product_ids }` | `{ repeated string product_ids }` | Ranked recommendations, at most 5, see [Recommendation logic](#recommendation-logic). `user_id` is only logged. Catalog trouble does not reach the caller: the service falls back to random picks (below). Only if that fallback also fails → gRPC `INTERNAL` ("Failed to fetch product catalog.") |

The frontend then keeps at most 4 of the returned IDs and fetches each product from the catalog.

### Recommendation logic

`product_ids` are the products the visitor is looking at (the viewed product and/or the cart lines).

1. **Resolve.** Each distinct id (at most the first 10) is looked up with `GetProduct`. Ids the catalog does not know (`NOT_FOUND`) are skipped. The category of a product is its `department` (the catalog's `Category.id`, the same value the catalog's filter takes); products without one add no category.
2. **Candidates.** For every collected category the service asks the catalog for `ListProductsFiltered(department, sort=RATING_DESC, page_size=50)` and pages through at most 3 pages (150 products). If no category was collected (no ids, none resolved, or only products without a department) the "category" is the whole shop (empty department filter).
3. **Rank.** Candidates of all collected categories are merged, **every id in `product_ids` is removed (also unknown ones)**, and the rest is sorted by `rating` desc, `review_count` desc, product id asc. The first 5 ids are returned. The result is deterministic: same catalog state and same input give the same answer; `user_id` plays no part.
4. **Cache.** The candidate list of each category (and of the whole shop) is cached in memory for **60 s**, measured on a monotonic clock (`time.monotonic`; the host's wall clock can jump by tens of seconds). The `GetProduct` lookups are not cached. A failed fetch is never cached. The cache is per process (replicas do not share it), so a catalog change shows up after at most 60 s.
5. **Fallback.** If anything in steps 1-3 fails (catalog down, deadline of 2 s per call exceeded, an older catalog without `ListProductsFiltered`, any other error) the service logs one `WARNING` ("Ranked recommendations unavailable, falling back to random picks: ...") and answers with the **old behaviour**: `ListProducts`, remove the given ids, random sample of up to 5. The caller sees a normal response. Only when that old call fails as well does the caller get `INTERNAL`, as before.

Verified: unit/integration tests (88 when this was written, 130 on 2026-11-14, all green, `ruff check`/`ruff format --check` clean) and a container smoke test on a throwaway docker network against a fake catalog gRPC server (ranked answers for a viewed product, two categories, no ids and an unknown id; with the catalog stopped the cached whole-shop list kept answering and the uncached requests ended in `INTERNAL` because the random fallback needs the catalog too). The catalog-down random fallback with a *working* `ListProducts` is covered by the tests that run the real `server.py`; it was not run in a container, and nothing was run against the real compose catalog.

Edge cases: a category with no other product than the viewed one yields an empty list (there is no top-up from the whole shop); the 150-product limit per category means that in a larger category ties on rating at the page boundary are cut in the catalog's name order, not by review count.

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `PRODUCT_CATALOG_SERVICE_ADDR` | **Yes** | — | `host:port` (no scheme) of product-catalog gRPC. **The process exits at startup with `RuntimeError` if unset** |
| `CATALOG_TIMEOUT_SECONDS` | No | `2` | Deadline in seconds of EVERY product-catalog call: the ranking calls (`GetProduct`, `ListProductsFiltered`) and the `ListProducts` of the random fallback. A number from 0.1 to 30; unset, empty, not a number, `nan`/`inf` or out of range means 2 (a typo never removes the deadline). A fallback call that hits the deadline ends the RPC with `INTERNAL` ("Failed to fetch product catalog."). The value is logged in the `starting` line as `catalog_timeout_seconds` |
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

- The catalog channel is created with `grpc.insecure_channel`, which connects lazily — the service starts fine even if the catalog is down; individual RPCs then fail and the service answers from the random fallback. The ranking calls have a 2 s deadline each (a constant, not configurable); the random fallback call has none.
- Telemetry is **pushed**, not scraped: traces, metrics and logs share one OTLP/gRPC endpoint (Alloy); profiles go to Pyroscope (via Alloy). `service.instance.id` is the pod hostname, so replicas stay distinct in Mimir.
- The former Prometheus endpoint (`METRICS_PORT`) no longer exists. Logs go to stdout as JSON lines and, with telemetry on, are additionally exported over OTLP with trace ids.
- With every telemetry variable unset the service starts with telemetry and profiling off: the OTel SDK and exporters are not even imported, the RPC metric instruments are API no-ops, and the startup lines are `Connecting to productcatalog at …` and `{"message":"starting", ... "telemetry":"telemetry off, profiling off"}` (the lifecycle test runs the real server and checks them; not re-checked in the image).
- With an endpoint set but no reachable collector, the exporters log retry errors to stderr and the service keeps serving.

## Container image

Multi-stage [`Dockerfile`](Dockerfile):

| Stage | Base | What happens |
|---|---|---|
| builder | `python:3.12-alpine` | installs `g++`/`linux-headers`, `pip install -r requirements.txt` (fully pinned), **compiles the protos** with `grpc_tools.protoc` into `generated/` and patches the relative import in `recommendation_pb2_grpc.py` |
| runtime | `python:3.12-alpine` | copies site-packages, generated stubs (`/app/src/generated`), `src/`; runs as `10001:10001` |

- **Entrypoint:** `python src/server.py` (working dir `/app`).
- **User:** numeric `USER 10001:10001` (`appuser`/`appgroup`, created with these fixed ids; `id` in the container prints `uid=10001(appuser) gid=10001(appgroup)`). Build args `SERVICE_VERSION` (default `dev`) and `SERVICE_COMMIT` (default `unknown`) set the OCI labels `version`/`revision` (plus `source`, `licenses=Apache-2.0`) and the ENV of the same names.
- **EXPOSE:** `8080` only.
- **Docker `HEALTHCHECK`:** exec form (no shell): Python `grpc.channel_ready_future` against `localhost:$PORT`, 8080 when `PORT` is unset or empty.
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
  runAsUser: 10001
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
| **Logs** | Python `logging` with an own stdlib JSON formatter (`src/logsetup.py`) to stdout (always) **and** an OTel `LoggingHandler` (OTLP/gRPC, with trace ids; only when telemetry is on) | Alloy → Loki |
| **Profiles** | `pyroscope-io`, tags `service_name`, `version`, `environment`; only when `PYROSCOPE_ADDR` is a valid URL | Alloy `:4040` → Pyroscope |

Metric names: `rpc_server_duration_seconds_{bucket,count,sum}` (buckets 1 ms … 10 s; the unit `s` yields `_seconds`), `rpc_server_requests_total`, `rpc_server_active_requests` (labels `rpc_system`, `rpc_service`, `rpc_method`, `rpc_grpc_status_code`). `build_info{service,version,commit}` is always 1 (version and commit from `SERVICE_VERSION` / `SERVICE_COMMIT`, `dev` / `unknown` when unset). Runtime metrics (standard library only): `python_process_resident_memory_bytes`, `python_process_cpu_seconds_total`, `python_process_uptime_seconds`, `python_process_open_fds`, `python_threads`, `python_gc_collections_total{generation}`, `python_gc_objects_collected_total{generation}`. Each stdout line is one valid JSON object (quotes and newlines, e.g. in gRPC error texts, are escaped; a test asserts this) with `timestamp` (RFC3339 UTC, milliseconds, e.g. `2026-11-13T10:00:00.123Z`), `severity` (`debug`, `info`, `warning`, `error`), `message`, `service`, `version`, `trace_id` and `span_id` (hex, only while a span is active, never empty or zero) and `error` (string, on errors). The former keys `name` and the epoch-float `timestamp` are gone. There is no `request_id` (this service has none). `LOG_LEVEL` sets the level. The `user_id` of a request is not logged (the frontend sends an account id or a session id there). Uncaught exceptions, also at import time or in a thread (e.g. `PRODUCT_CATALOG_SERVICE_ADDR` unset), are one JSON line (`message: "uncaught exception"`, `error`) and exit code 1, no traceback on stdout.

Trace correlation: the handler runs inside the gRPC server span, so its log lines (and the ones of the catalog client call, which is a child client span) carry that span's `trace_id`/`span_id`; an incoming `traceparent` from the frontend continues the same trace (tested in-process with an in-memory exporter). With telemetry off there are no spans and no ids. Metrics stay OTLP-pushed RPC metrics; there is no scrape endpoint on purpose (the Go services serve `/metrics` on 9090, this service delivers RED metrics over OTLP). Resource attributes on every signal: `service.name`, `service.version`, `service.namespace=everly`, `deployment.environment`, `service.instance.id`.

Every series carries `job="recommendation-service"` (from `service.name`) and `instance=<pod name>` (`service.instance.id`), which is what the Grafana dashboards select on. Logs land in Loki with `service_name="recommendation-service"` and trace/span ids, so a log line links to its trace.

## Running locally

Requirements: Python 3.12 (what the image runs; `uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt -r requirements-dev.txt`) or 3.13 (`.python-version`, what the lock was compiled for), Docker, a running `product-catalog-service` (or any gRPC stub on the configured address), [`grpcurl`](https://github.com/fullstorydev/grpcurl).

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
# tests/test_ranking.py compiles the proto too and runs the ranking against a fake catalog gRPC server (tests/catalog_fake.py),
# in-process and with the real server.py in a subprocess (ranking, exclusion, paging, 60 s cache, fallback)

docker build -t recommendation-service .
docker run --rm -p 8081:8080 \
  -e PRODUCT_CATALOG_SERVICE_ADDR=host.docker.internal:3550 recommendation-service
```

### Manual gRPC testing (reflection is on; `-proto` also works)

```bash
grpcurl -plaintext -proto proto/recommendation.proto \
  -d '{"user_id": "u1", "product_ids": ["OLJCESPC7Z", "66VCHSJNUP"]}' \
  localhost:8080 hipstershop.RecommendationService/ListRecommendations

grpcurl -plaintext -proto proto/health.proto localhost:8080 grpc.health.v1.Health/Check
```

## Project structure

```
├── src/
│   ├── server.py        # gRPC server (10-thread pool), RecommendationService + Health servicers, catalog client stub, random fallback, JSON logs, SIGTERM
│   ├── ranking.py       # category/rating ranking, 60 s monotonic-clock cache of the catalog category lists
│   ├── telemetry.py     # telemetry contract, resource attributes, OTel traces/metrics/logs (server + client instrumentation), Pyroscope, bounded shutdown
│   ├── logsetup.py      # JSON log formatter (shared field set, trace ids), LOG_LEVEL, JSON excepthooks
│   ├── generated/       # protoc output (git: only __init__.py; real stubs are built in Docker)
│   └── __init__.py
├── proto/               # recommendation.proto, health.proto
├── requirements.in / requirements.txt / requirements-dev.txt
├── pyproject.toml / pytest.ini / .python-version / .trivyignore
├── Dockerfile / .dockerignore
└── docs/                # empty placeholder
```

## Verification status (observability step 3, 2026-11-13)

| What | Result |
|---|---|
| `uv venv --python 3.12` (3.12.3), `uv pip install -r requirements.txt -r requirements-dev.txt`, stubs generated into `src/generated`, `pytest` | 130 passed. New: env matrix (endpoint, Pyroscope, resource attributes), log golden line and trace ids (in-process gRPC server with an in-memory exporter), JSON startup failure, malformed endpoint does not crash |
| `uvx ruff check .` and `ruff format --check .` | clean |
| Rollout prep 2026-11-14, merged head (origin/main merged in): same venv recipe, `pytest`; `ruff check .` / `ruff format --check .`; `docker build` (image runs as `10001:10001`, then removed) | 130 passed; ruff clean; build ok |
| NOT run | the compose stack, Tempo/Loki/Mimir/Pyroscope queries, telemetry ON against a real collector |

## Known limitations

- Ranking uses only categories, ratings and review counts; `user_id` is not used and nothing is personalised.
- A category with no other product than the viewed one yields no recommendations (no top-up from the whole shop). At most 150 products per category are considered.
- The category cache is per replica and lives 60 s; changes in the catalog appear after at most that long.
- The random fallback still fetches the whole catalog (`ListProducts`) and has no deadline.
- Health ignores catalog availability.
- Health stays `SERVING` during the 2 s shutdown drain (it is static).
- `requirements.txt` was compiled for Python 3.13 (`--python-version 3.13`) while the image runs 3.12; it installs and runs there (verified by the image build and the local test run on Python 3.12), but the lock was not recompiled for 3.12.
