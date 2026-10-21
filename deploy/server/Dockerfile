# The base image is taken by the 3.12-slim tag: pinning it by `@sha256:…` is possible only
# with registry access (`docker buildx imagetools inspect python:3.12-slim`), and it is
# the owner's step at deployment.
FROM python:3.12-slim

WORKDIR /app

# Dependencies are pinned from `uv.lock` (see the comment in requirements.txt).
COPY deploy/server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Only the server code and the package `__init__`: nothing extra from the repository root
# should get in here (`.env`, builds, recordings); `.dockerignore` in the root leaves
# exactly these three paths.
COPY src/argus/__init__.py /app/argus/__init__.py
COPY src/argus/server /app/argus/server

ENV PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1
EXPOSE 8421

# Is the server alive: `/pricing` answers without a login and without calls to paid
# services.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8421/pricing', timeout=4)"

# Not as root. The volume with the database already exists on the server and belongs to
# root (the previous container created it), and a regular user cannot write there, so
# startup begins as root with a single command "hand the database to the user" and
# immediately drops privileges (`setpriv` from util-linux, present in the base image): the
# server itself runs as 10001 and without supplementary groups.
# `sh -c`, not a separate file: a file with Windows line endings (CRLF) would not run on
# Linux.
ENTRYPOINT ["sh", "-c", "mkdir -p /app/data && chown -R 10001:10001 /app/data && exec setpriv --reuid=10001 --regid=10001 --clear-groups \"$@\"", "--"]
# WebSocket frame limits (audit of 2026-10-16): by default a message can be 16 MB, the
# queue holds 32, and it is also compressed, so a dozen open recognition sessions would
# take the container down on memory. The largest honest frame is a 35-second utterance
# (`relay_stt.MAX_APPEND_S`, about 2.2 MB in base64); frame compression does not help
# audio and opens the door to "decompression bombs".
CMD ["uvicorn", "argus.server.app:from_env", "--factory", "--host", "0.0.0.0", "--port", "8421", "--ws-max-size", "2500000", "--ws-max-queue", "2", "--ws-per-message-deflate", "false", "--timeout-keep-alive", "10"]
