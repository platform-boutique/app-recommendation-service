"""
Category- and rating-based ranking for ListRecommendations.

Given the products a visitor is looking at, find their categories (the catalog calls them departments, the
`Product.department` field), fetch the products of those categories through the catalog's filtered listing and rank
them by rating desc, review_count desc, product id asc. Nothing here is random: the same catalog state and the same
input always give the same answer.

The per-category lists are cached for CACHE_TTL_SECONDS on a monotonic clock (the host's wall clock jumps by tens of
seconds, so time.time() would expire or extend entries at random).

Any catalog problem raises; the caller (server.py) logs a warning and falls back to the old random behaviour.
"""

import threading
import time
from typing import Callable, Iterable, NamedTuple

import grpc

MAX_RESULTS = 5
CACHE_TTL_SECONDS = 60.0
# One category list is fetched as at most MAX_PAGES pages of PAGE_SIZE products, sorted rating-desc by the catalog.
PAGE_SIZE = 50
MAX_PAGES = 3
# The cart can hold many lines; only the first MAX_LOOKUPS distinct ids are resolved to categories.
MAX_LOOKUPS = 10
# Default deadline of every catalog call (ranking path and random fallback); a hanging catalog then ends in the random
# fallback, and a hanging fallback call ends in INTERNAL. CATALOG_TIMEOUT_SECONDS in the environment overrides it.
CATALOG_TIMEOUT_SECONDS = 2.0
MIN_CATALOG_TIMEOUT_SECONDS = 0.1
MAX_CATALOG_TIMEOUT_SECONDS = 30.0

ALL_PRODUCTS = ""  # cache key / department filter meaning "the whole shop"


def catalog_timeout(env) -> float:
    """CATALOG_TIMEOUT_SECONDS from the environment: a number of seconds in [0.1, 30]. Anything else (unset, empty, not
    a number, nan, out of range) means the default, so a typo never removes the deadline."""
    raw = (env.get("CATALOG_TIMEOUT_SECONDS") or "").strip()
    try:
        value = float(raw)
    except ValueError:
        return CATALOG_TIMEOUT_SECONDS
    if not MIN_CATALOG_TIMEOUT_SECONDS <= value <= MAX_CATALOG_TIMEOUT_SECONDS:  # False for nan too
        return CATALOG_TIMEOUT_SECONDS
    return value


class Candidate(NamedTuple):
    id: str
    rating: float
    review_count: int


def rank_key(c: Candidate):
    return (-c.rating, -c.review_count, c.id)


class Ranker:
    def __init__(
        self, catalog_stub, pb2, clock: Callable[[], float] = time.monotonic, timeout: float = CATALOG_TIMEOUT_SECONDS
    ):
        """`pb2` is the generated recommendation_pb2 module (injected so tests can pass their own)."""
        self._catalog = catalog_stub
        self._pb2 = pb2
        self._clock = clock
        self._timeout = timeout
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[float, list[Candidate]]] = {}

    # -- public ------------------------------------------------------------

    def recommend(self, viewed_ids: Iterable[str]) -> list[str]:
        """Up to MAX_RESULTS product ids, best first. Raises on any catalog failure."""
        viewed = list(dict.fromkeys(i for i in viewed_ids if i))
        departments = self._departments_of(viewed[:MAX_LOOKUPS])

        pool: dict[str, Candidate] = {}
        for dept in departments or [ALL_PRODUCTS]:
            for c in self._candidates(dept):
                pool.setdefault(c.id, c)

        excluded = set(viewed)
        ranked = sorted((c for c in pool.values() if c.id not in excluded), key=rank_key)
        return [c.id for c in ranked[:MAX_RESULTS]]

    # -- catalog access ------------------------------------------------------

    def _departments_of(self, product_ids: list[str]) -> list[str]:
        """Distinct non-empty departments of the products that exist, in input order. Unknown ids are skipped."""
        departments: dict[str, None] = {}
        for pid in product_ids:
            try:
                product = self._catalog.GetProduct(self._pb2.GetProductRequest(id=pid), timeout=self._timeout)
            except grpc.RpcError as err:
                if _code(err) == grpc.StatusCode.NOT_FOUND:
                    continue
                raise
            if product.department:
                departments[product.department] = None
        return list(departments)

    def _candidates(self, department: str) -> list[Candidate]:
        now = self._clock()
        with self._lock:
            hit = self._cache.get(department)
            if hit is not None and now - hit[0] < CACHE_TTL_SECONDS:
                return hit[1]

        fetched = self._fetch(department)
        with self._lock:
            # Stamp with the time the fetch finished so a slow fetch does not shorten the entry's life.
            self._cache[department] = (self._clock(), fetched)
        return fetched

    def _fetch(self, department: str) -> list[Candidate]:
        out: list[Candidate] = []
        for page in range(1, MAX_PAGES + 1):
            resp = self._catalog.ListProductsFiltered(
                self._pb2.ListProductsRequest(
                    department=department,
                    sort=self._pb2.PRODUCT_SORT_RATING_DESC,
                    page=page,
                    page_size=PAGE_SIZE,
                ),
                timeout=self._timeout,
            )
            out.extend(Candidate(p.id, p.rating, p.review_count) for p in resp.products)
            if len(resp.products) < PAGE_SIZE or (resp.total and len(out) >= resp.total):
                break
        return out


def _code(err: grpc.RpcError):
    code = getattr(err, "code", None)
    return code() if callable(code) else None
