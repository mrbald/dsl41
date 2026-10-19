# Delivery files belong to RPM; prepared runtimes belong to the operator (DL-389).
%{!?dsl41_version:%{error:Define dsl41_version from the delivery manifest}}
%{!?dsl41_release:%global dsl41_release 1}
%{!?dsl41_prefix:%global dsl41_prefix /opt/dsl41}
%{!?dsl41_profile:%global dsl41_profile base}
%{!?dsl41_python:%global dsl41_python /usr/bin/python3.12}
# Hash-bound delivery data admits no RPM build-time rewriting (DL-389).
%global debug_package %{nil}
%global __debug_install_post %{nil}
%global __os_install_post %{nil}

Name:           dsl41-offline
Version:        %{dsl41_version}
Release:        %{dsl41_release}%{?dist}
Summary:        Target-qualified offline dsl41 delivery
License:        AGPL-3.0-only
URL:            https://github.com/mrbald/dsl41
Source0:        dsl41-offline-%{version}.tar.gz
ExclusiveArch:  x86_64 aarch64
AutoReqProv:    no
Requires:       bash
Requires:       python3.12
Requires:       python3.12-pip

%description
The exact dsl41 release wheel, locked binary dependency wheels, preparation
recipe, and versioned deployment templates. Upstream dependency license files
remain in their wheels. Installation prepares a runtime without activating it.

%prep
%setup -q -n dsl41-offline-%{version}
case '%{dsl41_prefix}' in /*) ;; *) echo 'dsl41_prefix must be absolute' >&2; exit 1;; esac
case '%{dsl41_profile}' in base|ui) ;; *) echo 'dsl41_profile must be base or ui' >&2; exit 1;; esac

%build

%install
mkdir -p '%{buildroot}%{dsl41_prefix}/delivery/%{version}-%{release}'
cp -a . '%{buildroot}%{dsl41_prefix}/delivery/%{version}-%{release}/'

%check
%{dsl41_python} -I tools/bundle.py candidate . /tmp/dsl41-rpm-check '%{dsl41_profile}'
%{dsl41_python} -I tools/bundle.py candidate \
    '%{buildroot}%{dsl41_prefix}/delivery/%{version}-%{release}' \
    /tmp/dsl41-rpm-payload-check '%{dsl41_profile}'

%post
# A failed preparation leaves its candidate for inspection; activation is separate (DL-389).
PYTHON='%{dsl41_python}' /bin/bash \
    '%{dsl41_prefix}/delivery/%{version}-%{release}/tools/prepare.sh' \
    '%{dsl41_prefix}/delivery/%{version}-%{release}' '%{dsl41_prefix}' '%{dsl41_profile}'

%files
%{dsl41_prefix}/delivery/%{version}-%{release}
