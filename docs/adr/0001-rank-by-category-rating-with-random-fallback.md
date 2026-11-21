# 1. Rank by category, rating and review count, with the old random answer as fallback

Status: accepted (reconstructed from the code and the earlier README).

## Context

The original recommendation service returned a random sample of the catalog. The shop now has departments, ratings and review counts, and the catalog can filter and sort, but the frontend must keep getting an answer when the catalog is slow or an older catalog without `ListProductsFiltered` is running.

## Decision

Resolve the viewed products to their departments (`GetProduct`), fetch each department's candidates sorted by rating (`ListProductsFiltered`, cached 60 s per process on a monotonic clock), merge, drop the viewed ids and sort by rating, review count and id. The result is deterministic and personalisation is not attempted (`user_id` is only logged). If any step fails, log a warning and answer with the old behaviour (`ListProducts`, random sample).

## Consequences

- Stable, explainable results; no state to keep in sync.
- The cache is per replica and changes appear after at most 60 s.
- A category with no other product yields an empty list; at most 150 products per category are considered.
- The fallback needs the catalog as well, so a dead catalog still ends in `INTERNAL`.
