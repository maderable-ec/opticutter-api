# syntax=docker/dockerfile:1

# ---- Stage 1: builder ----
# Compiles production dependencies inside an isolated virtualenv.
FROM python:3.11-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build toolchain only in the builder (does not reach the final image).
# fonts-dejavu-core: TrueType font for the cutting diagram (Pillow). The dev
# stage inherits from builder, so it stays available for development/tests.
RUN apt-get update -y && \
    apt-get install -y --no-install-recommends build-essential fonts-dejavu-core && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Virtualenv copied to the following stages.
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /src

COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install -r requirements.txt

# ---- Stage 2: rustbuild ----
# The native packing kernel (rust/): a maturin/PyO3 extension that replaces the
# interpreted geometry of src/cutting/ (the packer, the strip constructor and
# the gen_fills loop) with a byte-identical Rust transliteration. It is a pure
# speedup — the Python path stays in the image as a fallback and produces the
# same layouts — so nothing here touches ENGINE_VERSION or the cache hash.
#
# Its own stage on purpose: the Rust toolchain is ~1GB, and neither `dev` nor
# `runtime` should inherit it. Only the built wheel crosses over.
FROM builder AS rustbuild

ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH=/usr/local/cargo/bin:$PATH

RUN apt-get update -y && \
    apt-get install -y --no-install-recommends curl ca-certificates && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | \
    sh -s -- -y --profile minimal --default-toolchain 1.83.0

COPY rust/ ./rust/

# --compatibility linux skips auditwheel: the wheel is installed into this
# image's own venv, never redistributed, so a manylinux tag buys nothing.
RUN pip install maturin==1.7.8 && \
    maturin build --release --manifest-path rust/Cargo.toml \
        --compatibility linux --out /wheels

# ---- Stage 3: venv ----
# The production virtualenv, now carrying the native kernel.
FROM builder AS venv

COPY --from=rustbuild /wheels /wheels
RUN pip install --no-deps /wheels/*.whl && rm -rf /wheels

# ---- Stage 4: dev ----
# Image for development/tests: adds ruff, pytest, etc. on top of the venv.
# docker-compose builds this target (build.target: dev).
# requirements.txt already landed in /src from the builder, so the
# "-r requirements.txt" inside requirements_dev.txt resolves correctly.
FROM venv AS dev

ENV PYTHONUNBUFFERED=1

COPY requirements_dev.txt .
RUN pip install -r requirements_dev.txt

EXPOSE 8000

# Overridden by docker-compose with --reload; a sensible default lives here.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ---- Stage 5: runtime (production, default target) ----
FROM python:3.11-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

WORKDIR /src

# fonts-dejavu-core: TrueType font for the cutting diagram (Pillow). The slim
# image ships no fonts; without this Pillow falls back to its bitmap default
# and breaks accented characters and the × symbol.
RUN apt-get update -y && \
    apt-get install -y --no-install-recommends fonts-dejavu-core && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Copies the already-built virtualenv (production deps + the native kernel, no
# toolchain). The kernel lives in the venv and NOT under /src on purpose:
# `make dev` and `make benchmark` bind-mount the repo over /src, which would
# shadow anything built there.
COPY --from=venv /opt/venv /opt/venv

# Unprivileged user.
RUN useradd --create-home --uid 1000 appuser

COPY . .
# Data dirs (anexos + print spool): created owned by appuser so a fresh named
# volume mounted at either path inherits writable ownership (uid 1000).
RUN mkdir -p /src/uploads /src/print_spool && chown -R appuser:appuser /src

USER appuser

EXPOSE 8000

# Liveness probe for the orchestrator. The slim image ships neither curl nor
# wget, so the check goes through the Python stdlib. /health needs no auth and
# does not touch the database.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).getcode()==200 else 1)"]

# Production command (no --reload). One worker: the right count is host-specific,
# so deployments override this (opticutter-infra's compose.yml runs --workers 2).
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
