ARG BASE
FROM ${BASE}
ARG TARGET
ARG APT_MIRROR
ARG RPM_MIRROR
ARG PYTHON_INDEX
RUN if [ "$TARGET" = el8 ]; then \
        if [ -n "$RPM_MIRROR" ]; then \
            sed -i -e 's|^mirrorlist=|#mirrorlist=|' \
                -e "s|^#baseurl=http://dl.rockylinux.org/\$contentdir|baseurl=$RPM_MIRROR|" \
                /etc/yum.repos.d/Rocky-*.repo; \
        fi; \
        dnf -y install systemd sudo python3.12 python3.12-pip rpm-build jq procps-ng \
            util-linux tar gzip findutils diffutils which shadow-utils git ca-certificates \
        && rpm -qa | sort >/opt/os-packages.txt && dnf clean all; \
    else \
        if [ -n "$APT_MIRROR" ]; then \
            sed -i "s|http://ports.ubuntu.com/ubuntu-ports/|$APT_MIRROR|g; s|http://archive.ubuntu.com/ubuntu/|$APT_MIRROR|g" \
                /etc/apt/sources.list.d/ubuntu.sources; \
        fi; \
        apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
            systemd systemd-sysv dbus sudo python3.12-venv python3-pip jq procps \
            util-linux ca-certificates git \
        && dpkg-query -W >/opt/os-packages.txt && rm -rf /var/lib/apt/lists/*; \
    fi \
    && python3.12 -I -c 'import venv, ensurepip' \
    && python3.12 -m venv /opt/uv \
    && /opt/uv/bin/pip install --index-url "$PYTHON_INDEX" uv==0.9.7 \
    && ln -s /opt/uv/bin/uv /usr/local/bin/uv \
    && useradd --create-home --shell /bin/bash runner \
    && echo 'runner ALL=(ALL) NOPASSWD:ALL' >/etc/sudoers.d/runner \
    && chmod 0440 /etc/sudoers.d/runner
CMD ["/sbin/init"]
