# Known limitations

- Fetches the whole catalog on every request (fine for 9 products; will not scale to large catalogs).
- Recommendations are random — no ranking, no use of `user_id`.
- Health ignores catalog availability; no deadline on the catalog call.
- Health stays `SERVING` during the 2 s shutdown drain (it is static).
- `requirements.txt` was compiled for Python 3.13 (`--python-version 3.13`) while the image runs 3.12; it installs and runs there (verified by the image build and the local test run on Python 3.12), but the lock was not recompiled for 3.12.
