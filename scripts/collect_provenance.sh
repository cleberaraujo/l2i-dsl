#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_PATH="$(realpath "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(dirname "$SCRIPT_PATH")"
REPO_DIR="$(realpath "$SCRIPT_DIR/..")"
NET_SRC_DIR="${NET_SRC_DIR:-$HOME/l2i-src}"
VENV_DIR="${VENV_DIR:-$HOME/l2i-dev/venv}"
PYTHON_REQUIREMENTS_LOCK="${PYTHON_REQUIREMENTS_LOCK:-$REPO_DIR/requirements/python-runtime.lock}"
OUTPUT_FILE="${1:-$REPO_DIR/results/provenance/environment-provenance.txt}"

mkdir -p "$(dirname "$OUTPUT_FILE")"

print_git_state() {
  local name="$1"
  local path="$2"

  echo "--- $name ---"
  if [[ -d "$path/.git" ]]; then
    echo "path=$path"
    echo "head=$(git -C "$path" rev-parse HEAD 2>/dev/null || echo unavailable)"
    echo "describe=$(git -C "$path" describe --always --dirty --tags 2>/dev/null || echo unavailable)"
    echo "status_begin"
    git -C "$path" status --short --untracked-files=all 2>/dev/null || true
    echo "status_end"
  else
    echo "path=$path"
    echo "state=not-cloned"
  fi
  echo
}

{
  echo "============================================================"
  echo "L2I ENVIRONMENT PROVENANCE"
  echo "============================================================"

  echo
  echo "=== CAPTURE ==="
  echo "timestamp_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "collector=$SCRIPT_PATH"

  echo
  echo "=== OPERATING SYSTEM ==="
  cat /etc/os-release 2>/dev/null || true
  uname -a 2>/dev/null || true

  echo
  echo "=== HARDWARE ALLOCATION ==="
  lscpu 2>/dev/null || true
  free -h 2>/dev/null || true
  df -hT / 2>/dev/null || true

  echo
  echo "=== TIME CONFIGURATION ==="
  timedatectl 2>/dev/null || true

  echo
  echo "=== DEPENDENCY LOCK ==="
  if [[ -f "$REPO_DIR/config/dependencies.env" ]]; then
    echo "source_lock_sha256=$(sha256sum "$REPO_DIR/config/dependencies.env" | awk '{print $1}')"
    cat "$REPO_DIR/config/dependencies.env"
  else
    echo "state=missing"
  fi

  echo
  echo "=== PYTHON DEPENDENCY LOCK ==="
  if [[ -f "$PYTHON_REQUIREMENTS_LOCK" ]]; then
    echo "path=$PYTHON_REQUIREMENTS_LOCK"
    echo "python_lock_sha256=$(sha256sum "$PYTHON_REQUIREMENTS_LOCK" | awk '{print $1}')"
    cat "$PYTHON_REQUIREMENTS_LOCK"
  else
    echo "state=missing"
    echo "expected_path=$PYTHON_REQUIREMENTS_LOCK"
  fi

  echo
  echo "=== L2I REPOSITORY ==="
  print_git_state "l2i-dsl" "$REPO_DIR"

  echo "=== EXTERNAL SOURCE REPOSITORIES ==="
  print_git_state "PI" "$NET_SRC_DIR/PI"
  print_git_state "behavioral-model" "$NET_SRC_DIR/behavioral-model"
  print_git_state "p4c" "$NET_SRC_DIR/p4c"
  print_git_state "sysrepo" "$NET_SRC_DIR/sysrepo"
  print_git_state "libnetconf2" "$NET_SRC_DIR/libnetconf2"
  print_git_state "Netopeer2" "$NET_SRC_DIR/Netopeer2"

  echo "=== SYSTEM TOOL VERSIONS ==="
  printf 'python3='; python3 --version 2>&1 || true
  printf 'gcc='; gcc --version 2>/dev/null | head -n 1 || true
  printf 'cmake='; cmake --version 2>/dev/null | head -n 1 || true
  printf 'protoc='; protoc --version 2>&1 || true
  printf 'libyang='; pkg-config --modversion libyang 2>/dev/null || true
  printf 'p4c='; p4c --version 2>&1 || true
  printf 'simple_switch_grpc='; simple_switch_grpc --version 2>&1 || true
  printf 'sysrepoctl='; sysrepoctl -V 2>&1 || true
  printf 'netopeer2-server='; netopeer2-server -V 2>&1 || true

  echo
  echo "=== RELEVANT DEBIAN PACKAGES ==="
  dpkg-query -W -f='${binary:Package}\t${Version}\n' 2>/dev/null \
    | grep -E '^(libyang|libprotobuf|protobuf|libgrpc|grpc|python3|cmake|gcc)' \
    | sort || true

  echo
  echo "=== PYTHON ENVIRONMENT ==="
  if [[ -x "$VENV_DIR/bin/python" ]]; then
    "$VENV_DIR/bin/python" --version 2>&1 || true
    "$VENV_DIR/bin/pip" freeze --all 2>/dev/null | sort || true
  else
    echo "state=not-created"
    echo "expected_path=$VENV_DIR"
  fi

  echo
  echo "=== NETWORK ==="
  ip -brief address 2>/dev/null || true
  ip route 2>/dev/null || true
} > "$OUTPUT_FILE"

sha256sum "$OUTPUT_FILE" > "${OUTPUT_FILE}.sha256"
printf 'Provenance written to %s\n' "$OUTPUT_FILE"
printf 'SHA-256 written to %s\n' "${OUTPUT_FILE}.sha256"
