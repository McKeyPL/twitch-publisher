#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
WITH_DEV=false
SKIP_SYSTEM=false
SKIP_BROWSERS=false
RECORDINGS_DIRECTORY="${RECORDINGS_ROOT:-$PROJECT_DIR/recordings}"

color() {
    local code="$1"
    shift
    if [[ -t 1 ]]; then
        printf '\033[%sm%s\033[0m\n' "$code" "$*"
    else
        printf '%s\n' "$*"
    fi
}

usage() {
    cat <<'EOF'
Usage: ./install.sh [options]

Supported families:
  Ubuntu 22.04+ and Debian 12+ (official Playwright Linux targets)
  RHEL 8+ and CentOS Stream 8+ (best-effort Playwright compatibility)

Options:
  --recordings-root PATH  Initial absolute recording directory
  --with-dev              Install requirements-dev.txt as well
  --skip-system           Do not install operating-system packages
  --skip-browsers         Do not install Playwright browser binaries
  -h, --help              Show this help
EOF
}

while (($#)); do
    case "$1" in
        --recordings-root)
            [[ $# -ge 2 ]] || { color 31 "[ERROR] --recordings-root requires a path."; exit 2; }
            RECORDINGS_DIRECTORY="$2"
            shift 2
            ;;
        --with-dev)
            WITH_DEV=true
            shift
            ;;
        --skip-system)
            SKIP_SYSTEM=true
            shift
            ;;
        --skip-browsers)
            SKIP_BROWSERS=true
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            color 31 "[ERROR] Unknown option: $1"
            usage
            exit 2
            ;;
    esac
done

[[ "$RECORDINGS_DIRECTORY" = /* ]] || {
    color 31 "[ERROR] --recordings-root must be an absolute POSIX path."
    exit 2
}

if [[ ! -r /etc/os-release ]]; then
    color 31 "[ERROR] /etc/os-release is unavailable; cannot detect the distribution."
    exit 3
fi

# shellcheck disable=SC1091
source /etc/os-release
DISTRO_ID="${ID,,}"
DISTRO_LIKE="${ID_LIKE:-}"
DISTRO_MAJOR="${VERSION_ID%%.*}"
PACKAGE_FAMILY=""

if [[ "$DISTRO_ID" =~ ^(ubuntu|debian)$ || "$DISTRO_LIKE" == *debian* ]]; then
    PACKAGE_FAMILY="apt"
elif [[ "$DISTRO_ID" =~ ^(rhel|centos|rocky|almalinux)$ || "$DISTRO_LIKE" == *rhel* || "$DISTRO_LIKE" == *fedora* ]]; then
    PACKAGE_FAMILY="rpm"
else
    color 31 "[ERROR] Unsupported distribution: ${PRETTY_NAME:-$DISTRO_ID}."
    exit 3
fi

if [[ "$PACKAGE_FAMILY" == "apt" ]]; then
    if [[ "$DISTRO_ID" == "debian" && "$DISTRO_MAJOR" -lt 12 ]]; then
        color 31 "[ERROR] Debian 12 or newer is required."
        exit 3
    fi
    if [[ "$DISTRO_ID" == "ubuntu" && "$DISTRO_MAJOR" -lt 22 ]]; then
        color 31 "[ERROR] Ubuntu 22.04 or newer is required."
        exit 3
    fi
elif [[ "$DISTRO_MAJOR" -lt 8 ]]; then
    color 31 "[ERROR] RHEL/CentOS 8 or newer is required; CentOS 7 is end-of-life."
    exit 3
fi

if ((EUID == 0)); then
    SUDO=()
elif command -v sudo >/dev/null 2>&1; then
    SUDO=(sudo)
else
    color 31 "[ERROR] Root privileges are required for system packages, but sudo is unavailable."
    exit 4
fi

install_apt_packages() {
    "${SUDO[@]}" apt-get update
    local firefox_package="firefox"
    [[ "$DISTRO_ID" == "debian" ]] && firefox_package="firefox-esr"
    "${SUDO[@]}" apt-get install -y \
        ca-certificates curl ffmpeg "$firefox_package" \
        python3 python3-pip python3-venv

    if [[ "$DISTRO_ID" == "ubuntu" && "$DISTRO_MAJOR" -eq 22 ]] && \
       ! command -v python3.11 >/dev/null 2>&1; then
        color 33 "[WARNING] Ubuntu 22.04 needs Python 3.11 from the deadsnakes PPA."
        "${SUDO[@]}" apt-get install -y software-properties-common
        "${SUDO[@]}" add-apt-repository -y ppa:deadsnakes/ppa
        "${SUDO[@]}" apt-get update
        "${SUDO[@]}" apt-get install -y python3.11 python3.11-venv
    fi
}

install_rpm_packages() {
    local manager
    manager="$(command -v dnf || command -v yum || true)"
    [[ -n "$manager" ]] || { color 31 "[ERROR] Neither dnf nor yum is available."; exit 4; }

    color 33 "[WARNING] Playwright does not officially support RHEL/CentOS; installation is best-effort."
    "${SUDO[@]}" "$manager" install -y \
        "https://dl.fedoraproject.org/pub/epel/epel-release-latest-${DISTRO_MAJOR}.noarch.rpm"
    "${SUDO[@]}" "$manager" install -y \
        "https://download1.rpmfusion.org/free/el/rpmfusion-free-release-${DISTRO_MAJOR}.noarch.rpm"
    "${SUDO[@]}" "$manager" install -y \
        python3.11 python3.11-pip ffmpeg firefox ca-certificates curl \
        alsa-lib atk at-spi2-atk cairo cups-libs dbus-libs expat fontconfig \
        freetype glib2 gtk3 libdrm libX11 libXcomposite libXdamage libXext \
        libXfixes libXrandr libXtst libxcb libxkbcommon mesa-libgbm nspr nss \
        pango liberation-fonts
}

if ! $SKIP_SYSTEM; then
    color 36 "Installing system packages for ${PRETTY_NAME:-$DISTRO_ID}..."
    if [[ "$PACKAGE_FAMILY" == "apt" ]]; then
        install_apt_packages
    else
        install_rpm_packages
    fi
fi

PYTHON_BIN=""
for candidate in python3.13 python3.12 python3.11 python3; do
    if command -v "$candidate" >/dev/null 2>&1 && \
       "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))'; then
        PYTHON_BIN="$(command -v "$candidate")"
        break
    fi
done

if [[ -z "$PYTHON_BIN" ]]; then
    color 31 "[ERROR] Python 3.11 or newer was not found after package installation."
    exit 5
fi

color 36 "Using $($PYTHON_BIN --version 2>&1) at $PYTHON_BIN"
cd "$PROJECT_DIR"

if [[ ! -x .venv/bin/python ]]; then
    "$PYTHON_BIN" -m venv .venv
fi

.venv/bin/python -m pip install --upgrade pip setuptools wheel
.venv/bin/python -m pip install --upgrade -r requirements.txt
if $WITH_DEV; then
    .venv/bin/python -m pip install --upgrade -r requirements-dev.txt
fi

if ! $SKIP_BROWSERS; then
    if [[ "$PACKAGE_FAMILY" == "apt" && ! $SKIP_SYSTEM ]]; then
        "${SUDO[@]}" .venv/bin/python -m playwright install-deps firefox chromium
    fi
    .venv/bin/python -m playwright install firefox chromium
fi

mkdir -p "$RECORDINGS_DIRECTORY" logs auth data
if [[ ! -f .env ]]; then
    {
        printf 'RECORDINGS_ROOT=%s\n' "$RECORDINGS_DIRECTORY"
        grep -v '^RECORDINGS_ROOT=' .env.example
    } >.env
    chmod 600 .env
    color 32 "Created .env with RECORDINGS_ROOT=$RECORDINGS_DIRECTORY"
else
    color 33 "[WARNING] Existing .env was preserved. Verify RECORDINGS_ROOT manually."
fi

chmod +x start.sh install.sh
color 32 "Installation completed successfully."
color 33 "Configure credentials and RUMBLE_LICENSE_OPTION in .env before the first upload."
color 36 "Start manually with: ./start.sh --browser-debug"
