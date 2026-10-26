# The wargame host: the EL8 offline image with uv added, so the service drill
# can run on it (drill-local.sh DRILL_BASE_IMAGE). The offline image itself
# keeps uv out for the rehearsal lanes. scripts/wargame.sh builds both.
ARG BASE
FROM ${BASE}
ARG PYTHON_INDEX=https://pypi.org/simple
# the uv drill-local.sh pins; tests/test_nightbank_deploy.py holds the two equal
RUN python3.12 -m venv /opt/uv \
    && /opt/uv/bin/pip install --index-url "$PYTHON_INDEX" uv==0.9.7 \
    && ln -s /opt/uv/bin/uv /usr/local/bin/uv
CMD ["/sbin/init"]
