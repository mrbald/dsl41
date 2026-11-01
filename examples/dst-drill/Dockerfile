# The DST drill's image: dsl41, libfaketime and the drill. Build it from the
# repository root:
#   docker build -f examples/dst-drill/Dockerfile -t dsl41-dst-drill .
# The base is pinned by its multi-arch index digest, so amd64 and arm64 hosts
# pull their own manifest.
FROM docker.io/library/python:3.12-slim-bookworm@sha256:34386ef0cb081344d7ec1c103ba398e6e9f64e9ab3a1509accc92a4e24a07258

RUN apt-get update && apt-get install -y --no-install-recommends libfaketime procps tzdata \
    && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir uv==0.9.7
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE LICENSING.md COMMERCIAL.md THIRD_PARTY_LICENSES ./
COPY src ./src
COPY grammars ./grammars
RUN uv sync --frozen --no-dev
COPY examples/dst-drill ./examples/dst-drill
# the runner and its jobs run as an ordinary user, as in production
RUN useradd --create-home --uid 1001 drill
USER drill
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
CMD ["python", "examples/dst-drill/drill.py"]
