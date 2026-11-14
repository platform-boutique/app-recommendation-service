# Logging comes first so that even an import error is reported as one JSON line; telemetry is imported before the
# server is built so that it can patch grpc.
import logging
import os
import random
import signal
import sys
import threading
from concurrent import futures

import logsetup

# ---------------------------------------------------------------------------
# Logger
# ---------------------------------------------------------------------------

logger = logging.getLogger("recommendation-service")
handler = logsetup.make_handler(
    os.environ.get("OTEL_SERVICE_NAME", "").strip() or "recommendation-service",
    os.environ.get("SERVICE_VERSION", "").strip() or "dev",
)
logger.addHandler(handler)
logger.setLevel(logsetup.log_level(os.environ))
logger.propagate = False
logsetup.install_excepthooks(logger)

import grpc  # noqa: E402
from grpc_health.v1 import health_pb2, health_pb2_grpc  # noqa: E402
from grpc_reflection.v1alpha import reflection  # noqa: E402

import telemetry  # noqa: E402
from generated import recommendation_pb2, recommendation_pb2_grpc  # noqa: E402
from ranking import CATALOG_TIMEOUT_SECONDS, Ranker, catalog_timeout  # noqa: E402
from telemetry import start_rpc_metrics  # noqa: F401, E402

# ---------------------------------------------------------------------------
# ProductCatalog gRPC client (upstream dependency)
# ---------------------------------------------------------------------------


def _build_product_catalog_stub() -> recommendation_pb2_grpc.ProductCatalogServiceStub:
    addr = os.environ.get("PRODUCT_CATALOG_SERVICE_ADDR", "")
    if not addr:
        raise RuntimeError("PRODUCT_CATALOG_SERVICE_ADDR environment variable not set")
    logger.info(f"Connecting to productcatalog at {addr}")
    channel = grpc.insecure_channel(addr)
    return recommendation_pb2_grpc.ProductCatalogServiceStub(channel)


# ---------------------------------------------------------------------------
# gRPC service implementation
# ---------------------------------------------------------------------------


class RecommendationServiceServicer(recommendation_pb2_grpc.RecommendationServiceServicer):
    MAX_RESPONSES = 5

    def __init__(self, product_catalog_stub, ranker=None, catalog_timeout_seconds=CATALOG_TIMEOUT_SECONDS):
        self._catalog = product_catalog_stub
        self._timeout = catalog_timeout_seconds
        self._ranker = (
            ranker
            if ranker is not None
            else Ranker(product_catalog_stub, recommendation_pb2, timeout=catalog_timeout_seconds)
        )

    def _rank(self, request):
        """Ranked ids (categories of the viewed products, rating desc), or None if the catalog cannot answer."""
        try:
            return self._ranker.recommend(request.product_ids)
        except Exception as err:  # any catalog trouble: the caller still gets recommendations
            logger.warning(
                "Ranked recommendations unavailable, falling back to random picks", extra={"error": repr(err)}
            )
            return None

    def _random_pick(self, owned_ids):
        """The original behaviour: random sample of the whole catalog minus the given ids."""
        cat_response = self._catalog.ListProducts(recommendation_pb2.Empty(), timeout=self._timeout)
        all_ids = [p.id for p in cat_response.products]
        candidates = list(set(all_ids) - set(owned_ids))
        num_return = min(self.MAX_RESPONSES, len(candidates))
        return random.SystemRandom().sample(candidates, num_return)

    def ListRecommendations(self, request, context):
        end_metrics = start_rpc_metrics("ListRecommendations")
        try:
            recommended = self._rank(request)
            if recommended is None:
                recommended = self._random_pick(request.product_ids)

            # The user id is not logged: the frontend sends the account id or the session id there.
            logger.info(f"ListRecommendations returning {len(recommended)} product_ids={recommended}")

            response = recommendation_pb2.ListRecommendationsResponse()
            response.product_ids.extend(recommended)
            end_metrics("0")  # OK
            return response

        except grpc.RpcError as err:
            logger.error("ProductCatalog RPC failed", extra={"error": str(err)})
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details("Failed to fetch product catalog.")
            end_metrics("13")  # INTERNAL
            return recommendation_pb2.ListRecommendationsResponse()

        except Exception as err:
            logger.error("ListRecommendations failed", extra={"error": str(err)})
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details("An unexpected error occurred.")
            end_metrics("13")  # INTERNAL
            return recommendation_pb2.ListRecommendationsResponse()


class HealthServicer(health_pb2_grpc.HealthServicer):
    def Check(self, request, context):
        return health_pb2.HealthCheckResponse(
            status=health_pb2.HealthCheckResponse.SERVING,
        )


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


# Seconds in-flight RPCs get to finish after SIGTERM before they are cancelled. Drain (2 s) plus the telemetry
# flush (at most 2 s) stays inside the charts' terminationGracePeriodSeconds: 5.
SHUTDOWN_GRACE_SECONDS = 2


def enable_reflection(server) -> None:
    """gRPC server reflection (CODE-02), so grpcurl works without the .proto files."""
    names = (
        recommendation_pb2.DESCRIPTOR.services_by_name["RecommendationService"].full_name,
        health_pb2.DESCRIPTOR.services_by_name["Health"].full_name,
        reflection.SERVICE_NAME,
    )
    reflection.enable_server_reflection(names, server)


def install_signal_handlers(stop_event: threading.Event) -> None:
    """SIGTERM/SIGINT set stop_event. Needed because PID 1 in a container ignores signals without a handler."""

    def _handler(signum, _frame):
        logger.info(f"Received {signal.Signals(signum).name}, shutting down")
        stop_event.set()

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def shutdown(server, grace: float = SHUTDOWN_GRACE_SECONDS) -> None:
    """Stop accepting RPCs, let in-flight ones finish for up to `grace` seconds, then flush telemetry."""
    server.stop(grace).wait()
    logger.info("RecommendationService stopped")
    telemetry.shutdown()


def main() -> int:
    port = os.environ.get("PORT", "8080")
    stop_event = threading.Event()
    install_signal_handlers(stop_event)

    product_catalog_stub = _build_product_catalog_stub()
    timeout = catalog_timeout(os.environ)

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    recommendation_pb2_grpc.add_RecommendationServiceServicer_to_server(
        RecommendationServiceServicer(product_catalog_stub, catalog_timeout_seconds=timeout), server
    )
    health_pb2_grpc.add_HealthServicer_to_server(HealthServicer(), server)
    enable_reflection(server)

    server.add_insecure_port(f"[::]:{port}")
    server.start()
    logger.info(
        "starting",
        extra={
            "fields": {
                "commit": telemetry.service_commit(os.environ),
                "port": port,
                "catalog_timeout_seconds": timeout,
                "telemetry": telemetry.describe(),
            }
        },
    )

    stop_event.wait()
    shutdown(server)
    return 0


if __name__ == "__main__":
    code = main()
    # Everything is flushed and stopped by now. Leave without interpreter finalization: with the OTLP exporter and
    # the Pyroscope profiler both pointed at a collector that does not answer, finalization blocks until SIGKILL.
    logging.shutdown()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
