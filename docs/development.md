# Develop & test

## Checks and commands

Requirements: Python 3.12 (what the image runs) or 3.13 (`.python-version`), [uv](https://github.com/astral-sh/uv) for the virtualenv, Docker for the image, Python 3.12 for the docs check. The commands below are the ones the shared pipeline (`platform-demo-workflows`, `service-ci-python`) runs, so a failure can be reproduced without pushing. Setup: `uv venv --python 3.12 .venv && uv pip install --python .venv/bin/python -r requirements.txt -r requirements-dev.txt` (the venv for the table below was made outside the repository). `Running locally` above has the run commands.

| Step | Command | Result on 2026-11-22 |
|---|---|---|
| Format and lint | `ruff format --check . && ruff check .` (the shared pipeline installs ruff with the version it pins) | run (ruff 0.17.0, the pinned CI version, from a separate venv): all checks passed, 28 files already formatted |
| Unit tests | `pytest` (`pytest.ini` sets `pythonpath=src`; the tests that need stubs compile the protos into a temp dir). `pytest --cov=src` prints coverage (pytest-cov is a dev dependency, report only) | run: 140 passed, coverage 75% (`ranking.py` 100%, `server.py` 67%, `telemetry.py` 52%); `tests/test_lifecycle.py` 5 more times in a row: 43 passed each time |
| Proto stubs | the `grpc_tools.protoc` command in Running locally (what the Dockerfile does) | run by the image build (and by the test suite in a temp dir) |
| Security scan | `bandit -r src/ -c pyproject.toml` | run (bandit 1.9.4 from a separate venv): exit 0, no issues |
| Dependency audit | `pip-audit -r requirements.txt` | run (pip-audit 2.10.1): no known vulnerabilities found |
| Image | `docker build --platform linux/amd64 -t platform-demo/recommendation-service:local .` | run: built, 179 MB, user `10001:10001`, exec-form HEALTHCHECK healthy; `grpcurl list` shows `RecommendationService`, `Health` and reflection; `docker stop` with the OTLP and Pyroscope addresses pointed at an address that never answers: 2.4 s, exit 0 |
| Image scan | `trivy image --severity CRITICAL,HIGH --exit-code 1 platform-demo/recommendation-service:local` | run (Trivy 0.76.1): exit 0, no CRITICAL or HIGH findings |
| Secret scan | `pre-commit run --all-files` (`.pre-commit-config.yaml`, gitleaks) or `gitleaks git --redact` | run with the gitleaks binary of the pre-commit cache (`gitleaks git --redact`, full history, 65 commits): no leaks found |
| Workflow lint | `actionlint` | not run in this bundle (only the two `uses:` pins of the callers changed; run on 2026-11-21 for the earlier version: no findings) |
| Docs | `mkdocs build --strict` (mkdocs 1.6.1 + mkdocs-techdocs-core 1.7.1, Python 3.12 venv) and the link check `python3 -I tools/check-md-links.py .` from `platform-demo-workflows` (relative links in `README.md` and `docs/`) | run, both passed (12 relative links, 0 broken) |
| Whole shop | `local/up.sh`, `local/smoke.sh` (the local compose stack of the workspace, not part of this repository) | run with this image: `SMOKE PASSED` three times in a row (87 steps each); the product pages of the first four products show the recommendations section with 4 products |

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
└── docs/                # TechDocs pages (mkdocs.yml in the root), docs/adr/ = decisions
```
