"""
Category- and rating-based recommendations (src/ranking.py and its use in src/server.py).

The ranking tests talk to a fake product-catalog gRPC server over a real channel, using the real generated stubs.
The servicer tests start the real server in a subprocess against the same fake catalog.
"""

import json
import os
import random
import shutil
import signal
import socket
import subprocess
import sys
import time

import grpc
import pytest

import ranking
from tests.catalog_fake import ROOT, compile_stubs, make_fake_catalog, protoc, serve

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def P(pid, dept, rating, reviews):
    return {"id": pid, "department": dept, "rating": rating, "review_count": reviews}


SHOP = [
    P("T1", "toys", 4.9, 10),
    P("T2", "toys", 4.9, 50),
    P("T3", "toys", 4.9, 50),
    P("T4", "toys", 3.0, 5),
    P("T5", "toys", 4.0, 1),
    P("T6", "toys", 4.5, 100),
    P("T7", "toys", 2.0, 0),
    P("E1", "tech", 4.8, 5),
    P("E2", "tech", 4.95, 1),
    P("O1", "other", 5.0, 2),
    P("N1", "", 4.99, 9),  # no department
]


@pytest.fixture(scope="module")
def stubs(tmp_path_factory):
    pytest.importorskip("grpc_tools")
    return compile_stubs(tmp_path_factory.mktemp("stubs"))


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Env:
    def __init__(self, stubs, products=SHOP):
        self.pb2, self.pb2_grpc = stubs
        self.catalog = make_fake_catalog(self.pb2, self.pb2_grpc, products)
        self.server, self.addr = serve(self.pb2_grpc, self.catalog)
        self.channel = grpc.insecure_channel(self.addr)
        self.stub = self.pb2_grpc.ProductCatalogServiceStub(self.channel)
        self.clock = Clock()
        self.ranker = ranking.Ranker(self.stub, self.pb2, clock=self.clock)

    def close(self):
        self.channel.close()
        self.server.stop(0)


@pytest.fixture()
def env(stubs):
    e = Env(stubs)
    yield e
    e.close()


# ---------------------------------------------------------------------------
# Ranking
# ---------------------------------------------------------------------------


class TestRanking:
    def test_ranks_the_category_by_rating_then_reviews_then_id(self, env):
        # toys minus T1: T2 and T3 tie on rating and reviews (id decides), then T6 4.5, T5 4.0, T4 3.0; T7 is cut at 5
        assert env.ranker.recommend(["T1"]) == ["T2", "T3", "T6", "T5", "T4"]

    def test_review_count_breaks_rating_ties(self, env):
        assert env.ranker.recommend(["T6"])[:3] == ["T2", "T3", "T1"]  # T1 has 10 reviews, T2/T3 have 50

    def test_returns_at_most_five(self, env):
        assert len(env.ranker.recommend(["T1"])) == 5

    def test_fewer_candidates_than_five(self, env):
        assert env.ranker.recommend(["E1"]) == ["E2"]

    def test_given_ids_are_excluded(self, env):
        out = env.ranker.recommend(["T1", "T2", "T3"])
        assert not {"T1", "T2", "T3"} & set(out)
        assert out == ["T6", "T5", "T4", "T7"]

    def test_unknown_given_ids_are_excluded_too(self, env):
        assert "T2" not in env.ranker.recommend(["T1", "T2-does-not-exist", "T2"])

    def test_other_categories_are_not_mixed_in(self, env):
        assert all(i.startswith("T") for i in env.ranker.recommend(["T4"]))

    def test_several_viewed_categories_are_merged_and_ranked_together(self, env):
        # toys + tech, T1 and E1 excluded: E2 4.95 first, then T2/T3 4.9, T6 4.5, T5 4.0
        assert env.ranker.recommend(["T1", "E1"]) == ["E2", "T2", "T3", "T6", "T5"]

    def test_no_input_ids_gives_the_top_rated_of_the_whole_shop(self, env):
        assert env.ranker.recommend([]) == ["O1", "N1", "E2", "T2", "T3"]

    def test_ids_that_do_not_resolve_give_the_whole_shop(self, env):
        assert env.ranker.recommend(["nope", "nada"]) == ["O1", "N1", "E2", "T2", "T3"]

    def test_a_category_with_nothing_else_in_it_gives_nothing(self, env):
        assert env.ranker.recommend(["O1"]) == []

    def test_product_without_department_gives_the_whole_shop(self, env):
        assert env.ranker.recommend(["N1"]) == ["O1", "E2", "T2", "T3", "T1"]

    def test_empty_and_duplicate_ids_are_ignored(self, env):
        assert env.ranker.recommend(["", "T1", "T1"]) == ["T2", "T3", "T6", "T5", "T4"]
        assert env.catalog.count("GetProduct") == 1

    def test_only_the_first_ten_ids_are_looked_up(self, env):
        env.ranker.recommend([f"X{i}" for i in range(25)])
        assert env.catalog.count("GetProduct") == ranking.MAX_LOOKUPS

    def test_deterministic_whatever_the_catalog_order(self, stubs):
        results = set()
        for seed in range(5):
            products = SHOP[:]
            random.Random(seed).shuffle(products)
            e = Env(stubs, products)
            try:
                results.add((tuple(e.ranker.recommend(["T1"])), tuple(e.ranker.recommend([]))))
            finally:
                e.close()
        assert len(results) == 1

    def test_does_not_use_randomness(self, env, monkeypatch):
        def boom(*a, **k):
            raise AssertionError("random used")

        monkeypatch.setattr(random.SystemRandom, "sample", boom)
        monkeypatch.setattr(random, "sample", boom)
        assert env.ranker.recommend(["T1"])


# ---------------------------------------------------------------------------
# What is asked of the catalog
# ---------------------------------------------------------------------------


def _many(n, dept="big"):
    # Rating descends with the number, so P000 is the best product.
    return [P(f"P{i:03d}", dept, 5.0 - i / 1000, 1) for i in range(n)]


class TestCatalogCalls:
    def test_filtered_request_has_department_sort_and_page_size(self, env):
        env.ranker.recommend(["T1"])
        ((_, req),) = [c for c in env.catalog.calls if c[0] == "ListProductsFiltered"]
        assert req.department == "toys"
        assert req.sort == env.pb2.PRODUCT_SORT_RATING_DESC
        assert (req.page, req.page_size) == (1, 50)

    def test_whole_shop_request_has_no_department(self, env):
        env.ranker.recommend([])
        ((_, req),) = [c for c in env.catalog.calls if c[0] == "ListProductsFiltered"]
        assert req.department == ""
        assert env.catalog.count("GetProduct") == 0

    def test_never_fetches_the_whole_catalog_unfiltered(self, env):
        env.ranker.recommend(["T1"])
        env.ranker.recommend([])
        assert env.catalog.count("ListProducts") == 0

    @pytest.mark.parametrize("size,pages", [(10, 1), (50, 1), (51, 2), (100, 2), (120, 3), (400, 3)])
    def test_pages_through_at_most_three_pages_of_fifty(self, stubs, size, pages):
        e = Env(stubs, _many(size))
        try:
            assert e.ranker.recommend([])[0] == "P000"
            assert e.catalog.count("ListProductsFiltered") == pages
        finally:
            e.close()

    def test_best_products_of_a_big_category_win_even_beyond_page_one(self, stubs):
        e = Env(stubs, _many(120))
        try:
            assert e.ranker.recommend(["P000"]) == ["P001", "P002", "P003", "P004", "P005"]
        finally:
            e.close()

    def test_every_call_has_a_deadline(self, stubs):
        seen = []

        class Spy:
            def __init__(self, inner):
                self.inner = inner

            def GetProduct(self, req, **kw):
                seen.append(kw.get("timeout"))
                return self.inner.GetProduct(req, **kw)

            def ListProductsFiltered(self, req, **kw):
                seen.append(kw.get("timeout"))
                return self.inner.ListProductsFiltered(req, **kw)

        e = Env(stubs)
        try:
            ranking.Ranker(Spy(e.stub), e.pb2).recommend(["T1"])
        finally:
            e.close()
        assert seen == [ranking.CATALOG_TIMEOUT_SECONDS] * 2


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


class TestCache:
    def test_second_call_is_served_from_the_cache(self, env):
        first = env.ranker.recommend(["T1"])
        assert env.catalog.count("ListProductsFiltered") == 1
        assert env.ranker.recommend(["T1"]) == first
        assert env.catalog.count("ListProductsFiltered") == 1

    def test_category_list_is_shared_between_different_viewed_products(self, env):
        env.ranker.recommend(["T1"])
        env.ranker.recommend(["T2"])
        assert env.catalog.count("ListProductsFiltered") == 1

    def test_each_category_and_the_whole_shop_have_their_own_entry(self, env):
        env.ranker.recommend(["T1"])
        env.ranker.recommend(["E1"])
        env.ranker.recommend([])
        assert env.catalog.count("ListProductsFiltered") == 3
        env.ranker.recommend(["T1", "E1"])
        env.ranker.recommend([])
        assert env.catalog.count("ListProductsFiltered") == 3

    def test_viewed_product_lookup_is_not_cached(self, env):
        env.ranker.recommend(["T1"])
        env.ranker.recommend(["T1"])
        assert env.catalog.count("GetProduct") == 2

    def test_entry_lives_for_sixty_seconds_on_the_injected_clock(self, env):
        assert ranking.CACHE_TTL_SECONDS == 60
        env.ranker.recommend(["T1"])
        env.clock.now += 59.9
        env.ranker.recommend(["T1"])
        assert env.catalog.count("ListProductsFiltered") == 1
        env.clock.now += 0.2
        env.ranker.recommend(["T1"])
        assert env.catalog.count("ListProductsFiltered") == 2

    def test_refetch_picks_up_catalog_changes(self, env):
        assert env.ranker.recommend(["T1"])[0] == "T2"
        env.catalog.products.append(env.pb2.Product(id="T9", department="toys", rating=5.0, review_count=1))
        assert env.ranker.recommend(["T1"])[0] == "T2"  # still cached
        env.clock.now += 61
        assert env.ranker.recommend(["T1"])[0] == "T9"

    def test_default_clock_is_monotonic(self, env):
        assert ranking.Ranker(env.stub, env.pb2)._clock is time.monotonic

    def test_wall_clock_jumps_do_not_touch_the_cache(self, env, monkeypatch):
        # A plain in-process stub: patching time.time would also confuse gRPC's own deadline handling.
        pb2, calls = env.pb2, []

        class Stub:
            def GetProduct(self, req, **kw):
                return pb2.Product(id="T1", department="toys")

            def ListProductsFiltered(self, req, **kw):
                calls.append(req)
                return pb2.ListProductsResponse(products=[pb2.Product(id="T2", rating=4.0)], total=1)

        ranker = ranking.Ranker(Stub(), pb2)  # real monotonic clock
        ranker.recommend(["T1"])
        wall = time.time()
        for jump in (3600, -3600, 45, -45):
            monkeypatch.setattr(time, "time", lambda jump=jump: wall + jump)
            assert ranker.recommend(["T1"]) == ["T2"]
        assert len(calls) == 1

    def test_failed_fetch_is_not_cached(self, env):
        env.catalog.fail_with = grpc.StatusCode.UNAVAILABLE
        with pytest.raises(grpc.RpcError):
            env.ranker.recommend([])
        env.catalog.fail_with = None
        assert env.ranker.recommend([])[0] == "O1"


# ---------------------------------------------------------------------------
# Failures (the ranker raises; server.py turns that into the random fallback)
# ---------------------------------------------------------------------------


class TestFailures:
    @pytest.mark.parametrize("ids", [[], ["T1"]])
    @pytest.mark.parametrize(
        "code", [grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.INTERNAL, grpc.StatusCode.UNIMPLEMENTED]
    )
    def test_catalog_errors_raise(self, env, ids, code):
        env.catalog.fail_with = code
        with pytest.raises(grpc.RpcError):
            env.ranker.recommend(ids)

    def test_unknown_product_is_not_a_failure(self, env):
        assert env.ranker.recommend(["nope"])[0] == "O1"

    def test_catalog_that_is_down_raises(self, stubs):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        pb2, pb2_grpc = stubs
        with grpc.insecure_channel(f"127.0.0.1:{port}") as channel:
            ranker = ranking.Ranker(pb2_grpc.ProductCatalogServiceStub(channel), pb2)
            with pytest.raises(grpc.RpcError):
                ranker.recommend(["T1"])


# ---------------------------------------------------------------------------
# The real server process against the fake catalog
# ---------------------------------------------------------------------------


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def app_dir(tmp_path_factory):
    pytest.importorskip("grpc_tools")
    app = tmp_path_factory.mktemp("app")
    shutil.copytree(ROOT / "src", app / "src", ignore=shutil.ignore_patterns("__pycache__"))
    gen = app / "src" / "generated"
    protoc(gen)
    stub = gen / "recommendation_pb2_grpc.py"
    stub.write_text(
        stub.read_text().replace(
            "import recommendation_pb2 as recommendation__pb2",
            "from . import recommendation_pb2 as recommendation__pb2",
        )
    )
    return app


class RunningService:
    """The real server.py in a subprocess, pointed at the fake catalog."""

    def __init__(self, app_dir, catalog_addr, stubs):
        self.pb2, self.pb2_grpc = stubs
        self.port = _free_port()
        env = {k: v for k, v in os.environ.items() if not k.startswith(("OTEL_", "PYROSCOPE", "ENABLE_"))}
        env.update(PORT=str(self.port), PRODUCT_CATALOG_SERVICE_ADDR=catalog_addr, PYTHONUNBUFFERED="1")
        self.proc = subprocess.Popen(
            [sys.executable, "src/server.py"],
            cwd=app_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.channel = grpc.insecure_channel(f"127.0.0.1:{self.port}")
        grpc.channel_ready_future(self.channel).result(timeout=15)
        self.stub = self.pb2_grpc.RecommendationServiceStub(self.channel)

    def recommend(self, ids):
        return self.stub.ListRecommendations(
            self.pb2.ListRecommendationsRequest(user_id="u1", product_ids=ids), timeout=10
        )

    def stop(self):
        self.channel.close()
        self.proc.send_signal(signal.SIGTERM)
        stdout, stderr = self.proc.communicate(timeout=10)
        records = [json.loads(line) for line in stdout.splitlines()]
        return self.proc.returncode, records, stderr


@pytest.fixture()
def service(stubs, app_dir):
    e = Env(stubs)
    svc = RunningService(app_dir, e.addr, stubs)
    yield e, svc
    if svc.proc.poll() is None:
        svc.proc.kill()
        svc.proc.communicate()
    e.close()


class TestServiceProcess:
    def test_ranked_recommendations_and_clean_shutdown(self, service):
        env, svc = service
        assert list(svc.recommend(["T1"]).product_ids) == ["T2", "T3", "T6", "T5", "T4"]
        assert list(svc.recommend([]).product_ids) == ["O1", "N1", "E2", "T2", "T3"]
        assert env.catalog.count("ListProducts") == 0
        code, records, stderr = svc.stop()
        assert code == 0
        assert not [r for r in records if r["severity"] in ("WARNING", "ERROR")]
        assert records[-1]["message"] == "RecommendationService stopped"

    def test_catalog_failure_falls_back_to_random_with_a_warning_and_no_error(self, service):
        env, svc = service
        env.catalog.fail_with = grpc.StatusCode.UNAVAILABLE  # ranking calls fail, ListProducts (old path) works
        out = list(svc.recommend(["T1", "T2"]).product_ids)  # no exception: not an RPC error for the caller
        assert len(out) == 5
        assert len(set(out)) == 5
        assert not {"T1", "T2"} & set(out)
        assert env.catalog.count("ListProducts") == 1
        code, records, _ = svc.stop()
        assert code == 0
        warnings = [r for r in records if r["severity"] == "WARNING"]
        assert len(warnings) == 1 and "falling back to random" in warnings[0]["message"]
        assert not [r for r in records if r["severity"] == "ERROR"]

    def test_fallback_result_varies(self, service):
        env, svc = service
        env.catalog.fail_with = grpc.StatusCode.UNAVAILABLE
        seen = {tuple(svc.recommend([]).product_ids) for _ in range(12)}
        svc.stop()
        assert len(seen) > 1  # 11 products choose 5 at random: 12 identical draws would be a 1 in 462**11 event

    def test_recovers_when_the_catalog_comes_back(self, service):
        env, svc = service
        env.catalog.fail_with = grpc.StatusCode.UNAVAILABLE
        svc.recommend(["T1"])
        env.catalog.fail_with = None
        assert list(svc.recommend(["T1"]).product_ids) == ["T2", "T3", "T6", "T5", "T4"]
        svc.stop()

    def test_when_even_the_old_path_fails_the_caller_gets_internal(self, service):
        env, svc = service
        env.catalog.fail_with = grpc.StatusCode.UNAVAILABLE
        env.catalog.fail_list_products = True
        with pytest.raises(grpc.RpcError) as err:
            svc.recommend(["T1"])
        assert err.value.code() == grpc.StatusCode.INTERNAL
        svc.stop()
