%{!?site_version:%global site_version 1.0.0}
%{!?site_release:%global site_release 1}
%{!?custom_python:%global custom_python 0}
# Preserve the prepared runtime, including its absolute interpreter paths (DL-389).
%global debug_package %{nil}
%global __debug_install_post %{nil}
%global __os_install_post %{nil}
%global _build_id_links none

Name: synthetic-engine
Version: %{site_version}
Release: %{site_release}%{?dist}
Summary: Synthetic engine runtime and estate
License: AGPL-3.0-only
Source0: %{name}-%{version}.tar.gz
ExclusiveArch: x86_64
AutoReqProv: no
Requires: systemd bash coreutils util-linux shadow-utils tzdata glibc libgcc
Requires(pre): systemd shadow-utils coreutils
Requires(post): systemd coreutils
Requires(preun): systemd
Requires(postun): systemd
Requires(posttrans): systemd util-linux coreutils
%if ! %{custom_python}
Requires: python3.12 python3.12-pip
%endif

%description
Site packaging example. One RPM delivers a prepared runtime, main config,
JIL files and unit. Runtime state is created outside the package payload.

%prep
%setup -q

%build

%install
install -d '%{buildroot}/opt/synthetic-engine' '%{buildroot}/etc/synthetic-engine'
install -d '%{buildroot}/usr/lib/systemd/system'
cp -a runtime cfg '%{buildroot}/opt/synthetic-engine/'
install -m 0644 dsl41.toml '%{buildroot}/etc/synthetic-engine/dsl41.toml'
install -m 0644 synthetic-engine.service '%{buildroot}/usr/lib/systemd/system/'

%pre
set -eu
echo "synthetic-engine pre instances=$1"
# The new pre runs before payload replacement; the old preun runs too late (DL-474).
if [ "$1" -gt 1 ]; then
    systemctl stop synthetic-engine.service
    case "$(systemctl show -p ActiveState synthetic-engine.service)" in
        ActiveState=inactive|ActiveState=failed) ;;
        *) echo 'engine did not stop; refusing payload replacement' >&2; exit 1 ;;
    esac
fi
getent group synthetic-engine >/dev/null || groupadd -r synthetic-engine
getent passwd synthetic-engine >/dev/null || \
    useradd -r -g synthetic-engine -d /srv/synthetic-engine -s /sbin/nologin synthetic-engine

%post
set -eu
echo "synthetic-engine post instances=$1"
# Only a missing root is provisioned; existing engine evidence stays intact (DL-474).
if [ ! -e /srv/synthetic-engine/run ]; then
    install -d -o synthetic-engine -g synthetic-engine -m 0700 /srv/synthetic-engine/run
fi
systemctl daemon-reload
if [ "$1" -eq 1 ]; then
    systemctl enable synthetic-engine.service
fi

%preun
set -eu
echo "synthetic-engine preun instances=$1"
if [ "$1" -eq 0 ]; then
    systemctl stop synthetic-engine.service
    systemctl disable synthetic-engine.service
fi

%postun
set -eu
echo "synthetic-engine postun instances=$1"
systemctl daemon-reload

%posttrans
set -eu
echo "synthetic-engine posttrans instances=$1"
# Old files and old scriptlets are gone before this start (DL-474).
systemctl start synthetic-engine.service
if ! timeout -k 5 180 /bin/sh -c '
    until runuser -u synthetic-engine -- /opt/synthetic-engine/runtime/bin/dsl41 \
        query status --json --socket /srv/synthetic-engine/run/control.sock 2>/dev/null |
        /opt/synthetic-engine/runtime/bin/python3.12 -I -c \
            '\''import json,sys; sys.exit(json.load(sys.stdin).get("engine") != "RUNNING")'\'' \
            >/dev/null 2>&1; do
        if systemctl is-failed --quiet synthetic-engine.service; then exit 1; fi
        sleep 1
    done
'; then
    echo 'RUNNING not confirmed; engine may be running or starting on the stored estate; check status and journal; root retained' >&2
    exit 1
fi
# One request only: an uncertain answer requires operator diagnosis (DL-463).
if ! timeout -k 5 180 runuser -u synthetic-engine -- \
    /opt/synthetic-engine/runtime/bin/dsl41 update --wait \
    --socket /srv/synthetic-engine/run/control.sock; then
    echo 'estate update failed or uncertain; inspect status and the printed request ID; do not retry blindly' >&2
    exit 1
fi

%files
/opt/synthetic-engine/runtime
/opt/synthetic-engine/cfg
%dir /etc/synthetic-engine
%config(noreplace) /etc/synthetic-engine/dsl41.toml
/usr/lib/systemd/system/synthetic-engine.service
