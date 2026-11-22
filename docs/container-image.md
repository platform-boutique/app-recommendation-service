# Container image

Multi-stage [`Dockerfile`](https://github.com/platform-boutique/app-recommendation-service/tree/main/Dockerfile):

| Stage | Base | What happens |
|---|---|---|
| builder | `python:3.12-alpine` | installs `g++`/`linux-headers`, `pip install -r requirements.txt` (fully pinned), **compiles the protos** with `grpc_tools.protoc` into `generated/` and patches the relative import in `recommendation_pb2_grpc.py` |
| runtime | `python:3.12-alpine` | copies site-packages, generated stubs (`/app/src/generated`), `src/`; runs as non-root |

- **Entrypoint:** `python src/server.py` (working dir `/app`).
- **User:** numeric `USER 10001:10001` (`appuser`/`appgroup`, created with these fixed ids; `id` in the container prints `uid=10001(appuser) gid=10001(appgroup)`). Build args `SERVICE_VERSION` (default `dev`) and `SERVICE_COMMIT` (default `unknown`) set the OCI labels `version`/`revision` (plus `source`, `licenses=Apache-2.0`) and the ENV of the same names.
- **EXPOSE:** `8080` only.
- **Docker `HEALTHCHECK`:** exec form (no shell): Python `grpc.channel_ready_future` against `localhost:$PORT`, 8080 when `PORT` is unset or empty.
- **Image size:** 179 MB uncompressed (`docker images`, local build on 2026-11-22; the earlier ~41 MB figure was not re-measured and is probably the compressed registry size).
- **Reproducibility:** the image installs from the fully pinned `requirements.txt` (compiled from `requirements.in` with `uv`), so transitive versions no longer float between builds. The lock was not regenerated in this change.
- **Build context:** [`.dockerignore`](https://github.com/platform-boutique/app-recommendation-service/tree/main/.dockerignore) keeps `.git`, tests, docs, caches and locally compiled stubs out of the context.
- The generated stubs are not in git; they only exist after the Docker build.
