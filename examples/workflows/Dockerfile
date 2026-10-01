FROM docker.io/library/python:3.14-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir uv==0.9.7
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE LICENSING.md COMMERCIAL.md THIRD_PARTY_LICENSES ./
COPY src ./src
COPY grammars ./grammars
RUN uv sync --frozen --no-dev
COPY examples/workflows/requirements.txt ./examples/workflows/requirements.txt
RUN uv pip install --python .venv/bin/python -r examples/workflows/requirements.txt
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 EXAMPLE_RUNS=/runs
# Copy only example source. Example run directories and bytecode stay outside the image.
COPY examples/workflows/support.py ./examples/workflows/support.py
COPY examples/fulfilment/*.py examples/fulfilment/*.jil examples/fulfilment/*.sql examples/fulfilment/README.md ./examples/fulfilment/
COPY examples/energy/*.py examples/energy/*.jil examples/energy/README.md ./examples/energy/
COPY examples/media/*.py examples/media/*.jil examples/media/README.md ./examples/media/
