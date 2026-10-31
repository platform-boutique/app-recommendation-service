# API

Package `hipstershop`, service `RecommendationService` ([`proto/recommendation.proto`](https://github.com/platform-boutique/app-recommendation-service/blob/main/proto/recommendation.proto)); health in [`proto/health.proto`](https://github.com/platform-boutique/app-recommendation-service/blob/main/proto/health.proto). The proto also embeds the client definition of the `ProductCatalogService` calls this service makes (`ListProducts`, `GetProduct`, `ListProductsFiltered`); it is a copy of the catalog's proto with identical field numbers, additive changes only.

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

Edge cases: a category with no other product than the viewed one yields an empty list (there is no top-up from the whole shop); the 150-product limit per category means that in a larger category ties on rating at the page boundary are cut in the catalog's name order, not by review count.
