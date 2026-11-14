# Container image

Multi-stage [`Dockerfile`](https://github.com/platform-boutique/app-recommendation-service/tree/main/Dockerfile):

| Stage | Base | What happens |
|---|---|---|
| builder | `python:3.12-alpine` | installs `g++`/`linux-headers`, `pip install -r requirements.txt` (fully pinned), **compiles the protos** with `grpc_tools.protoc` into `generated/` and patches the relative import in `recommendation_pb2_grpc.py` |
| runtime | `python:3.12-alpine` | copies site-packages, generated stubs (`/app/src/generated`), `src/`; runs as non-root |

- **Entrypoint:** `python src/server.py` (working dir `/app`).
- **User:** numeric `USER 100:101` (`appuser`/`appgroup`, created with these fixed ids; `id` in the container prints `uid=100(appuser) gid=101(appgroup)`). Matches the chart's `runAsUser: 100` / `runAsGroup: 101`.
- **EXPOSE:** `8080` only.
- **Docker `HEALTHCHECK`:** Python `grpc.channel_ready_future` against `localhost:${PORT:-8080}`.
- **Image size:** 175 MB uncompressed (`docker images`, local build on 2026-10-27; the earlier ~41 MB figure was not re-measured and is probably the compressed registry size).
- **Reproducibility:** the image installs from the fully pinned `requirements.txt` (compiled from `requirements.in` with `uv`), so transitive versions no longer float between builds. The lock was not regenerated in this change.
- **Build context:** [`.dockerignore`](https://github.com/platform-boutique/app-recommendation-service/tree/main/.dockerignore) keeps `.git`, tests, docs, caches and locally compiled stubs out of the context.
- The generated stubs are not in git; they only exist after the Docker build.
