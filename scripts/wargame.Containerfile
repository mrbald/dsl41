# The wargame host: a Linux container with systemd as PID 1, Python 3.12 with venv and pip,
# jq, procps and sudo, and `runner`, an unprivileged user with passwordless sudo, as on a
# GitHub runner. scripts/wargame.sh builds it on one of two bases, Ubuntu 24.04 or Rocky
# Linux 8 (systemd 239, the RHEL 8 family); TARGET names the package manager's family.
ARG BASE
FROM ${BASE}
ARG TARGET
RUN case "$TARGET" in \
        el8) dnf -y install systemd sudo python3.12 python3.12-pip jq procps-ng util-linux \
                 tar findutils shadow-utils tzdata ca-certificates \
             && dnf clean all ;; \
        ubuntu) apt-get update -q \
             && DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends \
                 systemd systemd-sysv dbus sudo python3.12 python3.12-venv jq procps \
                 util-linux tzdata ca-certificates \
             && rm -rf /var/lib/apt/lists/* ;; \
        *) echo "TARGET must be ubuntu or el8, not '$TARGET'" >&2; exit 2 ;; \
    esac \
    && python3.12 -I -c 'import venv, ensurepip' \
    && useradd --create-home --shell /bin/bash runner \
    && echo 'runner ALL=(ALL) NOPASSWD:ALL' >/etc/sudoers.d/runner \
    && chmod 0440 /etc/sudoers.d/runner
CMD ["/sbin/init"]
