# Operations

## Health

`grpc.health.v1.Health/Check` on the gRPC port (`8080`) is a static `SERVING`: it does not reflect the catalog. It stays `SERVING` during the 2 s shutdown drain. The image `HEALTHCHECK` is an exec-form Python probe against the local gRPC port (no shell).

```bash
grpcurl -plaintext localhost:8080 grpc.health.v1.Health/Check
grpcurl -plaintext localhost:8080 list
```

## Shutdown

On SIGTERM or SIGINT the server stops accepting RPCs, lets in-flight ones finish for up to 2 s (`SHUTDOWN_GRACE_SECONDS`), flushes telemetry (bounded, also with an unreachable collector) and exits 0. The lifecycle test runs the real server and checks that SIGTERM ends it with exit 0; `docker stop` of the image with the OTLP and Pyroscope addresses pointed at an address that never answers took 2.4 s and the exit code was 0 (2026-11-22). `telemetry.shutdown()` waits for its flush for exactly its deadline (2 s by default): measured 2.00-2.02 s against an unreachable collector, also with 24 busy processes on a 12-core host; an earlier note of 5-6 s did not reproduce.

## Scaling and replicas

Stateless: scale freely. The category cache (60 s) is per process, so replicas can answer from slightly different catalog states.

## Failure behaviour

| Failure | Behaviour |
|---|---|
| `PRODUCT_CATALOG_SERVICE_ADDR` unset | the process exits at start with a `RuntimeError` |
| Catalog down at start | the service starts (lazy channel); requests fall back to random picks |
| Catalog call fails or exceeds the deadline (2 s by default) | one `WARNING` ("Ranked recommendations unavailable, falling back to random picks"), the caller gets a normal answer |
| The fallback fails as well | `INTERNAL` ("Failed to fetch product catalog.") |
| Unknown product id in the request | skipped, and removed from the result |
| Collector unreachable | the exporters log retry errors to stderr and the service keeps serving |

## Runbook

| Symptom | Check |
|---|---|
| Recommendations look random | the `WARNING` log line names the catalog error; check product-catalog-service and `PRODUCT_CATALOG_SERVICE_ADDR` |
| Recommendations stay empty for a product | its department has no other product (there is no top-up from the whole shop) |
| `INTERNAL` | the catalog is down or too slow for the random fallback too; check `CATALOG_TIMEOUT_SECONDS` |
| A new product does not show | the category cache lives 60 s |

There is no dashboard or alert rule for this service in the repository yet (the dashboards are an open task).
