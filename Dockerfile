# syntax=docker/dockerfile:1
ARG BUILDARCH
FROM scratch AS vp-amd64
ADD --checksum=sha256:a3a728816a5937fbfd06d9987c8d8a1152c6a90de22a827b840079140021d030 https://github.com/voidzero-dev/vite-plus/releases/download/v0.3.1/vp-x86_64-unknown-linux-gnu.tar.gz /vp.tar.gz
FROM scratch AS vp-arm64
ADD --checksum=sha256:5af89c1118f402a71063ca0d2737a58ca61054b8996f4f6c87511700e7a6b4cb https://github.com/voidzero-dev/vite-plus/releases/download/v0.3.1/vp-aarch64-unknown-linux-gnu.tar.gz /vp.tar.gz
FROM vp-${BUILDARCH} AS vp-bin

FROM --platform=$BUILDPLATFORM node:24.21.0-bookworm-slim AS frontend
COPY --from=vp-bin /vp.tar.gz /tmp/vp.tar.gz
RUN mkdir /opt/vp && tar -xzf /tmp/vp.tar.gz -C /opt/vp
ENV PATH="/opt/vp:${PATH}"
WORKDIR /web
RUN vp env off
COPY web/package.json web/pnpm-lock.yaml ./
RUN vp install --frozen-lockfile
COPY web/ ./
# Bound native checker concurrency independently of the build host CPU count.
RUN GOMAXPROCS=2 GOMEMLIMIT=512MiB RAYON_NUM_THREADS=2 vp check && vp test run && vp build

FROM python:3.12-slim AS runtime
WORKDIR /app
ENV CODEBUDDY_AUTH_DIR=/data/auth
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY VERSION ./
COPY converter.py ./
COPY app/ ./app/
COPY --from=frontend /web/dist/ ./web/dist/
EXPOSE 8787
CMD ["python3", "converter.py", "--host", "0.0.0.0", "--port", "8787", "--skip-check"]
