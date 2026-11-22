# Known limitations

- Ranking uses only categories, ratings and review counts; `user_id` is not used and nothing is personalised.
- A category with no other product than the viewed one yields no recommendations (no top-up from the whole shop). At most 150 products per category are considered.
- The category cache is per replica and lives 60 s; changes in the catalog appear after at most that long.
- The random fallback still fetches the whole catalog (`ListProducts`); it uses the same deadline as the ranking calls (`CATALOG_TIMEOUT_SECONDS`, default 2 s) and its failure ends the RPC with `INTERNAL`.
- Health ignores catalog availability.
- Metrics are RED, `build_info` and runtime metrics. There is no business counter (OBS-20 names orders, reservations and payments, none of which this service owns); how often the random fallback answers is visible only as `WARNING` logs (decision 2026-11-22).
- Health stays `SERVING` during the 2 s shutdown drain (it is static).
- `requirements.txt` was compiled for Python 3.13 (`--python-version 3.13`) while the image runs 3.12; it installs and runs there (verified by the image build and the local test run on Python 3.12), but the lock was not recompiled for 3.12.
