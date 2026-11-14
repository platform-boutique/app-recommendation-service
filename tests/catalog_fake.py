"""
A fake product-catalog gRPC server for the ranking tests, built on the REAL generated stubs.

The stubs are compiled from proto/recommendation.proto into a temp dir (what the Dockerfile does). They are loaded
under private names and never left in sys.modules, because test_server.py stubs `generated` there.
"""

import subprocess
import sys
from concurrent import futures
from pathlib import Path

import grpc

ROOT = Path(__file__).resolve().parent.parent


def protoc(out_dir: Path) -> None:
    """Compile proto/recommendation.proto into out_dir (absolute imports, as protoc writes them)."""
    subprocess.run(
        [
            sys.executable,
            "-m",
            "grpc_tools.protoc",
            f"-I{ROOT / 'proto'}",
            f"--python_out={out_dir}",
            f"--grpc_python_out={out_dir}",
            str(ROOT / "proto" / "recommendation.proto"),
        ],
        check=True,
    )


def compile_stubs(out_dir: Path):
    """Compile the proto into out_dir and return (recommendation_pb2, recommendation_pb2_grpc)."""
    protoc(out_dir)
    names = ("recommendation_pb2", "recommendation_pb2_grpc")
    saved = {n: sys.modules.pop(n) for n in names if n in sys.modules}
    sys.path.insert(0, str(out_dir))
    try:
        import recommendation_pb2
        import recommendation_pb2_grpc
    finally:
        sys.path.pop(0)
        for n in names:
            sys.modules.pop(n, None)
        sys.modules.update(saved)
    return recommendation_pb2, recommendation_pb2_grpc


def make_fake_catalog(pb2, pb2_grpc, products):
    """products: list of dicts (id, department, rating, review_count). Returns a servicer with call counters."""

    class FakeCatalog(pb2_grpc.ProductCatalogServiceServicer):
        def __init__(self):
            self.products = [pb2.Product(**p) for p in products]
            self.calls = []  # (method, request)
            self.fail_with = None  # grpc.StatusCode: every call except ListProducts aborts with it
            self.fail_list_products = False

        def GetProduct(self, request, context):
            self.calls.append(("GetProduct", request))
            if self.fail_with:
                context.abort(self.fail_with, "injected")
            for p in self.products:
                if p.id == request.id:
                    return p
            context.abort(grpc.StatusCode.NOT_FOUND, f"no product with ID {request.id}")

        def ListProductsFiltered(self, request, context):
            self.calls.append(("ListProductsFiltered", request))
            if self.fail_with:
                context.abort(self.fail_with, "injected")
            matches = [p for p in self.products if not request.department or p.department == request.department]
            # Same order as the real catalog's PRODUCT_SORT_RATING_DESC: rating desc, then name (here: id)
            matches.sort(key=lambda p: (-p.rating, p.id))
            size = request.page_size or 24
            page = request.page or 1
            return pb2.ListProductsResponse(
                products=matches[(page - 1) * size : page * size], total=len(matches), page=page, page_size=size
            )

        def ListProducts(self, request, context):
            self.calls.append(("ListProducts", request))
            if self.fail_list_products:
                context.abort(grpc.StatusCode.UNAVAILABLE, "injected")
            return pb2.ListProductsResponse(products=self.products)

        def count(self, method):
            return sum(1 for m, _ in self.calls if m == method)

    return FakeCatalog()


def serve(pb2_grpc, servicer):
    """Start the servicer on a free local port. Returns (server, address)."""
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=4))
    pb2_grpc.add_ProductCatalogServiceServicer_to_server(servicer, server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    return server, f"127.0.0.1:{port}"
