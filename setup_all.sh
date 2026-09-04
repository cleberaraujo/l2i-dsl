#!/usr/bin/env bash
# ============================================================
# setup_all.sh — Bootstrap idempotente (CTA / SeloR)
#
# Objetivos:
# - detectar corretamente a raiz do repositório l2i-dsl
# - usar diretórios locais fora do repositório:
#     ~/l2i-dsl
#     ~/l2i-src
#     ~/l2i-dev/venv
# - compilar dependências por código-fonte quando necessário
# - configurar NETCONF completo (usuário, chave, YANG, NACM)
# - subir serviços reais
# - reaplicar o pipeline P4 ao iniciar os serviços
# - usar sempre o Python da venv
# - limpar topologias temporárias automaticamente ao fim de S1 e S2
# - manter comportamento idempotente
# ============================================================
set -Eeuo pipefail

# -------------------------------
# logging e utilidades
# -------------------------------
info(){ echo "[info] $*"; }
warn(){ echo "[warn] $*"; }
err(){ echo "[erro] $*" >&2; }

run(){
  info "$*"
  if [[ "${DRY_RUN:-0}" == "1" ]]; then
    return 0
  fi
  "$@"
}

need_cmd() {
  command -v "$1" >/dev/null 2>&1 || { err "Comando obrigatório não encontrado: $1"; exit 1; }
}

realpath_safe() {
  python3 - <<'PY' "$1"
import os, sys
print(os.path.realpath(sys.argv[1]))
PY
}

find_repo_root() {
  local start="$1"
  local d
  d="$(realpath_safe "$start")"
  while [[ "$d" != "/" ]]; do
    if [[ -d "$d/l2i" && -d "$d/scenarios" && -d "$d/scripts" ]]; then
      echo "$d"
      return 0
    fi
    d="$(dirname "$d")"
  done
  return 1
}

SCRIPT_PATH="$(realpath_safe "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(dirname "$SCRIPT_PATH")"

DEFAULT_REPO_DIR="$HOME/l2i-dsl"

if [[ -n "${L2I_REPO_DIR:-}" && -d "${L2I_REPO_DIR}/l2i" && -d "${L2I_REPO_DIR}/scenarios" && -d "${L2I_REPO_DIR}/scripts" ]]; then
  REPO_DIR="$(realpath_safe "$L2I_REPO_DIR")"
elif REPO_DIR="$(find_repo_root "$SCRIPT_DIR")"; then
  :
elif REPO_DIR="$(find_repo_root "$PWD")"; then
  :
elif [[ -d "$DEFAULT_REPO_DIR/l2i" && -d "$DEFAULT_REPO_DIR/scenarios" && -d "$DEFAULT_REPO_DIR/scripts" ]]; then
  REPO_DIR="$(realpath_safe "$DEFAULT_REPO_DIR")"
else
  err "Não foi possível localizar a raiz do repositório."
  err "Defina L2I_REPO_DIR=/caminho/para/l2i-dsl se necessário."
  exit 1
fi

DEPENDENCY_LOCK="${DEPENDENCY_LOCK:-$REPO_DIR/config/dependencies.env}"
if [[ ! -f "$DEPENDENCY_LOCK" ]]; then
  err "Arquivo canônico de dependências não encontrado: $DEPENDENCY_LOCK"
  exit 1
fi
# shellcheck disable=SC1090
source "$DEPENDENCY_LOCK"

require_dependency_lock() {
  local name value
  local commit_variables=(
    L2I_PI_REF
    L2I_BMV2_REF
    L2I_P4C_REF
    L2I_SYSREPO_REF
    L2I_LIBNETCONF2_REF
    L2I_NETOPEER2_REF
  )
  local version_variables=(
    L2I_LIBYANG_UPSTREAM_VERSION
    L2I_PROTOBUF_UPSTREAM_VERSION
    L2I_GRPC_UPSTREAM_VERSION
  )

  for name in "${commit_variables[@]}"; do
    value="${!name:-}"
    [[ "$value" =~ ^[0-9a-f]{40}$ ]] || {
      err "Revisão inválida ou ausente em $DEPENDENCY_LOCK: $name=${value:-<vazio>}"
      exit 1
    }
  done

  for name in "${version_variables[@]}"; do
    value="${!name:-}"
    [[ -n "$value" ]] || {
      err "Versão ausente em $DEPENDENCY_LOCK: $name"
      exit 1
    }
  done

  for name in L2I_P4C_ENABLE_BMV2 L2I_P4C_ENABLE_EBPF; do
    value="${!name:-}"
    [[ "$value" == "ON" || "$value" == "OFF" ]] || {
      err "Perfil p4c inválido em $DEPENDENCY_LOCK: $name=${value:-<vazio>}"
      exit 1
    }
  done
}

require_dependency_lock

require_repo_layout() {
  [[ -d "$REPO_DIR/l2i" && -d "$REPO_DIR/scenarios" && -d "$REPO_DIR/scripts" ]] || {
    err "Layout do repositório inválido em $REPO_DIR."
    exit 1
  }
  [[ -d "$REPO_DIR/specs" ]] || { err "Diretório specs não encontrado em $REPO_DIR."; exit 1; }
  [[ -d "$REPO_DIR/yang" ]] || { err "Diretório yang não encontrado em $REPO_DIR."; exit 1; }
}

# -------------------------------
# configuração geral
# -------------------------------
HOME_DIR="$HOME"
NET_SRC_DIR="${NET_SRC_DIR:-$HOME_DIR/l2i-src}"
DEV_DIR="${DEV_DIR:-$HOME_DIR/l2i-dev}"
VENV_DIR="${VENV_DIR:-$DEV_DIR/venv}"

MAKE_JOBS="${MAKE_JOBS:-$(nproc 2>/dev/null || echo 2)}"
DRY_RUN="${DRY_RUN:-0}"

NETCONF_USER="${NETCONF_USER:-netconf}"
NETCONF_HOME="${NETCONF_HOME:-/var/lib/netconf}"
NETCONF_KEY="${NETCONF_KEY:-$HOME_DIR/.ssh/l2i_netconf_key}"
NETCONF_LISTEN_ADDRESS="${NETCONF_LISTEN_ADDRESS:-127.0.0.1}"
NETCONF_ENDPOINT_NAME="${NETCONF_ENDPOINT_NAME:-default-ssh}"

PYTHON_BIN="${PYTHON_BIN:-$VENV_DIR/bin/python}"
PIP_BIN="${PIP_BIN:-$VENV_DIR/bin/pip}"
PYTHON_REQUIREMENTS_LOCK="${PYTHON_REQUIREMENTS_LOCK:-$REPO_DIR/requirements/python-runtime.lock}"

NETCONF_PORT="${NETCONF_PORT:-830}"
NETCONF_PIDFILE="${NETCONF_PIDFILE:-/tmp/netopeer2-server.pid}"
NETCONF_LOG_FILE="${NETCONF_LOG_FILE:-/tmp/netopeer2-server.log}"
P4_PORT="${P4_PORT:-9559}"
P4_THRIFT_PORT="${P4_THRIFT_PORT:-9090}"
P4_REQUIRE_THRIFT="${P4_REQUIRE_THRIFT:-0}"
P4_GRPC_ADDR="${P4_GRPC_ADDR:-0.0.0.0:${P4_PORT}}"
P4_ADDR="${P4_ADDR:-127.0.0.1:${P4_PORT}}"

RESULTS_DIR="${RESULTS_DIR:-$REPO_DIR/results}"
BUILD_DIR_SYSREPO="${BUILD_DIR_SYSREPO:-$NET_SRC_DIR/build-sysrepo}"
BUILD_DIR_LIBNETCONF2="${BUILD_DIR_LIBNETCONF2:-$NET_SRC_DIR/build-libnetconf2}"
BUILD_DIR_NETOPEER2="${BUILD_DIR_NETOPEER2:-$NET_SRC_DIR/build-Netopeer2}"
BUILD_DIR_PI="${BUILD_DIR_PI:-$NET_SRC_DIR/build-PI}"
BUILD_DIR_BMV2="${BUILD_DIR_BMV2:-$NET_SRC_DIR/build-behavioral-model}"
BUILD_DIR_P4C="${BUILD_DIR_P4C:-$NET_SRC_DIR/build-p4c}"

SYSREPO_REPO="${SYSREPO_REPO:-https://github.com/sysrepo/sysrepo.git}"
LIBNETCONF2_REPO="${LIBNETCONF2_REPO:-https://github.com/CESNET/libnetconf2.git}"
NETOPEER2_REPO="${NETOPEER2_REPO:-https://github.com/CESNET/Netopeer2.git}"
PI_REPO="${PI_REPO:-https://github.com/p4lang/PI.git}"
BMV2_REPO="${BMV2_REPO:-https://github.com/p4lang/behavioral-model.git}"
P4C_REPO="${P4C_REPO:-https://github.com/p4lang/p4c.git}"

# Canonical immutable revisions. Environment overrides are accepted for
# controlled compatibility experiments, but every effective value is recorded
# by the provenance collector.
PI_REF="${PI_REF:-$L2I_PI_REF}"
BMV2_REF="${BMV2_REF:-$L2I_BMV2_REF}"
P4C_REF="${P4C_REF:-$L2I_P4C_REF}"
P4C_ENABLE_BMV2="${P4C_ENABLE_BMV2:-$L2I_P4C_ENABLE_BMV2}"
P4C_ENABLE_EBPF="${P4C_ENABLE_EBPF:-$L2I_P4C_ENABLE_EBPF}"
P4C_FORCE_REBUILD="${P4C_FORCE_REBUILD:-0}"
SYSREPO_REF="${SYSREPO_REF:-$L2I_SYSREPO_REF}"
LIBNETCONF2_REF="${LIBNETCONF2_REF:-$L2I_LIBNETCONF2_REF}"
NETOPEER2_REF="${NETOPEER2_REF:-$L2I_NETOPEER2_REF}"

export DEBIAN_FRONTEND=noninteractive
export PKG_CONFIG_PATH="/usr/local/lib/pkgconfig:/usr/local/lib64/pkgconfig:${PKG_CONFIG_PATH:-}"
export CMAKE_PREFIX_PATH="/usr/local:${CMAKE_PREFIX_PATH:-}"
export LD_LIBRARY_PATH="/usr/local/lib:/usr/local/lib64:${LD_LIBRARY_PATH:-}"

# -------------------------------
# helpers de sistema
# -------------------------------
ensure_dirs() {
  run mkdir -p "$NET_SRC_DIR" "$DEV_DIR" "$RESULTS_DIR" "$HOME_DIR/.ssh"
}

sudo_keep_env() {
  if [[ "$DRY_RUN" == "1" ]]; then
    info "sudo -E $*"
    return 0
  fi
  sudo -E "$@"
}

append_if_missing() {
  local line="$1"
  local file="$2"
  if [[ ! -f "$file" ]] || ! grep -Fqx "$line" "$file"; then
    printf '%s\n' "$line" | sudo tee -a "$file" >/dev/null
  fi
}

clone_or_update_git() {
  local repo_url="$1"
  local repo_dir="$2"
  local repo_ref="$3"

  if [[ ! -d "$repo_dir/.git" ]]; then
    run git clone "$repo_url" "$repo_dir"
  fi

  run git -C "$repo_dir" fetch --all --tags --prune

  if ! git -C "$repo_dir" cat-file -e "${repo_ref}^{commit}" 2>/dev/null; then
    err "Revisão imutável não encontrada em $repo_dir: $repo_ref"
    exit 1
  fi

  run git -C "$repo_dir" checkout --detach "$repo_ref"
  run git -C "$repo_dir" submodule update --init --recursive

  local effective_ref
  effective_ref="$(git -C "$repo_dir" rev-parse HEAD)"
  [[ "$effective_ref" == "$repo_ref" ]] || {
    err "Revisão efetiva divergente em $repo_dir: esperado=$repo_ref obtido=$effective_ref"
    exit 1
  }
}

cmake_build_install() {
  local src_dir="$1"
  local build_dir="$2"
  shift 2

  run mkdir -p "$build_dir"
  run cmake -S "$src_dir" -B "$build_dir" "$@"
  run cmake --build "$build_dir" -j"$MAKE_JOBS"
  run sudo cmake --install "$build_dir"
  run sudo ldconfig
}

autotools_build_install() {
  local src_dir="$1"
  shift
  run bash -lc "cd '$src_dir' && ./autogen.sh"
  run bash -lc "cd '$src_dir' && ./configure $*"
  run bash -lc "cd '$src_dir' && make -j'$MAKE_JOBS'"
  run bash -lc "cd '$src_dir' && sudo make install"
  run sudo ldconfig
}

port_listening() {
  local port="$1"
  local listeners
  listeners="$(ss -H -ltn "sport = :$port" 2>/dev/null)" || return 2
  [[ -n "$listeners" ]]
}

assert_system_tools() {
  need_cmd git
  need_cmd cmake
  need_cmd pkg-config
  need_cmd protoc
  need_cmd python3
  need_cmd ssh-keygen
  need_cmd ss
}

assert_repo_files() {
  require_repo_layout
  [[ -f "$REPO_DIR/yang/l2i-qos.yang" ]] || { err "Arquivo YANG ausente: $REPO_DIR/yang/l2i-qos.yang"; exit 1; }
  [[ -f "$REPO_DIR/specs/valid/s1_unicast_qos.json" ]] || { err "Spec S1 ausente."; exit 1; }
  [[ -f "$REPO_DIR/specs/valid/s2_multicast_source_oriented.json" ]] || { err "Spec S2 ausente."; exit 1; }
  [[ -f "$REPO_DIR/scripts/p4_push_pipeline.py" ]] || { err "Script p4_push_pipeline.py ausente."; exit 1; }
  [[ -f "$REPO_DIR/scripts/p4_build_and_run.sh" ]] || { err "Script p4_build_and_run.sh ausente."; exit 1; }
}

# -------------------------------
# pacotes base
# -------------------------------
preseed_iperf3() {
  local selection="iperf3 iperf3/start_daemon boolean false"

  info "Configurando iperf3 para não iniciar automaticamente como daemon."
  if [[ "$DRY_RUN" == "1" ]]; then
    info "printf '%s\n' '$selection' | sudo debconf-set-selections"
    return 0
  fi

  command -v debconf-set-selections >/dev/null 2>&1 || {
    err "debconf-set-selections não está disponível."
    exit 1
  }
  printf '%s\n' "$selection" | sudo debconf-set-selections
}

apt_base() {
  preseed_iperf3
  run sudo env DEBIAN_FRONTEND=noninteractive apt-get update
  run sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y \
    ca-certificates curl git rsync \
    build-essential cmake ninja-build pkg-config \
    autoconf automake libtool libtool-bin \
    python3 python3-dev python3-venv python3-pip \
    iproute2 iputils-ping net-tools iperf3 fping graphviz ethtool \
    openssh-client openssl \
    protobuf-compiler protobuf-compiler-grpc \
    libprotobuf-dev libprotobuf-c-dev protobuf-c-compiler \
    libyang2-dev libyang2-tools \
    libssh-dev libssl-dev libcurl4-openssl-dev libpcre2-dev \
    libavl-dev libev-dev libsqlite3-dev libsystemd-dev \
    libboost-dev libboost-system-dev libboost-filesystem-dev \
    libboost-program-options-dev libboost-thread-dev \
    libboost-test-dev libboost-iostreams-dev libboost-graph-dev \
    libboost-regex-dev \
    libfl-dev libgc-dev bison flex libreadline-dev libgmp-dev libpcap-dev \
    thrift-compiler libthrift-dev libnanomsg-dev \
    libgrpc++-dev libgrpc-dev
}

# -------------------------------
# python / venv
# -------------------------------
verify_python_lock() {
  [[ -f "$PYTHON_REQUIREMENTS_LOCK" ]] || {
    err "Lock Python ausente: $PYTHON_REQUIREMENTS_LOCK"
    exit 1
  }

  run "$PYTHON_BIN" - "$PYTHON_REQUIREMENTS_LOCK" <<'PY'
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import sys

lock = Path(sys.argv[1])
errors = []
checked = 0

for raw_line in lock.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#"):
        continue
    if "==" not in line:
        errors.append(f"unsupported lock entry: {line}")
        continue
    name, expected = line.split("==", 1)
    try:
        installed = version(name)
    except PackageNotFoundError:
        errors.append(f"missing: {name}=={expected}")
        continue
    checked += 1
    if installed != expected:
        errors.append(
            f"version mismatch: {name} expected={expected} installed={installed}"
        )

if errors:
    raise SystemExit("\n".join(errors))

print(f"PYTHON_LOCK_OK packages={checked}")
PY
}

python_env() {
  ensure_dirs

  if [[ -x "$PYTHON_BIN" ]] && ! "$PYTHON_BIN" -V >/dev/null 2>&1; then
    warn "Venv inválida detectada em $VENV_DIR. Recriando."
    run rm -rf "$VENV_DIR"
  fi

  if [[ ! -x "$PYTHON_BIN" ]]; then
    run python3 -m venv "$VENV_DIR"
  fi

  [[ -f "$PYTHON_REQUIREMENTS_LOCK" ]] || {
    err "Lock Python ausente: $PYTHON_REQUIREMENTS_LOCK"
    exit 1
  }

  run "$PIP_BIN" install \
    --disable-pip-version-check \
    --requirement "$PYTHON_REQUIREMENTS_LOCK"
  run "$PIP_BIN" check
  verify_python_lock

  run "$PYTHON_BIN" - <<'PY'
import cryptography
import grpc
import ncclient
import paramiko
import yaml
from p4.v1 import p4runtime_pb2
from p4runtime_sh.shell import P4RuntimeClient
print("PYTHON_ENV_OK")
PY
}

# -------------------------------
# builds por código-fonte
# -------------------------------
verify_system_dependency_versions() {
  local libyang_version protobuf_version grpc_version

  libyang_version="$(pkg-config --modversion libyang 2>/dev/null || true)"
  protobuf_version="$(protoc --version 2>/dev/null | awk '{print $2}' || true)"
  grpc_version="$(dpkg-query -W -f='${Version}' libgrpc-dev 2>/dev/null | sed 's/-.*//' || true)"

  [[ "$libyang_version" == "$L2I_LIBYANG_UPSTREAM_VERSION" ]] || {
    err "libyang incompatível: esperado=$L2I_LIBYANG_UPSTREAM_VERSION obtido=${libyang_version:-ausente}"
    exit 1
  }
  [[ "$protobuf_version" == "$L2I_PROTOBUF_UPSTREAM_VERSION" ]] || {
    err "Protocol Buffers incompatível: esperado=$L2I_PROTOBUF_UPSTREAM_VERSION obtido=${protobuf_version:-ausente}"
    exit 1
  }
  [[ "$grpc_version" == "$L2I_GRPC_UPSTREAM_VERSION" ]] || {
    err "gRPC incompatível: esperado=$L2I_GRPC_UPSTREAM_VERSION obtido=${grpc_version:-ausente}"
    exit 1
  }

  info "Dependências de sistema verificadas: libyang=$libyang_version protobuf=$protobuf_version grpc=$grpc_version"
}

build_sysrepo() {
  local src="$NET_SRC_DIR/sysrepo"
  clone_or_update_git "$SYSREPO_REPO" "$src" "$SYSREPO_REF"
  cmake_build_install "$src" "$BUILD_DIR_SYSREPO" \
    -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_EXAMPLES=OFF \
    -DGEN_LANGUAGE_BINDINGS=OFF
}

build_libnetconf2() {
  local src="$NET_SRC_DIR/libnetconf2"
  clone_or_update_git "$LIBNETCONF2_REPO" "$src" "$LIBNETCONF2_REF"
  cmake_build_install "$src" "$BUILD_DIR_LIBNETCONF2" \
    -DCMAKE_BUILD_TYPE=Release \
    -DENABLE_SSH=ON \
    -DENABLE_TLS=OFF
}

build_netopeer2() {
  local src="$NET_SRC_DIR/Netopeer2"
  clone_or_update_git "$NETOPEER2_REPO" "$src" "$NETOPEER2_REF"
  cmake_build_install "$src" "$BUILD_DIR_NETOPEER2" \
    -DCMAKE_BUILD_TYPE=Release
}

build_netconf() {
  verify_system_dependency_versions

  if command -v netopeer2-server >/dev/null 2>&1 && command -v sysrepoctl >/dev/null 2>&1; then
    info "Stack NETCONF já presente — pulando build."
    return 0
  fi

  build_sysrepo
  build_libnetconf2
  build_netopeer2
}

build_pi() {
  local src="$NET_SRC_DIR/PI"
  if command -v simple_switch_grpc >/dev/null 2>&1 && pkg-config --exists libpi >/dev/null 2>&1; then
    info "PI/BMv2 já aparentam estar instalados — pulando build do PI."
    return 0
  fi

  clone_or_update_git "$PI_REPO" "$src" "$PI_REF"
  run mkdir -p "$BUILD_DIR_PI"
  run bash -lc "cd '$src' && ./autogen.sh"
  run bash -lc "cd '$BUILD_DIR_PI' && '$src/configure' --with-proto"
  run bash -lc "cd '$BUILD_DIR_PI' && make -j'$MAKE_JOBS'"
  run bash -lc "cd '$BUILD_DIR_PI' && sudo make install"
  run sudo ldconfig
}

build_bmv2() {
  local src="$NET_SRC_DIR/behavioral-model"
  if command -v simple_switch_grpc >/dev/null 2>&1; then
    info "simple_switch_grpc já encontrado — pulando build do BMv2."
    return 0
  fi

  clone_or_update_git "$BMV2_REPO" "$src" "$BMV2_REF"
  run bash -lc "cd '$src' && ./install_deps.sh || true"
  autotools_build_install "$src" --with-pi
}

p4c_profile_matches() {
  [[ -f "$BUILD_DIR_P4C/CMakeCache.txt" ]] \
    && grep -Fqx "ENABLE_BMV2:BOOL=$P4C_ENABLE_BMV2" "$BUILD_DIR_P4C/CMakeCache.txt" \
    && grep -Fqx "ENABLE_EBPF:BOOL=$P4C_ENABLE_EBPF" "$BUILD_DIR_P4C/CMakeCache.txt"
}

build_p4c() {
  local src="$NET_SRC_DIR/p4c"

  [[ "$P4C_FORCE_REBUILD" == "0" || "$P4C_FORCE_REBUILD" == "1" ]] || {
    err "P4C_FORCE_REBUILD deve ser 0 ou 1: $P4C_FORCE_REBUILD"
    exit 1
  }

  if command -v p4c >/dev/null 2>&1 \
      && [[ "$P4C_FORCE_REBUILD" != "1" ]] \
      && p4c_profile_matches; then
    info "p4c já encontrado com o perfil canônico — pulando build."
    info "Use P4C_FORCE_REBUILD=1 para forçar a reconstrução."
    return 0
  fi

  if command -v p4c >/dev/null 2>&1 && ! p4c_profile_matches; then
    warn "p4c instalado com perfil divergente; reconfigurando o build canônico."
  fi

  clone_or_update_git "$P4C_REPO" "$src" "$P4C_REF"
  cmake_build_install "$src" "$BUILD_DIR_P4C" \
    -DCMAKE_BUILD_TYPE=Release \
    -DENABLE_GTESTS=OFF \
    -DENABLE_P4TEST=OFF \
    -DENABLE_BMV2="$P4C_ENABLE_BMV2" \
    -DENABLE_EBPF="$P4C_ENABLE_EBPF"

  if [[ "$DRY_RUN" != "1" ]]; then
    grep -Fqx "ENABLE_BMV2:BOOL=$P4C_ENABLE_BMV2" "$BUILD_DIR_P4C/CMakeCache.txt" || {
      err "Perfil p4c divergente: ENABLE_BMV2"
      exit 1
    }
    grep -Fqx "ENABLE_EBPF:BOOL=$P4C_ENABLE_EBPF" "$BUILD_DIR_P4C/CMakeCache.txt" || {
      err "Perfil p4c divergente: ENABLE_EBPF"
      exit 1
    }
  fi
}

build_p4() {
  if command -v simple_switch_grpc >/dev/null 2>&1 \
      && command -v p4c >/dev/null 2>&1 \
      && p4c_profile_matches \
      && [[ "$P4C_FORCE_REBUILD" != "1" ]]; then
    info "Stack P4 já instalada com o perfil canônico — pulando build."
    return 0
  fi

  build_pi
  build_bmv2
  build_p4c
}

# -------------------------------
# netconf
# -------------------------------
write_nacm_file() {
  local nacm_file="$REPO_DIR/l2i-nacm-netconf-permit.xml"
  cat > "$nacm_file" <<XML
<nacm xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-acm">
  <enable-nacm>true</enable-nacm>
  <read-default>permit</read-default>
  <write-default>permit</write-default>
  <exec-default>permit</exec-default>
  <groups>
    <group>
      <name>netconf-group</name>
      <user-name>${NETCONF_USER}</user-name>
    </group>
  </groups>
  <rule-list>
    <name>netconf-all-l2i</name>
    <group>netconf-group</group>
    <rule>
      <name>permit-l2i-qos-all</name>
      <module-name>l2i-qos</module-name>
      <access-operations>*</access-operations>
      <action>permit</action>
    </rule>
  </rule-list>
</nacm>
XML
}

ensure_netopeer2_hostkey() {
  local hostkey_script="$NET_SRC_DIR/Netopeer2/scripts/merge_hostkey.sh"
  local sysrepocfg_bin openssl_bin hostkey_state

  [[ -f "$hostkey_script" ]] || {
    err "Script de provisionamento da host key não encontrado: $hostkey_script"
    exit 1
  }

  sysrepocfg_bin="$(command -v sysrepocfg || true)"
  openssl_bin="$(command -v openssl || true)"
  [[ -n "$sysrepocfg_bin" ]] || {
    err "sysrepocfg não encontrado. Execute build_netconf antes."
    exit 1
  }
  [[ -n "$openssl_bin" ]] || {
    err "openssl não encontrado."
    exit 1
  }

  run sudo env \
    SYSREPOCFG_EXECUTABLE="$sysrepocfg_bin" \
    OPENSSL_EXECUTABLE="$openssl_bin" \
    bash "$hostkey_script"

  if [[ "$DRY_RUN" != "1" ]]; then
    hostkey_state="$(
      sudo "$sysrepocfg_bin" -X \
        -x "/ietf-keystore:keystore/asymmetric-keys/asymmetric-key[name='genkey']/name" \
        2>/dev/null || true
    )"
    grep -Fq "genkey" <<<"$hostkey_state" || {
      err "A host key genkey não foi encontrada no ietf-keystore."
      exit 1
    }
  fi
}

write_netconf_user_auth_file() {
  local auth_file="$REPO_DIR/l2i-netconf-user-auth.xml"
  local client_key_algorithm client_key_data

  [[ -f "$NETCONF_KEY.pub" ]] || {
    err "Chave pública NETCONF ausente: $NETCONF_KEY.pub"
    exit 1
  }

  read -r client_key_algorithm client_key_data _ < "$NETCONF_KEY.pub"
  [[ -n "$client_key_algorithm" && -n "$client_key_data" ]] || {
    err "Formato inválido da chave pública NETCONF: $NETCONF_KEY.pub"
    exit 1
  }

  cat > "$auth_file" <<XML
<netconf-server xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-server">
  <listen>
    <endpoint>
      <name>${NETCONF_ENDPOINT_NAME}</name>
      <ssh>
        <tcp-server-parameters>
          <local-address>${NETCONF_LISTEN_ADDRESS}</local-address>
          <local-port>${NETCONF_PORT}</local-port>
        </tcp-server-parameters>
        <ssh-server-parameters>
          <server-identity>
            <host-key>
              <name>default-key</name>
              <public-key>
                <keystore-reference>genkey</keystore-reference>
              </public-key>
            </host-key>
          </server-identity>
          <client-authentication>
            <supported-authentication-methods>
              <publickey/>
            </supported-authentication-methods>
            <users>
              <user>
                <name>${NETCONF_USER}</name>
                <authorized-key>
                  <name>l2i-client-key</name>
                  <algorithm>${client_key_algorithm}</algorithm>
                  <key-data>${client_key_data}</key-data>
                </authorized-key>
              </user>
            </users>
          </client-authentication>
        </ssh-server-parameters>
      </ssh>
    </endpoint>
  </listen>
</netconf-server>
XML
}

apply_netconf_user_auth() {
  local auth_file="$REPO_DIR/l2i-netconf-user-auth.xml"
  ensure_netopeer2_hostkey
  write_netconf_user_auth_file
  run sudo sysrepocfg --edit="$auth_file" -d running -f xml -m ietf-netconf-server
  run sudo sysrepocfg --edit="$auth_file" -d startup -f xml -m ietf-netconf-server
}

configure_netconf() {
  require_repo_layout
  assert_repo_files

  if ! id -u "$NETCONF_USER" >/dev/null 2>&1; then
    run sudo useradd --system --shell /usr/sbin/nologin \
      --home-dir "$NETCONF_HOME" --create-home "$NETCONF_USER"
  else
    info "Usuário NETCONF já existe: $NETCONF_USER"
    run sudo usermod --shell /usr/sbin/nologin --home "$NETCONF_HOME" "$NETCONF_USER"
  fi

  run mkdir -p "$(dirname "$NETCONF_KEY")"
  if [[ ! -f "$NETCONF_KEY" ]]; then
    run ssh-keygen -t rsa -b 2048 -f "$NETCONF_KEY" -N ""
  fi

  run sudo mkdir -p "$NETCONF_HOME/.ssh"
  run sudo install -o "$NETCONF_USER" -g "$NETCONF_USER" -m 700 -d "$NETCONF_HOME/.ssh"
  run sudo install -o "$NETCONF_USER" -g "$NETCONF_USER" -m 600 "$NETCONF_KEY.pub" "$NETCONF_HOME/.ssh/authorized_keys"
  run sudo chown -R "$NETCONF_USER:$NETCONF_USER" "$NETCONF_HOME"

  info "Provisionando a identidade SSH do Netopeer2 no ietf-keystore."
  info "Aplicando listener NETCONF canônico em ${NETCONF_LISTEN_ADDRESS}:${NETCONF_PORT}."

  if ! command -v sysrepoctl >/dev/null 2>&1; then
    err "sysrepoctl não encontrado. Execute build_netconf antes."
    exit 1
  fi

  if ! sudo sysrepoctl -l | awk '{print $1}' | grep -Fxq "l2i-qos"; then
    run sudo sysrepoctl -i "$REPO_DIR/yang/l2i-qos.yang" -s "$REPO_DIR/yang"
  else
    info "Módulo YANG l2i-qos já instalado."
  fi

  apply_netconf_user_auth

  write_nacm_file
  run sudo sysrepocfg --import="$REPO_DIR/l2i-nacm-netconf-permit.xml" -f xml -d running -m ietf-netconf-acm
  run sudo sysrepocfg --import="$REPO_DIR/l2i-nacm-netconf-permit.xml" -f xml -d startup -m ietf-netconf-acm

  info "NETCONF configurado com usuário=$NETCONF_USER, chave=$NETCONF_KEY e módulo l2i-qos."
}

NETCONF_SYSTEMD_UNIT="netopeer2-server.service"
NETCONF_PROC_ROOT="${NETCONF_PROC_ROOT:-/proc}"
NETCONF_STABILITY_NS=2000000000
NETCONF_READINESS_TIMEOUT_NS=60000000000
NETCONF_UNIT_TIMEOUT_NS=30000000000
NETCONF_STOP_TIMEOUT_NS=10000000000
NETCONF_RESIDUAL_PID_TIMEOUT_NS=1000000000
NETCONF_RESIDUAL_PID_POLL_INTERVAL=0.05
NETCONF_BOOTSTRAP_MODULES=(ietf-keystore ietf-netconf-acm ietf-netconf-server)
declare -A NETCONF_CERTIFIED_STARTUP_SHA256=(
  [ietf-keystore]="b44726600d70ff3b653d92c764d5bbc8a03a94dba2d5e702641639283e736f25"
  [ietf-netconf-acm]="0a8a1b531211ecc7765e104e2a64fc8843444b1051533398990aa961a4201252"
  [ietf-netconf-server]="48dbb26be547f3b246769f156fe007feec0cd1e545ace4e5a256352f1d7eb77b"
)
NETCONF_PRIMARY_ERROR=""
NETCONF_START_ATTEMPTED=0

netconf_fail() {
  local code="$1"
  shift
  NETCONF_PRIMARY_ERROR="$*"
  err "$NETCONF_PRIMARY_ERROR"
  return "$code"
}

netconf_module_installed() {
  local module="$1"
  sudo -n sysrepoctl -l 2>/dev/null \
    | awk -F'|' -v wanted="$module" '
        NR > 2 {
          name=$1; flags=$3
          gsub(/^[[:space:]]+|[[:space:]]+$/, "", name)
          gsub(/[[:space:]]/, "", flags)
          if (name == wanted && flags ~ /I/) found=1
        }
        END { exit(found ? 0 : 1) }
      '
}

netconf_python_ready() {
  [[ -x "$PYTHON_BIN" ]] && "$PYTHON_BIN" -c 'import ncclient' >/dev/null 2>&1
}

netconf_snapshot() {
  local module="$1"
  local datastore="$2"
  local snapshot
  local raw_hash
  local normalized_hash
  snapshot="$(mktemp "${TMPDIR:-/tmp}/l2i-netconf-snapshot.XXXXXX")" || return 1
  if ! sudo -n sysrepocfg -X -d "$datastore" -m "$module" >"$snapshot"; then
    rm -f -- "$snapshot"
    return 1
  fi
  raw_hash="$(sha256sum "$snapshot" | awk '{print $1}')"
  if [[ -s "$snapshot" ]]; then
    if ! normalized_hash="$($PYTHON_BIN -c '
import pathlib, sys, xml.etree.ElementTree as ET
text = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
sys.stdout.write(ET.canonicalize(text, strip_text=True))
' "$snapshot" | sha256sum | awk '{print $1}')"; then
      rm -f -- "$snapshot"
      return 1
    fi
  else
    normalized_hash="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
  fi
  rm -f -- "$snapshot"
  printf '%s %s\n' "$raw_hash" "$normalized_hash"
}

netconf_main_pid() {
  systemctl show "$NETCONF_SYSTEMD_UNIT" --property=MainPID --value 2>/dev/null
}

netconf_expected_executable() {
  local expected="${NETCONF_EXPECTED_EXECUTABLE:-}"
  [[ -n "$expected" ]] || expected="$(command -v netopeer2-server 2>/dev/null || true)"
  [[ -n "$expected" ]] || return 1
  readlink -e -- "$expected" 2>/dev/null
}

netconf_pid_executable() {
  local pid="${1:-}"
  local allow_privileged="${2:-1}"
  local pid_dir link output executable line_count total_bytes nonnul_bytes
  [[ "$pid" =~ ^[1-9][0-9]*$ && "$pid" != "1" ]] || return 1
  [[ "$allow_privileged" == "0" || "$allow_privileged" == "1" ]] || return 1
  pid_dir="$NETCONF_PROC_ROOT/$pid"
  link="$pid_dir/exe"
  [[ -d "$pid_dir" ]] || return 2
  output="$(mktemp "${TMPDIR:-/tmp}/l2i-netconf-pid-exe.XXXXXX")" || return 1
  if readlink -e -- "$link" >"$output" 2>/dev/null; then
    :
  elif [[ ! -d "$pid_dir" ]]; then
    rm -f -- "$output"
    return 2
  elif [[ "$allow_privileged" == "0" ]]; then
    rm -f -- "$output"
    return 1
  elif sudo -n readlink -f -- "$link" >"$output" 2>/dev/null; then
    :
  elif [[ ! -d "$pid_dir" ]]; then
    rm -f -- "$output"
    return 2
  else
    rm -f -- "$output"
    return 1
  fi
  if [[ ! -d "$pid_dir" ]]; then
    rm -f -- "$output"
    return 2
  fi
  line_count="$(wc -l < "$output")"
  total_bytes="$(wc -c < "$output")"
  nonnul_bytes="$(LC_ALL=C tr -d '\000' < "$output" | wc -c)"
  if [[ "$line_count" != "1" || "$total_bytes" != "$nonnul_bytes" ]]; then
    rm -f -- "$output"
    return 1
  fi
  IFS= read -r executable < "$output" || {
    rm -f -- "$output"
    return 1
  }
  rm -f -- "$output"
  [[ -n "$executable" && "$executable" == /* && "$executable" != *$'\n'* ]] || return 1
  printf '%s\n' "$executable"
}

netconf_unit_control_group() {
  systemctl show "$NETCONF_SYSTEMD_UNIT" --property=ControlGroup --value 2>/dev/null
}

netconf_pid_control_group() {
  local pid="${1:-}"
  local hierarchy controllers group
  [[ -r "$NETCONF_PROC_ROOT/$pid/cgroup" ]] || return 1
  while IFS=: read -r hierarchy controllers group; do
    [[ -n "$group" ]] || continue
    printf '%s\n' "$group"
  done < "$NETCONF_PROC_ROOT/$pid/cgroup"
}

netconf_pid_start_time() {
  local pid="${1:-}"
  local stat_line rest start_time
  local -a stat_fields=()
  [[ "$pid" =~ ^[1-9][0-9]*$ && "$pid" != "1" ]] || return 1
  [[ -r "$NETCONF_PROC_ROOT/$pid/stat" ]] || return 1
  IFS= read -r stat_line < "$NETCONF_PROC_ROOT/$pid/stat" || return 1
  rest="${stat_line##*) }"
  [[ "$rest" != "$stat_line" ]] || return 1
  read -r -a stat_fields <<< "$rest"
  ((${#stat_fields[@]} >= 20)) || return 1
  start_time="${stat_fields[19]}"
  [[ "$start_time" =~ ^[0-9]+$ ]] || return 1
  printf '%s\n' "$start_time"
}

netconf_pid_cgroup_matches_unit() {
  local pid="${1:-}"
  local unit_group pid_group
  unit_group="$(netconf_unit_control_group)" || return 1
  [[ -n "$unit_group" && "$unit_group" != "/" ]] || return 1
  while IFS= read -r pid_group; do
    [[ "$pid_group" == "$unit_group" || "$pid_group" == "$unit_group/"* ]] && return 0
  done < <(netconf_pid_control_group "$pid")
  return 1
}

netconf_pid_matching_unit_group() {
  local pid="${1:-}"
  local unit_group pid_group
  unit_group="$(netconf_unit_control_group)" || return 1
  [[ -n "$unit_group" && "$unit_group" != "/" ]] || return 1
  while IFS= read -r pid_group; do
    if [[ "$pid_group" == "$unit_group" || "$pid_group" == "$unit_group/"* ]]; then
      printf '%s\n' "$pid_group"
      return 0
    fi
  done < <(netconf_pid_control_group "$pid")
  return 1
}

netconf_process_identity() {
  local pid="${1:-}"
  local executable group
  executable="$(netconf_pid_executable "$pid")" || return 1
  group="$(netconf_pid_control_group "$pid")" || return 1
  printf '%s|%s|%s\n' "$pid" "$executable" "$group"
}

netconf_pid_valid() {
  local pid="${1:-}"
  local executable expected main_pid load_state
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] || return 1
  [[ "$pid" != "1" ]] || return 1
  load_state="$(systemctl show "$NETCONF_SYSTEMD_UNIT" --property=LoadState --value 2>/dev/null || true)"
  [[ "$load_state" == "loaded" ]] || return 1
  main_pid="$(netconf_main_pid)"
  [[ "$main_pid" == "$pid" ]] || return 1
  sudo -n kill -0 "$pid" 2>/dev/null || return 1
  executable="$(netconf_pid_executable "$pid")" || return 1
  expected="$(netconf_expected_executable)" || return 1
  [[ "${executable##*/}" == "netopeer2-server" && "$executable" == "$expected" ]] || return 1
  netconf_pid_cgroup_matches_unit "$pid"
}

netconf_capture_process_fingerprint() {
  local pid="${1:-}"
  local executable group start_time
  netconf_pid_valid "$pid" || return 1
  executable="$(netconf_pid_executable "$pid")" || return 1
  group="$(netconf_pid_matching_unit_group "$pid")" || return 1
  start_time="$(netconf_pid_start_time "$pid")" || return 1
  printf '%s\n%s\n%s\n%s\n' "$pid" "$executable" "$group" "$start_time"
}

netconf_process_fingerprint_valid() {
  local fingerprint="${1:-}"
  local pid executable group start_time current_executable current_start_time pid_groups pid_group
  local -a fields=()
  mapfile -t fields <<< "$fingerprint"
  ((${#fields[@]} == 4)) || return 1
  pid="${fields[0]}"
  executable="${fields[1]}"
  group="${fields[2]}"
  start_time="${fields[3]}"
  [[ "$pid" =~ ^[1-9][0-9]*$ && "$pid" != "1" ]] || return 1
  [[ -n "$executable" && "${executable##*/}" == "netopeer2-server" ]] || return 1
  [[ -n "$group" && "$group" != "/" && "$start_time" =~ ^[0-9]+$ ]] || return 1
  sudo -n kill -0 "$pid" 2>/dev/null || return 1
  current_executable="$(netconf_pid_executable "$pid")" || return 1
  [[ "$current_executable" == "$executable" ]] || return 1
  current_start_time="$(netconf_pid_start_time "$pid")" || return 1
  [[ "$current_start_time" == "$start_time" ]] || return 1
  pid_groups="$(netconf_pid_control_group "$pid")" || return 1
  while IFS= read -r pid_group; do
    [[ "$pid_group" == "$group" ]] && return 0
  done <<< "$pid_groups"
  return 1
}

netconf_unit_active_running() {
  local active sub pid
  active="$(systemctl show "$NETCONF_SYSTEMD_UNIT" --property=ActiveState --value 2>/dev/null || true)"
  sub="$(systemctl show "$NETCONF_SYSTEMD_UNIT" --property=SubState --value 2>/dev/null || true)"
  pid="$(netconf_main_pid)"
  [[ "$active" == "active" && "$sub" == "running" ]] && netconf_pid_valid "$pid"
}

netconf_pid_kthread_state() {
  local pid="${1:-}" pid_dir status_path status line state="" count=0
  pid_dir="$NETCONF_PROC_ROOT/$pid"
  status_path="$pid_dir/status"
  [[ "$pid" =~ ^[1-9][0-9]*$ && "$pid" != "1" ]] || return 1

  if status="$(cat -- "$status_path" 2>/dev/null)"; then
    :
  else
    [[ ! -d "$pid_dir" ]] && return 2
    if status="$(sudo -n cat -- "$status_path" 2>/dev/null)"; then
      :
    else
      [[ ! -d "$pid_dir" ]] && return 2
      return 1
    fi
  fi
  [[ -d "$pid_dir" ]] || return 2

  while IFS= read -r line; do
    case "$line" in
      $'Kthread:\t0') state=0; count=$((count + 1)) ;;
      $'Kthread:\t1') state=1; count=$((count + 1)) ;;
      Kthread:*) return 1 ;;
    esac
  done <<< "$status"
  (( count == 1 )) || return 1
  printf '%s\n' "$state"
}

NETCONF_RESIDUAL_ENUMERATED_COUNT=0
NETCONF_RESIDUAL_CANONICAL_COUNT=0
NETCONF_RESIDUAL_NONCANONICAL_COUNT=0
NETCONF_RESIDUAL_KERNEL_THREAD_COUNT=0
NETCONF_RESIDUAL_GONE_COUNT=0
NETCONF_RESIDUAL_UNRESOLVED_COUNT=0
NETCONF_RESIDUAL_REEVALUATED_PID_COUNT=0
NETCONF_RESIDUAL_REEVALUATION_COUNT=0

netconf_residual_reset_metrics() {
  NETCONF_RESIDUAL_ENUMERATED_COUNT=0
  NETCONF_RESIDUAL_CANONICAL_COUNT=0
  NETCONF_RESIDUAL_NONCANONICAL_COUNT=0
  NETCONF_RESIDUAL_KERNEL_THREAD_COUNT=0
  NETCONF_RESIDUAL_GONE_COUNT=0
  NETCONF_RESIDUAL_UNRESOLVED_COUNT=0
  NETCONF_RESIDUAL_REEVALUATED_PID_COUNT=0
  NETCONF_RESIDUAL_REEVALUATION_COUNT=0
}

netconf_residual_poll_sleep() {
  sleep "$NETCONF_RESIDUAL_PID_POLL_INTERVAL"
}

netconf_classify_residual_pid() {
  local pid="${1:-}" expected="${2:-}" pid_dir
  local start_ns deadline_ns now_ns start_before="" start_after=""
  local executable="" kthread="" status=0 attempt=0
  pid_dir="$NETCONF_PROC_ROOT/$pid"
  NETCONF_RESIDUAL_PID_CLASS="unresolved"
  [[ "$pid" =~ ^[1-9][0-9]*$ && "$pid" != "1" ]] || return 0
  [[ -n "$expected" && "$expected" == /* && "$expected" != *$'\n'* ]] || return 0
  start_ns="$(netconf_monotonic_ns)" || return 0
  deadline_ns=$((start_ns + NETCONF_RESIDUAL_PID_TIMEOUT_NS))
  now_ns="$start_ns"

  while :; do
    ((now_ns <= deadline_ns)) || return 0
    attempt=$((attempt + 1))
    if ((attempt > 1)); then
      NETCONF_RESIDUAL_REEVALUATION_COUNT=$((NETCONF_RESIDUAL_REEVALUATION_COUNT + 1))
      if ((attempt == 2)); then
        NETCONF_RESIDUAL_REEVALUATED_PID_COUNT=$((NETCONF_RESIDUAL_REEVALUATED_PID_COUNT + 1))
      fi
    fi

    if [[ ! -d "$pid_dir" ]]; then
      NETCONF_RESIDUAL_PID_CLASS="gone"
      return 0
    fi

    start_before=""
    kthread=""
    executable=""
    if start_before="$(netconf_pid_start_time "$pid")"; then
      :
    elif [[ ! -d "$pid_dir" ]]; then
      NETCONF_RESIDUAL_PID_CLASS="gone"
      return 0
    fi

    if kthread="$(netconf_pid_kthread_state "$pid")"; then
      :
    else
      status=$?
      if ((status == 2)) || [[ ! -d "$pid_dir" ]]; then
        NETCONF_RESIDUAL_PID_CLASS="gone"
        return 0
      fi
      kthread=""
    fi

    if executable="$(netconf_pid_executable "$pid")"; then
      :
    else
      status=$?
      if ((status == 2)) || [[ ! -d "$pid_dir" ]]; then
        NETCONF_RESIDUAL_PID_CLASS="gone"
        return 0
      fi
      executable=""
    fi

    if start_after="$(netconf_pid_start_time "$pid")"; then
      :
    elif [[ ! -d "$pid_dir" ]]; then
      NETCONF_RESIDUAL_PID_CLASS="gone"
      return 0
    else
      start_after=""
    fi

    if [[ ! -d "$pid_dir" ]]; then
      NETCONF_RESIDUAL_PID_CLASS="gone"
      return 0
    fi

    if [[ -n "$start_before" && -n "$start_after" && "$start_before" == "$start_after" ]]; then
      if [[ "$kthread" == "1" ]]; then
        NETCONF_RESIDUAL_PID_CLASS="kernel-thread"
        return 0
      fi
      if [[ "$kthread" == "0" && -n "$executable" && "$executable" == /* \
         && "$executable" != "$NETCONF_PROC_ROOT" \
         && "$executable" != "$NETCONF_PROC_ROOT/"* ]]; then
        if [[ "$executable" == "$expected" || "$executable" == "$expected (deleted)" ]]; then
          NETCONF_RESIDUAL_PID_CLASS="canonical"
        else
          NETCONF_RESIDUAL_PID_CLASS="noncanonical"
        fi
        return 0
      fi
    fi

    now_ns="$(netconf_monotonic_ns)" || return 0
    ((now_ns < deadline_ns)) || return 0
    netconf_residual_poll_sleep || return 0
    now_ns="$(netconf_monotonic_ns)" || return 0
  done
}

netconf_residual_pids() {
  local expected link pid pid_dir scan_error=0
  netconf_residual_reset_metrics
  expected="$(netconf_expected_executable)" || return 1
  [[ -n "$expected" && "$expected" == /* && "$expected" != *$'\n'* ]] || return 1
  [[ -d "$NETCONF_PROC_ROOT" && -r "$NETCONF_PROC_ROOT" && -x "$NETCONF_PROC_ROOT" ]] || return 1
  for link in "$NETCONF_PROC_ROOT"/[0-9]*/exe; do
    [[ -e "$link" || -L "$link" ]] || continue
    pid_dir="${link%/exe}"
    pid="${pid_dir##*/}"
    [[ "$pid" =~ ^[1-9][0-9]*$ && "$pid" != "1" ]] || continue
    NETCONF_RESIDUAL_ENUMERATED_COUNT=$((NETCONF_RESIDUAL_ENUMERATED_COUNT + 1))
    if ! netconf_classify_residual_pid "$pid" "$expected"; then
      NETCONF_RESIDUAL_PID_CLASS="unresolved"
    fi
    case "$NETCONF_RESIDUAL_PID_CLASS" in
      canonical)
        NETCONF_RESIDUAL_CANONICAL_COUNT=$((NETCONF_RESIDUAL_CANONICAL_COUNT + 1))
        printf '%s\n' "$pid"
        ;;
      noncanonical)
        NETCONF_RESIDUAL_NONCANONICAL_COUNT=$((NETCONF_RESIDUAL_NONCANONICAL_COUNT + 1))
        ;;
      kernel-thread)
        NETCONF_RESIDUAL_KERNEL_THREAD_COUNT=$((NETCONF_RESIDUAL_KERNEL_THREAD_COUNT + 1))
        ;;
      gone)
        NETCONF_RESIDUAL_GONE_COUNT=$((NETCONF_RESIDUAL_GONE_COUNT + 1))
        ;;
      *)
        NETCONF_RESIDUAL_UNRESOLVED_COUNT=$((NETCONF_RESIDUAL_UNRESOLVED_COUNT + 1))
        scan_error=1
        ;;
    esac
  done
  ((scan_error == 0))
}

netconf_monotonic_ns() {
  "$PYTHON_BIN" -c 'import time; print(time.monotonic_ns())'
}

netconf_poll_sleep() {
  sleep 0.2
}

netconf_wait_unit_ready() {
  local start_ns deadline_ns now_ns
  start_ns="$(netconf_monotonic_ns)" || return 1
  deadline_ns=$((start_ns + NETCONF_UNIT_TIMEOUT_NS))
  while :; do
    now_ns="$(netconf_monotonic_ns)" || return 1
    (( now_ns <= deadline_ns )) || return 1
    netconf_unit_active_running && return 0
    netconf_poll_sleep
  done
}

netconf_stability_gate() {
  local start_ns deadline_ns now_ns stable_since_ns=-1
  local pid identity stable_identity=""
  start_ns="$(netconf_monotonic_ns)" || return 1
  deadline_ns=$((start_ns + NETCONF_READINESS_TIMEOUT_NS))
  while :; do
    now_ns="$(netconf_monotonic_ns)" || return 1
    (( now_ns <= deadline_ns )) || return 1
    pid="$(netconf_main_pid)"
    identity=""
    if netconf_unit_active_running && port_listening "$NETCONF_PORT" \
       && netconf_pid_valid "$pid"; then
      identity="$(netconf_process_identity "$pid")" || identity=""
    fi
    if [[ -n "$identity" ]]; then
      if [[ "$identity" != "$stable_identity" ]]; then
        stable_identity="$identity"
        stable_since_ns="$now_ns"
      elif (( now_ns - stable_since_ns >= NETCONF_STABILITY_NS )); then
        return 0
      fi
    else
      stable_identity=""
      stable_since_ns=-1
    fi
    netconf_poll_sleep
  done
}

netconf_preflight() {
  local module residuals
  for module in systemctl sysrepocfg sysrepoctl ss sha256sum awk cat readlink mktemp; do
    command -v "$module" >/dev/null 2>&1 \
      || netconf_fail 19 "Comando obrigatório NETCONF ausente: $module" || return $?
  done
  netconf_expected_executable >/dev/null \
    || netconf_fail 29 "Executável canônico Netopeer2 ausente" || return $?
  netconf_python_ready \
    || netconf_fail 20 "Python/venv NETCONF ou ncclient indisponível: $PYTHON_BIN" || return $?
  id -u "$NETCONF_USER" >/dev/null 2>&1 \
    || netconf_fail 21 "Usuário NETCONF ausente: $NETCONF_USER" || return $?
  [[ -f "$NETCONF_KEY" && -r "$NETCONF_KEY" ]] \
    || netconf_fail 22 "Chave NETCONF ausente ou ilegível: $NETCONF_KEY" || return $?
  sudo -n true >/dev/null 2>&1 \
    || netconf_fail 23 "sudo não interativo indisponível" || return $?
  [[ "$(systemctl show "$NETCONF_SYSTEMD_UNIT" --property=LoadState --value 2>/dev/null || true)" == "loaded" ]] \
    || netconf_fail 28 "Unidade systemd NETCONF não está carregada" || return $?
  for module in "${NETCONF_BOOTSTRAP_MODULES[@]}" l2i-qos; do
    netconf_module_installed "$module" \
      || netconf_fail 24 "Módulo sysrepo obrigatório ausente: $module" || return $?
  done
  [[ "$(systemctl is-active "$NETCONF_SYSTEMD_UNIT" 2>/dev/null || true)" == "inactive" ]] \
    || netconf_fail 25 "Unidade NETCONF não está inativa no preflight" || return $?
  ! port_listening "$NETCONF_PORT" \
    || netconf_fail 26 "Porta NETCONF já está ocupada: $NETCONF_PORT" || return $?
  residuals="$(netconf_residual_pids 2>/dev/null)" \
    || netconf_fail 27 "Falha ao verificar processos Netopeer2 residuais" || return $?
  [[ -z "$residuals" ]] \
    || netconf_fail 27 "Processo Netopeer2 residual detectado: $residuals" || return $?
}

netconf_read_only_probe() {
  "$PYTHON_BIN" - "$NETCONF_LISTEN_ADDRESS" "$NETCONF_PORT" "$NETCONF_USER" "$NETCONF_KEY" <<'PY'
import sys
from ncclient import manager
host, port, username, key_filename = sys.argv[1], int(sys.argv[2]), sys.argv[3], sys.argv[4]
session = None
try:
    session = manager.connect(
        host=host, port=port, username=username, key_filename=key_filename,
        hostkey_verify=False, allow_agent=False, look_for_keys=False, timeout=20,
    )
    reply = session.get_config(source="running")
    if not bool(getattr(reply, "ok", False)):
        raise RuntimeError("invalid get-config reply")
finally:
    if session is not None:
        session.close_session()
PY
}

netconf_limited_diagnostics() {
  warn "Diagnóstico limitado da unidade NETCONF:"
  systemctl show "$NETCONF_SYSTEMD_UNIT" \
    --property=ActiveState,SubState,Result,MainPID,ExecMainStatus 2>&1 | tail -n 20 >&2 || true
  ss -H -ltnp "sport = :$NETCONF_PORT" 2>&1 | tail -n 20 >&2 || true
  sudo -n journalctl -u "$NETCONF_SYSTEMD_UNIT" -n 40 --no-pager 2>&1 | tail -n 40 >&2 || true
}

netconf_main_pid_absent() {
  local pid="${1:-}"
  [[ -z "$pid" || "$pid" == "0" ]] && return 0
  [[ "$pid" =~ ^[1-9][0-9]*$ && "$pid" != "1" ]] || return 1
  [[ -d "$NETCONF_PROC_ROOT" && -r "$NETCONF_PROC_ROOT" && -x "$NETCONF_PROC_ROOT" ]] || return 1
  [[ ! -e "$NETCONF_PROC_ROOT/$pid" ]]
}

netconf_residuals_only_captured() {
  local captured_pid="${1:-}"
  local residuals residual_pid count=0
  [[ "$captured_pid" =~ ^[1-9][0-9]*$ && "$captured_pid" != "1" ]] || return 1
  residuals="$(netconf_residual_pids)" || return 1
  [[ -n "$residuals" ]] || return 1
  while IFS= read -r residual_pid; do
    [[ "$residual_pid" == "$captured_pid" ]] || return 1
    count=$((count + 1))
  done <<< "$residuals"
  ((count == 1))
}

netconf_cleanup_diagnostic() {
  local reason="$1"
  local captured_pid="${2:-<none>}"
  local residuals residual_status
  if residuals="$(netconf_residual_pids)"; then
    residual_status="${residuals:-<none>}"
  else
    residual_status="<unavailable>"
  fi
  warn "NETCONF_CLEANUP_DIAGNOSTIC_BEGIN"
  warn "reason=$reason"
  warn "captured_pid=$captured_pid"
  warn "residual_pids=$residual_status"
  warn "NETCONF_CLEANUP_DIAGNOSTIC_END"
}

netconf_wait_absent() {
  local start_ns deadline_ns now_ns active pid residuals port_rc
  start_ns="$(netconf_monotonic_ns)" || return 1
  deadline_ns=$((start_ns + NETCONF_STOP_TIMEOUT_NS))
  while :; do
    now_ns="$(netconf_monotonic_ns)" || return 1
    (( now_ns <= deadline_ns )) || return 1
    active="$(systemctl show "$NETCONF_SYSTEMD_UNIT" --property=ActiveState --value 2>/dev/null)" \
      || return 1
    pid="$(netconf_main_pid)" || return 1
    residuals="$(netconf_residual_pids)" || return 1
    if port_listening "$NETCONF_PORT"; then
      port_rc=0
    else
      port_rc=$?
    fi
    if [[ "$active" == "inactive" || "$active" == "failed" ]] \
       && ((port_rc == 1)) && netconf_main_pid_absent "$pid" \
       && [[ -z "$residuals" ]]; then
      return 0
    fi
    netconf_poll_sleep
  done
}

netconf_systemd_stop_once() {
  local unit_pid="${1:-}"
  local stop_rc=0 fingerprint="" captured_pid="" fingerprint_required=0 fingerprint_valid=1
  if [[ -n "$unit_pid" && "$unit_pid" != "0" ]]; then
    fingerprint_required=1
    if fingerprint="$(netconf_capture_process_fingerprint "$unit_pid")"; then
      captured_pid="${fingerprint%%$'\n'*}"
    else
      fingerprint_valid=0
      captured_pid="$unit_pid"
    fi
  fi
  sudo -n systemctl stop "$NETCONF_SYSTEMD_UNIT" || stop_rc=$?
  sudo -n rm -f "$NETCONF_PIDFILE" 2>/dev/null || true
  if netconf_wait_absent; then
    if ((fingerprint_required && ! fingerprint_valid)); then
      netconf_cleanup_diagnostic "pre-stop process fingerprint was ambiguous" "$captured_pid"
      return 90
    fi
    if ((fingerprint_required)) && ! netconf_main_pid_absent "$captured_pid"; then
      netconf_cleanup_diagnostic "captured PID still exists with changed or reused identity" "$captured_pid"
      return 90
    fi
    return "$stop_rc"
  fi
  if ((! fingerprint_required || ! fingerprint_valid)); then
    netconf_cleanup_diagnostic "no authenticated pre-stop process fingerprint" "$captured_pid"
    return 90
  fi
  if ! netconf_residuals_only_captured "$captured_pid"; then
    netconf_cleanup_diagnostic "residual set is ambiguous or includes an additional process" "$captured_pid"
    return 90
  fi
  if ! netconf_process_fingerprint_valid "$fingerprint"; then
    netconf_cleanup_diagnostic "captured process identity changed before TERM" "$captured_pid"
    return 90
  fi
  warn "Fallback restrito: processo autenticado $captured_pid persistiu após timeout; enviando TERM."
  sudo -n kill -TERM "$captured_pid" 2>/dev/null || true
  sleep 0.5
  if netconf_wait_absent; then
    if netconf_main_pid_absent "$captured_pid"; then
      return "$stop_rc"
    fi
    netconf_cleanup_diagnostic "captured PID changed or was reused after TERM" "$captured_pid"
    return 90
  fi
  if ! netconf_residuals_only_captured "$captured_pid"; then
    netconf_cleanup_diagnostic "residual set changed after TERM" "$captured_pid"
    return 90
  fi
  if ! netconf_process_fingerprint_valid "$fingerprint"; then
    netconf_cleanup_diagnostic "captured process identity changed before KILL" "$captured_pid"
    return 90
  fi
  warn "Fallback restrito: processo autenticado $captured_pid persistiu após TERM; enviando KILL."
  sudo -n kill -KILL "$captured_pid" 2>/dev/null || true
  if netconf_wait_absent; then
    if netconf_main_pid_absent "$captured_pid"; then
      return "$stop_rc"
    fi
    netconf_cleanup_diagnostic "captured PID changed or was reused after KILL" "$captured_pid"
    return 90
  fi
  netconf_cleanup_diagnostic "final absence gate failed after authenticated fallback" "$captured_pid"
  return 90
}

stop_netconf() {
  local unit_pid
  if [[ "$DRY_RUN" == "1" ]]; then
    info "Executaria parada idempotente de $NETCONF_SYSTEMD_UNIT."
    return 0
  fi
  unit_pid="$(netconf_main_pid)"
  netconf_systemd_stop_once "$unit_pid"
}

netconf_start_body() {
  local module pair raw normalized pid
  local qos_before_raw qos_before_normalized qos_after_raw qos_after_normalized
  declare -A startup_raw=()
  declare -A startup_normalized=()

  netconf_preflight || return $?
  for module in "${NETCONF_BOOTSTRAP_MODULES[@]}"; do
    pair="$(netconf_snapshot "$module" startup)" \
      || netconf_fail 30 "Falha ao capturar startup/$module" || return $?
    read -r raw normalized <<<"$pair"
    [[ -n "$raw" && -n "$normalized" ]] \
      || netconf_fail 31 "Hashes vazios para startup/$module" || return $?
    [[ "$raw" == "${NETCONF_CERTIFIED_STARTUP_SHA256[$module]}" ]] \
      || netconf_fail 32 "Hash certificado divergente em startup/$module" || return $?
    startup_raw[$module]="$raw"
    startup_normalized[$module]="$normalized"
  done
  pair="$(netconf_snapshot l2i-qos running)" \
    || netconf_fail 33 "Falha ao capturar running/l2i-qos" || return $?
  read -r qos_before_raw qos_before_normalized <<<"$pair"
  [[ -n "$qos_before_raw" && -n "$qos_before_normalized" ]] \
    || netconf_fail 34 "Hashes vazios para running/l2i-qos" || return $?

  NETCONF_START_ATTEMPTED=1
  sudo -n systemctl start netopeer2-server.service \
    || netconf_fail 40 "Falha no start único da unidade Netopeer2" || return $?
  netconf_wait_unit_ready \
    || netconf_fail 41 "Timeout aguardando unidade active/running com PID válido" || return $?
  pid="$(netconf_main_pid)"

  for module in "${NETCONF_BOOTSTRAP_MODULES[@]}"; do
    sudo -n sysrepocfg -C startup -d running -m "$module" \
      || netconf_fail 50 "Falha na cópia startup→running de $module" || return $?
    pair="$(netconf_snapshot "$module" running)" \
      || netconf_fail 51 "Falha ao validar running/$module" || return $?
    read -r raw normalized <<<"$pair"
    [[ "$normalized" == "${startup_normalized[$module]}" ]] \
      || netconf_fail 52 "Divergência semântica/normalizada em running/$module" || return $?
  done

  for module in "${NETCONF_BOOTSTRAP_MODULES[@]}"; do
    pair="$(netconf_snapshot "$module" startup)" \
      || netconf_fail 53 "Falha no snapshot final de startup/$module" || return $?
    read -r raw normalized <<<"$pair"
    [[ "$raw" == "${startup_raw[$module]}" && "$normalized" == "${startup_normalized[$module]}" ]] \
      || netconf_fail 54 "startup/$module foi alterado durante bootstrap" || return $?
  done
  pair="$(netconf_snapshot l2i-qos running)" \
    || netconf_fail 55 "Falha no snapshot final de running/l2i-qos" || return $?
  read -r qos_after_raw qos_after_normalized <<<"$pair"
  [[ "$qos_after_raw" == "$qos_before_raw" && "$qos_after_normalized" == "$qos_before_normalized" ]] \
    || netconf_fail 56 "running/l2i-qos foi alterado durante bootstrap" || return $?

  netconf_stability_gate \
    || netconf_fail 60 "Porta 830/PID não permaneceram estáveis por 2 segundos" || return $?
  netconf_pid_valid "$(netconf_main_pid)" \
    || netconf_fail 61 "PID Netopeer2 morreu antes da probe autenticada" || return $?
  netconf_read_only_probe \
    || netconf_fail 62 "Sessão NETCONF/get-config running falhou" || return $?
  printf '%s\n' "$pid" | sudo -n tee "$NETCONF_PIDFILE" >/dev/null \
    || netconf_fail 63 "Falha ao registrar PID real Netopeer2" || return $?
  info "Netopeer2 qualificado em 127.0.0.1:$NETCONF_PORT (PID real=$pid)."
}

start_netconf() {
  local rc unit_pid primary
  NETCONF_PRIMARY_ERROR=""
  NETCONF_START_ATTEMPTED=0
  if [[ "$DRY_RUN" == "1" ]]; then
    info "Validaria o bootstrap NETCONF certificado via systemd."
    return 0
  fi
  if netconf_start_body; then
    return 0
  else
    rc=$?
  fi
  primary="${NETCONF_PRIMARY_ERROR:-bootstrap NETCONF falhou (rc=$rc)}"
  if [[ "$NETCONF_START_ATTEMPTED" == "1" ]]; then
    netconf_limited_diagnostics
    unit_pid="$(netconf_main_pid)"
    netconf_systemd_stop_once "$unit_pid" || true
  fi
  NETCONF_PRIMARY_ERROR="$primary"
  err "Falha primária preservada: $NETCONF_PRIMARY_ERROR"
  return "$rc"
}

# -------------------------------
# p4
# -------------------------------
push_p4_pipeline() {
  require_repo_layout
  assert_repo_files
  run "$PYTHON_BIN" "$REPO_DIR/scripts/p4_push_pipeline.py" --addr "$P4_ADDR"
}

wait_for_port() {
  local port="$1"
  local timeout_s="${2:-15}"
  local i
  for ((i=1; i<=timeout_s; i++)); do
    if port_listening "$port"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

show_bmv2_diagnostics() {
  local log_file="${1:-/tmp/l2i_minimal/bmv2.log}"
  warn "Diagnóstico P4/BMv2:"
  if pgrep -af simple_switch_grpc >/dev/null 2>&1; then
    pgrep -af simple_switch_grpc || true
  else
    warn "Processo simple_switch_grpc não encontrado após tentativa de inicialização."
  fi

  if [[ -f "$log_file" ]]; then
    warn "Metadados de $log_file:"
    stat "$log_file" >&2 || true
    warn "Primeiras linhas de $log_file:"
    sed -n '1,80p' "$log_file" >&2 || true
    warn "Últimas linhas de $log_file:"
    tail -n 80 "$log_file" >&2 || true
  else
    warn "Arquivo de log do BMv2 não encontrado em $log_file."
  fi
}

stop_p4() {
  require_repo_layout
  run sudo "$REPO_DIR/scripts/p4_stop.sh"
}

start_p4() {
  require_repo_layout
  assert_repo_files

  local bmv2_log="${BMV2_LOG_FILE:-/tmp/l2i_minimal/bmv2.log}"
  local timeout="${P4_START_TIMEOUT:-20}"

  if port_listening "$P4_PORT"; then
    if [[ "$P4_REQUIRE_THRIFT" == "1" ]] \
       && ! port_listening "$P4_THRIFT_PORT"; then
      warn "P4Runtime ativo, mas Thrift obrigatório está ausente; reiniciando o BMv2."
      stop_p4
    else
      info "P4Runtime já está em execução na porta $P4_PORT."
      if port_listening "$P4_THRIFT_PORT"; then
        info "P4 Thrift opcional também está ativo na porta $P4_THRIFT_PORT."
      else
        info "P4 Thrift opcional não está disponível neste build."
      fi
      return 0
    fi
  elif port_listening "$P4_THRIFT_PORT"; then
    warn "Thrift ativo sem P4Runtime; reiniciando o BMv2."
    stop_p4
  fi

  run bash -lc "cd '$REPO_DIR' && P4_THRIFT_PORT='$P4_THRIFT_PORT' P4_REQUIRE_THRIFT='$P4_REQUIRE_THRIFT' P4_GRPC_ADDR='$P4_GRPC_ADDR' ./scripts/p4_build_and_run.sh"

  if ! wait_for_port "$P4_PORT" "$timeout"; then
    show_bmv2_diagnostics "$bmv2_log"
    err "P4Runtime não abriu a porta $P4_PORT dentro do tempo esperado."
    exit 1
  fi

  if port_listening "$P4_THRIFT_PORT"; then
    info "P4 ativo. P4Runtime=:$P4_PORT Thrift=:$P4_THRIFT_PORT."
  elif [[ "$P4_REQUIRE_THRIFT" == "1" ]]; then
    show_bmv2_diagnostics "$bmv2_log"
    err "P4Runtime abriu, mas o Thrift obrigatório não abriu a porta $P4_THRIFT_PORT."
    exit 1
  else
    info "P4 ativo. P4Runtime=:$P4_PORT; Thrift opcional indisponível neste build."
  fi
}

start_real_services() {
  require_repo_layout
  start_netconf
  start_p4
  push_p4_pipeline

  port_listening "$NETCONF_PORT" || { err "NETCONF não está em escuta."; exit 1; }
  port_listening "$P4_PORT" || { err "P4Runtime não está em escuta."; exit 1; }

  if [[ "$P4_REQUIRE_THRIFT" == "1" ]]; then
    port_listening "$P4_THRIFT_PORT" || { err "P4 Thrift obrigatório não está em escuta."; exit 1; }
  fi

  if port_listening "$P4_THRIFT_PORT"; then
    info "Serviços reais ativos. NETCONF=:$NETCONF_PORT P4Runtime=:$P4_PORT Thrift=:$P4_THRIFT_PORT"
  else
    info "Serviços reais ativos. NETCONF=:$NETCONF_PORT P4Runtime=:$P4_PORT Thrift=indisponível/opcional"
  fi
}

stop_real_services() {
  stop_p4
  stop_netconf
}

# -------------------------------
# limpeza e execução de cenários
# -------------------------------
cleanup_topologies_only() {
  require_repo_layout
  run sudo "$REPO_DIR/scripts/s1_topology_cleanup.sh" || true
  run sudo "$REPO_DIR/scripts/s2_topology_cleanup.sh" || true
  run sudo "$REPO_DIR/scripts/s2_p4_topology_cleanup.sh" || true
  run sudo "$REPO_DIR/scripts/cleanup_net.sh" || true
}

cleanup() {
  cleanup_topologies_only
  run sudo "$REPO_DIR/scripts/p4_stop.sh" --remove-links || true
  stop_netconf
}

run_python_module_as_root() {
  local module="$1"
  shift
  sudo -E \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}" \
    PATH="$VENV_DIR/bin:$PATH" \
    "$PYTHON_BIN" -m "$module" "$@"
}

run_python_script_as_root() {
  local script="$1"
  shift
  sudo -E \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}" \
    PATH="$VENV_DIR/bin:$PATH" \
    "$PYTHON_BIN" "$script" "$@"
}

run_with_cleanup_trap() {
  local setup_script="$1"
  shift
  local runner=("$@")

  cleanup_topologies_only
  trap 'cleanup_topologies_only' EXIT INT TERM

  run sudo "$setup_script"
  if [[ "$DRY_RUN" == "1" ]]; then
    trap - EXIT INT TERM
    cleanup_topologies_only
    return 0
  fi

  "${runner[@]}"
  local rc=$?

  trap - EXIT INT TERM
  cleanup_topologies_only
  return $rc
}

run_s1_real() {
  require_repo_layout
  run_with_cleanup_trap \
    "$REPO_DIR/scripts/s1_topology_setup.sh" \
    run_python_module_as_root scenarios.multidomain_s1 \
      --spec "$REPO_DIR/specs/valid/s1_unicast_qos.json" \
      --duration "${S1_DURATION:-30}" \
      --flow-mbps "${S1_FLOW_MBPS:-8}" \
      --be-mbps "${S1_BE_MBPS:-60}" \
      --bwA "${S1_BWA:-100}" \
      --bwB "${S1_BWB:-50}" \
      --bwC "${S1_BWC:-100}" \
      --delay-ms "${S1_DELAY_MS:-1}" \
      --rtt-interval-ms "${S1_RTT_INTERVAL_MS:-50}" \
      --bandwidth-tolerance-mbps "${S1_BANDWIDTH_TOLERANCE_MBPS:-0.25}" \
      --profile-id "${S1_PROFILE_ID:-s1-canonical-v1}" \
      --repetition "${S1_REPETITION:-1}" \
      --mode "${S1_MODE:-adapt}" \
      --backend real
}

run_s1_mock() {
  require_repo_layout
  run_with_cleanup_trap \
    "$REPO_DIR/scripts/s1_topology_setup.sh" \
    run_python_module_as_root scenarios.multidomain_s1 \
      --spec "$REPO_DIR/specs/valid/s1_unicast_qos.json" \
      --duration "${S1_DURATION:-30}" \
      --flow-mbps "${S1_FLOW_MBPS:-8}" \
      --be-mbps "${S1_BE_MBPS:-60}" \
      --bwA "${S1_BWA:-100}" \
      --bwB "${S1_BWB:-50}" \
      --bwC "${S1_BWC:-100}" \
      --delay-ms "${S1_DELAY_MS:-1}" \
      --rtt-interval-ms "${S1_RTT_INTERVAL_MS:-50}" \
      --bandwidth-tolerance-mbps "${S1_BANDWIDTH_TOLERANCE_MBPS:-0.25}" \
      --profile-id "${S1_PROFILE_ID:-s1-canonical-v1}" \
      --repetition "${S1_REPETITION:-1}" \
      --mode "${S1_MODE:-adapt}" \
      --backend mock
}

s2_operational_execution_id() {
  local mode="${S2_MODE:-adapt}"
  local candidate
  if [[ -v S2_EXECUTION_ID ]]; then
    candidate="$S2_EXECUTION_ID"
  else
    candidate="s2-operational-$(date -u +%Y%m%dT%H%M%S%NZ)-${mode}"
  fi
  if [[ "$candidate" == "." || "$candidate" == ".." || ! "$candidate" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
    err "S2_EXECUTION_ID inválido: deve ser um componente de path seguro"
    return 1
  fi
  printf '%s\n' "$candidate"
}

run_s2_real() {
  require_repo_layout
  local execution_id
  execution_id="$(s2_operational_execution_id)" || return 1
  run_with_cleanup_trap \
    "$REPO_DIR/scripts/s2_topology_setup.sh" \
    run_python_module_as_root scenarios.multidomain_s2 \
      --spec "$REPO_DIR/specs/valid/s2_multicast_source_oriented.json" \
      --execution-id "$execution_id" \
      --repetition "${S2_REPETITION:-1}" \
      --results-root "$REPO_DIR/results/S2" \
      --duration "${S2_DURATION:-10}" \
      --mode "${S2_MODE:-adapt}" \
      --backend real \
      --packet-interval-ms "${S2_PACKET_INTERVAL_MS:-50}" \
      --recovery-bin-ms "${S2_RECOVERY_BIN_MS:-500}" \
      --stable-k-bins "${S2_STABLE_K_BINS:-3}"
}


run_s2_p4_dataplane_smoke() {
  require_repo_layout

  local output_dir="${S2_DP_OUTPUT_DIR:-$REPO_DIR/results/S2/dataplane-smoke-$(date -u +%Y%m%dT%H%M%SZ)}"
  local group="${S2_DP_MCAST_DST:-239.1.1.1}"
  local group_id="${S2_DP_MCAST_GROUP_ID:-1}"
  local udp_port="${S2_DP_UDP_PORT:-5001}"
  local rate_validation_args=()
  local sender_profile_id="${S2_DP_SENDER_PROFILE_ID:-phase13-tailspin-900us-affinity-v1}"

  # Phase 13 selected the portable 900 us tail-spin profile on the complete
  # namespace/veth/BMv2 multicast path. Rate validation is therefore enabled by
  # default for this smoke command. Set S2_DP_REQUIRE_RATE_VALIDATION=0 only for
  # an explicitly replication-only diagnostic run.
  if [[ "${S2_DP_REQUIRE_RATE_VALIDATION:-1}" == "1" ]]; then
    rate_validation_args+=(--require-rate-validation)
  fi

  cleanup_topologies_only
  trap 'cleanup_topologies_only' EXIT INT TERM

  run sudo "$REPO_DIR/scripts/s2_p4_topology_setup.sh"

  if [[ "$DRY_RUN" == "1" ]]; then
    trap - EXIT INT TERM
    cleanup_topologies_only
    return 0
  fi

  run_python_script_as_root \
    "$REPO_DIR/scripts/p4_program_s2.py" \
      --addr "$P4_ADDR" \
      --device-id 0 \
      --outdir /tmp/l2i_minimal \
      --mgrp "$group_id" \
      --dst-mcast "$group" \
      --ports 1 2

  run_python_script_as_root \
    "$REPO_DIR/scripts/s2_multicast_dataplane_smoke.py" \
      orchestrate \
      --group "$group" \
      --port "$udp_port" \
      --source-namespace h1 \
      --source-ip 10.0.0.1 \
      --receiver B:h3:10.0.0.3 \
      --receiver C:h4:10.0.0.4 \
      --duration "${S2_DP_DURATION:-3}" \
      --rate-mbps "${S2_DP_RATE_MBPS:-2}" \
      --packet-size "${S2_DP_PACKET_SIZE:-1200}" \
      --pacing-mode "${S2_DP_PACING_MODE:-repeated_sleep_spin}" \
      --spin-threshold-us "${S2_DP_SPIN_THRESHOLD_US:-900}" \
      --sender-profile-id "$sender_profile_id" \
      --sender-cpu "${S2_DP_SENDER_CPU:-auto}" \
      --max-abs-rate-error-pct "${S2_DP_MAX_ABS_RATE_ERROR_PCT:-5}" \
      --min-inter-send-ratio "${S2_DP_MIN_INTER_SEND_RATIO:-0.98}" \
      "${rate_validation_args[@]}" \
      --min-delivery "${S2_DP_MIN_DELIVERY:-0.99}" \
      --output-dir "$output_dir"

  local rc=$?
  echo "S2_DP_OUTPUT_DIR=$output_dir"

  trap - EXIT INT TERM
  cleanup_topologies_only
  return "$rc"
}


run_s2_p4_qos_contention() {
  require_repo_layout

  local output_dir="${S2_QOS_OUTPUT_DIR:-$REPO_DIR/results/S2/qos-contention-$(date -u +%Y%m%dT%H%M%SZ)}"
  local mode="${S2_QOS_MODE:-baseline}"
  local group="${S2_QOS_MCAST_DST:-239.1.1.1}"
  local group_id="${S2_QOS_MCAST_GROUP_ID:-1}"
  local multicast_port="${S2_QOS_MULTICAST_PORT:-5001}"
  local background_port="${S2_QOS_BACKGROUND_PORT:-6001}"
  local qos_profile_id="${S2_QOS_PROFILE_ID:-phase14-s2-p4-qos-contention-v1}"

  # The current P4 pipeline performs forwarding and replication only. The
  # shared bottleneck and QoS classes are created on the Linux receiver-B
  # attachment egress, so the experiment never attributes queue guarantees to
  # BMv2 or to the P4 program.
  cleanup_topologies_only
  trap 'cleanup_topologies_only' EXIT INT TERM

  run sudo "$REPO_DIR/scripts/s2_p4_topology_setup.sh"

  if [[ "$DRY_RUN" == "1" ]]; then
    trap - EXIT INT TERM
    cleanup_topologies_only
    return 0
  fi

  run_python_script_as_root \
    "$REPO_DIR/scripts/p4_program_s2.py" \
      --addr "$P4_ADDR" \
      --device-id 0 \
      --outdir /tmp/l2i_minimal \
      --mgrp "$group_id" \
      --dst-mcast "$group" \
      --ports 1 2

  # Background traffic enters BMv2 at port 3 and exits through port 1. This
  # makes it share the same receiver-B egress as one multicast replica, while
  # receiver C on port 2 remains an uncontended control path.
  run_python_script_as_root \
    "$REPO_DIR/scripts/p4_program_unicast.py" \
      --addr "$P4_ADDR" \
      --device-id 0 \
      --outdir /tmp/l2i_minimal \
      --ingress-port 3 \
      --egress-port 1

  run_python_script_as_root \
    "$REPO_DIR/scripts/s2_p4_qos_contention.py" \
      orchestrate \
      --mode "$mode" \
      --output-dir "$output_dir" \
      --bottleneck-device "${S2_QOS_BOTTLENECK_DEVICE:-s2b-h3}" \
      --capacity-mbps "${S2_QOS_CAPACITY_MBPS:-3}" \
      --multicast-reserved-mbps "${S2_QOS_MULTICAST_RESERVED_MBPS:-2.1}" \
      --queue-limit-packets "${S2_QOS_QUEUE_LIMIT_PACKETS:-64}" \
      --group "$group" \
      --multicast-port "$multicast_port" \
      --background-port "$background_port" \
      --qos-profile-id "$qos_profile_id" \
      --duration "${S2_QOS_DURATION:-12}" \
      --background-duration "${S2_QOS_BACKGROUND_DURATION:-18}" \
      --background-prefill-s "${S2_QOS_BACKGROUND_PREFILL_S:-1.5}" \
      --background-drain-s "${S2_QOS_BACKGROUND_DRAIN_S:-1.5}" \
      --multicast-receiver-drain-s "${S2_QOS_MULTICAST_RECEIVER_DRAIN_S:-1}" \
      --multicast-rate-mbps "${S2_QOS_MULTICAST_RATE_MBPS:-2}" \
      --background-rate-mbps "${S2_QOS_BACKGROUND_RATE_MBPS:-2}" \
      --packet-size "${S2_QOS_PACKET_SIZE:-1200}" \
      --tc-overhead-bytes "${S2_QOS_TC_OVERHEAD_BYTES:-42}" \
      --spin-threshold-us "${S2_QOS_SPIN_THRESHOLD_US:-900}" \
      --sender-profile-id "${S2_QOS_SENDER_PROFILE_ID:-phase13-tailspin-900us-affinity-v1}" \
      --multicast-sender-cpu "${S2_QOS_MULTICAST_SENDER_CPU:-auto}" \
      --background-sender-cpu "${S2_QOS_BACKGROUND_SENDER_CPU:-auto-distinct}" \
      --max-abs-rate-error-pct "${S2_QOS_MAX_ABS_RATE_ERROR_PCT:-5}" \
      --min-inter-send-ratio "${S2_QOS_MIN_INTER_SEND_RATIO:-0.98}" \
      --worker-ready-timeout-s "${S2_QOS_WORKER_READY_TIMEOUT_S:-10}" \
      --worker-stop-timeout-s "${S2_QOS_WORKER_STOP_TIMEOUT_S:-5}" \
      --worker-max-runtime-s "${S2_QOS_WORKER_MAX_RUNTIME_S:-180}"

  local rc=$?
  echo "S2_QOS_OUTPUT_DIR=$output_dir"

  trap - EXIT INT TERM
  cleanup_topologies_only
  return "$rc"
}


run_s2_p4_state_recovery() {
  require_repo_layout

  local output_dir="${S2_RECOVERY_OUTPUT_DIR:-$REPO_DIR/results/S2/state-recovery-$(date -u +%Y%m%dT%H%M%SZ)}"
  local group="${S2_RECOVERY_MCAST_DST:-239.1.1.1}"
  local group_id="${S2_RECOVERY_MCAST_GROUP_ID:-1}"
  local multicast_port="${S2_RECOVERY_MULTICAST_PORT:-5001}"
  local control_port="${S2_RECOVERY_CONTROL_PORT:-6001}"

  # The fault model removes only multicast control-plane state. A separate
  # unicast rule and paced unicast flow remain active as a process-continuity
  # control, so a multicast outage is not confused with a BMv2 restart.
  cleanup_topologies_only
  trap 'cleanup_topologies_only' EXIT INT TERM

  run sudo "$REPO_DIR/scripts/s2_p4_topology_setup.sh"

  if [[ "$DRY_RUN" == "1" ]]; then
    trap - EXIT INT TERM
    cleanup_topologies_only
    return 0
  fi

  run_python_script_as_root \
    "$REPO_DIR/scripts/s2_p4_state_recovery.py" \
      --output-dir "$output_dir" \
      --recovery-profile-id "${S2_RECOVERY_PROFILE_ID:-phase15-s2-p4-state-recovery-v1}" \
      --p4-addr "$P4_ADDR" \
      --p4-host 127.0.0.1 \
      --p4-port "$P4_PORT" \
      --device-id 0 \
      --p4-outdir /tmp/l2i_minimal \
      --group "$group" \
      --group-id "$group_id" \
      --multicast-ports 1 2 \
      --multicast-port "$multicast_port" \
      --control-port "$control_port" \
      --duration "${S2_RECOVERY_DURATION:-14}" \
      --fault-after-s "${S2_RECOVERY_FAULT_AFTER_S:-4}" \
      --fault-hold-s "${S2_RECOVERY_FAULT_HOLD_S:-2}" \
      --minimum-post-s "${S2_RECOVERY_MINIMUM_POST_S:-5}" \
      --window-guard-s "${S2_RECOVERY_WINDOW_GUARD_S:-0.25}" \
      --multicast-rate-mbps "${S2_RECOVERY_MULTICAST_RATE_MBPS:-2}" \
      --control-rate-mbps "${S2_RECOVERY_CONTROL_RATE_MBPS:-0.5}" \
      --packet-size "${S2_RECOVERY_PACKET_SIZE:-1200}" \
      --spin-threshold-us "${S2_RECOVERY_SPIN_THRESHOLD_US:-900}" \
      --multicast-sender-cpu "${S2_RECOVERY_MULTICAST_SENDER_CPU:-auto}" \
      --control-sender-cpu "${S2_RECOVERY_CONTROL_SENDER_CPU:-auto-distinct}" \
      --max-abs-rate-error-pct "${S2_RECOVERY_MAX_ABS_RATE_ERROR_PCT:-5}" \
      --min-inter-send-ratio "${S2_RECOVERY_MIN_INTER_SEND_RATIO:-0.98}" \
      --minimum-stable-delivery "${S2_RECOVERY_MINIMUM_STABLE_DELIVERY:-0.99}" \
      --minimum-control-delivery "${S2_RECOVERY_MINIMUM_CONTROL_DELIVERY:-0.99}" \
      --maximum-fault-delivery "${S2_RECOVERY_MAXIMUM_FAULT_DELIVERY:-0.05}" \
      --maximum-first-packet-recovery-ms "${S2_RECOVERY_MAXIMUM_FIRST_PACKET_MS:-50}" \
      --state-readback-timeout-s "${S2_RECOVERY_STATE_READBACK_TIMEOUT_S:-2}" \
      --state-poll-interval-s "${S2_RECOVERY_STATE_POLL_INTERVAL_S:-0.02}" \
      --worker-max-runtime-s "${S2_RECOVERY_WORKER_MAX_RUNTIME_S:-120}"

  local rc=$?
  echo "S2_RECOVERY_OUTPUT_DIR=$output_dir"

  trap - EXIT INT TERM
  cleanup_topologies_only
  return "$rc"
}


run_s2_p4_autonomous_assurance() {
  require_repo_layout

  local output_dir="${S2_ASSURANCE_OUTPUT_DIR:-$REPO_DIR/results/S2/autonomous-assurance-$(date -u +%Y%m%dT%H%M%SZ)}"
  local group="${S2_ASSURANCE_MCAST_DST:-239.1.1.1}"
  local group_id="${S2_ASSURANCE_MCAST_GROUP_ID:-1}"
  local multicast_port="${S2_ASSURANCE_MULTICAST_PORT:-5001}"
  local control_port="${S2_ASSURANCE_CONTROL_PORT:-6001}"

  # The independent injector receives only a traffic-start barrier and its own
  # private delay. The MAD assurance controller continuously observes P4Runtime
  # state and never receives the fault schedule or a remediation command.
  cleanup_topologies_only
  trap 'cleanup_topologies_only' EXIT INT TERM

  run sudo "$REPO_DIR/scripts/s2_p4_topology_setup.sh"

  if [[ "$DRY_RUN" == "1" ]]; then
    trap - EXIT INT TERM
    cleanup_topologies_only
    return 0
  fi

  run_python_script_as_root \
    "$REPO_DIR/scripts/s2_p4_autonomous_assurance.py" \
      orchestrate \
      --output-dir "$output_dir" \
      --assurance-profile-id "${S2_ASSURANCE_PROFILE_ID:-phase16-s2-p4-autonomous-assurance-v1}" \
      --p4-addr "$P4_ADDR" \
      --p4-host 127.0.0.1 \
      --p4-port "$P4_PORT" \
      --device-id 0 \
      --p4-outdir /tmp/l2i_minimal \
      --group "$group" \
      --group-id "$group_id" \
      --multicast-ports 1 2 \
      --multicast-port "$multicast_port" \
      --control-port "$control_port" \
      --duration "${S2_ASSURANCE_DURATION:-12}" \
      --fault-after-s "${S2_ASSURANCE_FAULT_AFTER_S:-4}" \
      --fault-kind "${S2_ASSURANCE_FAULT_KIND:-both}" \
      --minimum-post-s "${S2_ASSURANCE_MINIMUM_POST_S:-5}" \
      --window-guard-s "${S2_ASSURANCE_WINDOW_GUARD_S:-0.25}" \
      --multicast-rate-mbps "${S2_ASSURANCE_MULTICAST_RATE_MBPS:-2}" \
      --control-rate-mbps "${S2_ASSURANCE_CONTROL_RATE_MBPS:-0.5}" \
      --packet-size "${S2_ASSURANCE_PACKET_SIZE:-1200}" \
      --spin-threshold-us "${S2_ASSURANCE_SPIN_THRESHOLD_US:-900}" \
      --multicast-sender-cpu "${S2_ASSURANCE_MULTICAST_SENDER_CPU:-auto}" \
      --control-sender-cpu "${S2_ASSURANCE_CONTROL_SENDER_CPU:-auto-distinct}" \
      --max-abs-rate-error-pct "${S2_ASSURANCE_MAX_ABS_RATE_ERROR_PCT:-5}" \
      --min-inter-send-ratio "${S2_ASSURANCE_MIN_INTER_SEND_RATIO:-0.98}" \
      --minimum-stable-delivery "${S2_ASSURANCE_MINIMUM_STABLE_DELIVERY:-0.99}" \
      --minimum-control-delivery "${S2_ASSURANCE_MINIMUM_CONTROL_DELIVERY:-0.99}" \
      --minimum-lost-packets "${S2_ASSURANCE_MINIMUM_LOST_PACKETS:-1}" \
      --maximum-first-packet-recovery-ms "${S2_ASSURANCE_MAX_FIRST_PACKET_MS:-50}" \
      --maximum-detection-ms "${S2_ASSURANCE_MAX_DETECTION_MS:-150}" \
      --maximum-control-plane-recovery-ms "${S2_ASSURANCE_MAX_CONTROL_PLANE_RECOVERY_MS:-250}" \
      --maximum-total-reconciliation-ms "${S2_ASSURANCE_MAX_TOTAL_RECONCILIATION_MS:-400}" \
      --assurance-poll-interval-s "${S2_ASSURANCE_POLL_INTERVAL_S:-0.02}" \
      --assurance-drift-confirmations "${S2_ASSURANCE_DRIFT_CONFIRMATIONS:-3}" \
      --assurance-convergence-confirmations "${S2_ASSURANCE_CONVERGENCE_CONFIRMATIONS:-2}" \
      --assurance-maximum-remediation-attempts "${S2_ASSURANCE_MAX_REMEDIATION_ATTEMPTS:-3}" \
      --assurance-forced-remediation-rejections "${S2_ASSURANCE_FORCED_REMEDIATION_REJECTIONS:-0}" \
      --assurance-initial-backoff-s "${S2_ASSURANCE_INITIAL_BACKOFF_S:-0.01}" \
      --assurance-backoff-multiplier "${S2_ASSURANCE_BACKOFF_MULTIPLIER:-2}" \
      --assurance-maximum-backoff-s "${S2_ASSURANCE_MAX_BACKOFF_S:-0.10}" \
      --state-readback-timeout-s "${S2_ASSURANCE_STATE_READBACK_TIMEOUT_S:-2}" \
      --state-poll-interval-s "${S2_ASSURANCE_STATE_POLL_INTERVAL_S:-0.01}" \
      --worker-max-runtime-s "${S2_ASSURANCE_WORKER_MAX_RUNTIME_S:-120}"

  local rc=$?
  echo "S2_ASSURANCE_OUTPUT_DIR=$output_dir"

  trap - EXIT INT TERM
  cleanup_topologies_only
  return "$rc"
}


run_s2_p4_autonomous_assurance_foundation() {
  require_repo_layout

  # Repeat the autonomous reconciliation experiment and aggregate every drift,
  # remediation, convergence, dataplane, and continuity record without treating
  # a scientific candidate decision as an operational shell failure.
  run \
    "$REPO_DIR/scripts/run_s2_p4_autonomous_assurance_foundation.sh" \
    "${PHASE16_FOUNDATION_OUTPUT_DIR:-$REPO_DIR/results/S2/autonomous-assurance-foundation-$(date -u +%Y%m%dT%H%M%SZ)}"
}


run_s2_p4_autonomous_assurance_validation() {
  require_repo_layout

  # Exercise a mirrored matrix that includes a no-fault control, selective
  # component drift, and a bounded synthetic remediation rejection. The runner
  # verifies exact classification, exact component reapply, retry, and backoff.
  run \
    "$REPO_DIR/scripts/run_s2_p4_autonomous_assurance_validation.sh" \
    "${PHASE16_VALIDATION_OUTPUT_DIR:-$REPO_DIR/results/S2/autonomous-assurance-validation-$(date -u +%Y%m%dT%H%M%SZ)}"
}


run_s2_multidomain_autonomous_assurance() {
  require_repo_layout

  local output_dir="${S2_MULTIDOMAIN_ASSURANCE_OUTPUT_DIR:-$REPO_DIR/results/S2/multidomain-assurance-$(date -u +%Y%m%dT%H%M%SZ)}"

  # The dedicated Linux veth is created by the scenario. Netopeer2 and BMv2
  # remain external services so their process continuity can be measured. The
  # independent injector receives no controller event or remediation command.
  run_python_script_as_root \
    "$REPO_DIR/scripts/s2_multidomain_autonomous_assurance.py" \
      orchestrate \
      --output-dir "$output_dir" \
      --assurance-profile-id "${S2_MULTIDOMAIN_ASSURANCE_PROFILE_ID:-phase17-s2-multidomain-assurance-foundation-v1}" \
      --linux-device "${S2_MULTIDOMAIN_ASSURANCE_LINUX_DEVICE:-l2i-md-a0}" \
      --linux-peer "${S2_MULTIDOMAIN_ASSURANCE_LINUX_PEER:-l2i-md-a1}" \
      --netconf-host "${S2_MULTIDOMAIN_ASSURANCE_NETCONF_HOST:-127.0.0.1}" \
      --netconf-port "${S2_MULTIDOMAIN_ASSURANCE_NETCONF_PORT:-$NETCONF_PORT}" \
      --netconf-username "${S2_MULTIDOMAIN_ASSURANCE_NETCONF_USERNAME:-$NETCONF_USER}" \
      --netconf-key "${S2_MULTIDOMAIN_ASSURANCE_NETCONF_KEY:-$NETCONF_KEY}" \
      --netconf-timeout-s "${S2_MULTIDOMAIN_ASSURANCE_NETCONF_TIMEOUT_S:-5}" \
      --p4-addr "$P4_ADDR" \
      --p4-host 127.0.0.1 \
      --p4-port "$P4_PORT" \
      --device-id 0 \
      --p4-outdir /tmp/l2i_minimal \
      --group "${S2_MULTIDOMAIN_ASSURANCE_GROUP:-239.1.1.1}" \
      --group-id "${S2_MULTIDOMAIN_ASSURANCE_GROUP_ID:-1}" \
      --multicast-ports 1 2 \
      --multicast-port "${S2_MULTIDOMAIN_ASSURANCE_MULTICAST_PORT:-5001}" \
      --observer-election-low "${S2_MULTIDOMAIN_ASSURANCE_OBSERVER_ELECTION_LOW:-17100}" \
      --injector-election-low "${S2_MULTIDOMAIN_ASSURANCE_INJECTOR_ELECTION_LOW:-17110}" \
      --initial-cleanup-election-low "${S2_MULTIDOMAIN_ASSURANCE_INITIAL_CLEANUP_ELECTION_LOW:-17180}" \
      --initial-program-election-low "${S2_MULTIDOMAIN_ASSURANCE_INITIAL_PROGRAM_ELECTION_LOW:-17190}" \
      --remediation-election-low "${S2_MULTIDOMAIN_ASSURANCE_REMEDIATION_ELECTION_LOW:-17120}" \
      --cleanup-election-low "${S2_MULTIDOMAIN_ASSURANCE_CLEANUP_ELECTION_LOW:-17990}" \
      --qos-class "${S2_MULTIDOMAIN_ASSURANCE_QOS_CLASS:-prio10}" \
      --capacity-mbps "${S2_MULTIDOMAIN_ASSURANCE_CAPACITY_MBPS:-3}" \
      --minimum-mbps "${S2_MULTIDOMAIN_ASSURANCE_MINIMUM_MBPS:-2}" \
      --maximum-mbps "${S2_MULTIDOMAIN_ASSURANCE_MAXIMUM_MBPS:-3}" \
      --fault-after-s "${S2_MULTIDOMAIN_ASSURANCE_FAULT_AFTER_S:-4}" \
      --injector-completion-timeout-s "${S2_MULTIDOMAIN_ASSURANCE_INJECTOR_COMPLETION_TIMEOUT_S:-10}" \
      --assurance-recovery-timeout-s "${S2_MULTIDOMAIN_ASSURANCE_RECOVERY_TIMEOUT_S:-12}" \
      --maximum-detection-ms "${S2_MULTIDOMAIN_ASSURANCE_MAX_DETECTION_MS:-1500}" \
      --maximum-control-plane-recovery-ms "${S2_MULTIDOMAIN_ASSURANCE_MAX_CONTROL_PLANE_RECOVERY_MS:-2500}" \
      --maximum-total-reconciliation-ms "${S2_MULTIDOMAIN_ASSURANCE_MAX_TOTAL_RECONCILIATION_MS:-4000}" \
      --assurance-poll-interval-s "${S2_MULTIDOMAIN_ASSURANCE_POLL_INTERVAL_S:-0.05}" \
      --assurance-drift-confirmations "${S2_MULTIDOMAIN_ASSURANCE_DRIFT_CONFIRMATIONS:-3}" \
      --assurance-convergence-confirmations "${S2_MULTIDOMAIN_ASSURANCE_CONVERGENCE_CONFIRMATIONS:-2}" \
      --assurance-maximum-remediation-attempts "${S2_MULTIDOMAIN_ASSURANCE_MAX_REMEDIATION_ATTEMPTS:-3}" \
      --assurance-initial-backoff-s "${S2_MULTIDOMAIN_ASSURANCE_INITIAL_BACKOFF_S:-0.05}" \
      --assurance-backoff-multiplier "${S2_MULTIDOMAIN_ASSURANCE_BACKOFF_MULTIPLIER:-2}" \
      --assurance-maximum-backoff-s "${S2_MULTIDOMAIN_ASSURANCE_MAX_BACKOFF_S:-0.5}"

  local rc=$?
  echo "S2_MULTIDOMAIN_ASSURANCE_OUTPUT_DIR=$output_dir"
  return "$rc"
}


run_s2_multidomain_autonomous_assurance_foundation() {
  require_repo_layout

  # Repeat coordinated control-plane assurance across all three real domains.
  # Candidate classification remains separate from shell operational status.
  run \
    "$REPO_DIR/scripts/run_s2_multidomain_autonomous_assurance_foundation.sh" \
    "${PHASE17_FOUNDATION_OUTPUT_DIR:-$REPO_DIR/results/S2/multidomain-assurance-foundation-$(date -u +%Y%m%dT%H%M%SZ)}"
}


run_s2_multidomain_autonomous_assurance_timing_validation() {
  require_repo_layout

  # Validate immutable calibrated bounds across a mirrored fault-time matrix.
  # The validation runner never derives new limits from its own observations.
  run \
    "$REPO_DIR/scripts/run_s2_multidomain_autonomous_assurance_timing_validation.sh" \
    "${PHASE17_TIMING_VALIDATION_OUTPUT_DIR:-$REPO_DIR/results/S2/multidomain-assurance-timing-validation-$(date -u +%Y%m%dT%H%M%SZ)}"
}


run_s2_multidomain_autonomous_assurance_final_timing_validation() {
  require_repo_layout

  # Validate the recalibrated end-to-end detection bound and its explicit
  # sub-metrics on an independent mirrored matrix. No bound is derived from
  # the validation observations produced by this runner.
  run \
    "$REPO_DIR/scripts/run_s2_multidomain_autonomous_assurance_final_timing_validation.sh" \
    "${PHASE17_FINAL_TIMING_VALIDATION_OUTPUT_DIR:-$REPO_DIR/results/S2/multidomain-assurance-final-timing-validation-$(date -u +%Y%m%dT%H%M%SZ)}"
}


run_s2_multidomain_selective_assurance() {
  require_repo_layout

  local output_dir="${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_OUTPUT_DIR:-$REPO_DIR/results/S2/multidomain-selective-assurance-$(date -u +%Y%m%dT%H%M%SZ)}"

  # Reuse the certified Phase 17 domain adapters and MAD controller while the
  # Phase 18 scenario varies only the independent fault subset and the bounded
  # synthetic test rejection policy.
  run_python_script_as_root \
    "$REPO_DIR/scripts/s2_multidomain_selective_assurance.py" \
      orchestrate \
      --output-dir "$output_dir" \
      --condition-id "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_CONDITION_ID:-manual}" \
      --assurance-profile-id "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_PROFILE_ID:-phase18-s2-multidomain-selective-assurance-foundation-v1}" \
      --fault-domains "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_FAULT_DOMAINS-A,B,C}" \
      --synthetic-rejection-domain "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_SYNTHETIC_REJECTION_DOMAIN:-}" \
      --synthetic-rejection-count "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_SYNTHETIC_REJECTION_COUNT:-0}" \
      --timing-candidate-policy "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_TIMING_CANDIDATE_POLICY:-observe-only}" \
      --linux-device "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_LINUX_DEVICE:-l2i-md-a0}" \
      --linux-peer "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_LINUX_PEER:-l2i-md-a1}" \
      --netconf-host "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_NETCONF_HOST:-127.0.0.1}" \
      --netconf-port "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_NETCONF_PORT:-$NETCONF_PORT}" \
      --netconf-username "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_NETCONF_USERNAME:-$NETCONF_USER}" \
      --netconf-key "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_NETCONF_KEY:-$NETCONF_KEY}" \
      --netconf-timeout-s "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_NETCONF_TIMEOUT_S:-5}" \
      --p4-addr "$P4_ADDR" \
      --p4-host 127.0.0.1 \
      --p4-port "$P4_PORT" \
      --device-id 0 \
      --p4-outdir /tmp/l2i_minimal \
      --group "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_GROUP:-239.1.1.1}" \
      --group-id "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_GROUP_ID:-1}" \
      --multicast-ports 1 2 \
      --multicast-port "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_MULTICAST_PORT:-5001}" \
      --observer-election-low "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_OBSERVER_ELECTION_LOW:-18100}" \
      --injector-election-low "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_INJECTOR_ELECTION_LOW:-18110}" \
      --initial-cleanup-election-low "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_INITIAL_CLEANUP_ELECTION_LOW:-18180}" \
      --initial-program-election-low "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_INITIAL_PROGRAM_ELECTION_LOW:-18190}" \
      --remediation-election-low "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_REMEDIATION_ELECTION_LOW:-18120}" \
      --cleanup-election-low "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_CLEANUP_ELECTION_LOW:-18990}" \
      --qos-class "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_QOS_CLASS:-prio10}" \
      --capacity-mbps "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_CAPACITY_MBPS:-3}" \
      --minimum-mbps "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_MINIMUM_MBPS:-2}" \
      --maximum-mbps "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_MAXIMUM_MBPS:-3}" \
      --fault-after-s "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_FAULT_AFTER_S:-4}" \
      --no-fault-observation-s "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_NO_FAULT_OBSERVATION_S:-3}" \
      --injector-completion-timeout-s "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_INJECTOR_COMPLETION_TIMEOUT_S:-12}" \
      --assurance-recovery-timeout-s "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_RECOVERY_TIMEOUT_S:-18}" \
      --maximum-detection-ms "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_MAX_DETECTION_MS:-4000}" \
      --maximum-control-plane-recovery-ms "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_MAX_CONTROL_PLANE_RECOVERY_MS:-3750}" \
      --maximum-total-reconciliation-ms "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_MAX_TOTAL_RECONCILIATION_MS:-6250}" \
      --assurance-poll-interval-s "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_POLL_INTERVAL_S:-0.05}" \
      --assurance-drift-confirmations "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_DRIFT_CONFIRMATIONS:-3}" \
      --assurance-convergence-confirmations "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_CONVERGENCE_CONFIRMATIONS:-2}" \
      --assurance-maximum-remediation-attempts "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_MAX_REMEDIATION_ATTEMPTS:-3}" \
      --assurance-initial-backoff-s "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_INITIAL_BACKOFF_S:-0.05}" \
      --assurance-backoff-multiplier "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_BACKOFF_MULTIPLIER:-2}" \
      --assurance-maximum-backoff-s "${S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_MAX_BACKOFF_S:-0.5}"

  local rc=$?
  echo "S2_MULTIDOMAIN_SELECTIVE_ASSURANCE_OUTPUT_DIR=$output_dir"
  return "$rc"
}


run_s2_multidomain_selective_assurance_foundation() {
  require_repo_layout

  # Run the six-condition Phase 18 foundation: no-fault control, three
  # single-domain drifts, simultaneous drift, and one synthetic partial reject.
  run \
    "$REPO_DIR/scripts/run_s2_multidomain_selective_assurance_foundation.sh" \
    "${PHASE18_FOUNDATION_OUTPUT_DIR:-$REPO_DIR/results/S2/multidomain-selective-assurance-foundation-$(date -u +%Y%m%dT%H%M%SZ)}"
}


run_s2_multidomain_selective_assurance_validation() {
  require_repo_layout

  # Run the mirrored twelve-condition Phase 18 validation matrix. Functional
  # selectivity and bounded synthetic retry are enforced; timing remains
  # observe-only because the Phase 17 timing profile is already certified.
  run \
    "$REPO_DIR/scripts/run_s2_multidomain_selective_assurance_validation.sh" \
    "${PHASE18_VALIDATION_OUTPUT_DIR:-$REPO_DIR/results/S2/multidomain-selective-assurance-validation-$(date -u +%Y%m%dT%H%M%SZ)}"
}


run_s2_p4_state_recovery_foundation() {
  require_repo_layout

  # The foundation runner repeats the state-deletion experiment and aggregates
  # recovery, delivery, and continuity evidence without treating a scientific
  # candidate decision as an operational shell failure.
  run \
    "$REPO_DIR/scripts/run_s2_p4_state_recovery_foundation.sh" \
    "${PHASE15_FOUNDATION_OUTPUT_DIR:-$REPO_DIR/results/S2/state-recovery-foundation-$(date -u +%Y%m%dT%H%M%SZ)}"
}


run_s2_p4_state_recovery_validation() {
  require_repo_layout

  # Exercise a mirrored timing matrix so recovery is not validated at only one
  # sender phase or one fault hold duration. The runner preserves every row and
  # performs an aggregate timing and delivery classification.
  run \
    "$REPO_DIR/scripts/run_s2_p4_state_recovery_validation.sh" \
    "${PHASE15_VALIDATION_OUTPUT_DIR:-$REPO_DIR/results/S2/state-recovery-validation-$(date -u +%Y%m%dT%H%M%SZ)}"
}

run_s2_p4_qos_contention_semantic_validation() {
  require_repo_layout

  # Run the validated, counterbalanced baseline/adapt matrix. The runner invokes
  # the per-mode setup command and stores every execution under one evidence
  # directory so the comparison can be reproduced without manual ordering.
  local output_dir="${PHASE14_SEMANTIC_OUTPUT_DIR:-$REPO_DIR/results/S2/qos-contention-semantic-$(date -u +%Y%m%dT%H%M%SZ)}"

  run \
    "$REPO_DIR/scripts/run_s2_p4_qos_contention_semantic_validation.sh" \
    "$output_dir"
}

validate_phase19_experiment_contract() {
  require_repo_layout

  # Validate the shared execution identity and artifact persistence contract.
  # This action is static and does not start services or execute S1/S2 traffic.
  run env \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON_BIN" \
    "$REPO_DIR/scripts/validate_phase19_experiment_contract.py"
}

validate_phase19_s1_static() {
  require_repo_layout

  # Validate the canonical S1 source, topology, optional-bound semantics, and
  # dry-run Linux TC plans.  This action does not create namespaces, start
  # services, invoke S1 traffic, or contact NETCONF/P4Runtime endpoints.
  run env \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}" \
    "$PYTHON_BIN" \
    "$REPO_DIR/scripts/validate_phase19_s1_static.py"
}

# -------------------------------
# verificações rápidas
# -------------------------------
verify_python_imports() {
  verify_python_lock
  run "$PIP_BIN" check
  run "$PYTHON_BIN" - <<'PY'
import cryptography
import grpc
import ncclient
import paramiko
import yaml
from p4.v1 import p4runtime_pb2
from p4runtime_sh.shell import P4RuntimeClient
print("VERIFY_PYTHON_OK")
PY
}

verify_services() {
  port_listening "$NETCONF_PORT" || { err "NETCONF fora de escuta."; exit 1; }
  wait_for_port "$P4_PORT" 2 || { err "P4Runtime fora de escuta."; exit 1; }

  if [[ "$P4_REQUIRE_THRIFT" == "1" ]]; then
    wait_for_port "$P4_THRIFT_PORT" 2 || { err "P4 Thrift obrigatório fora de escuta."; exit 1; }
  fi

  if port_listening "$P4_THRIFT_PORT"; then
    info "VERIFY_SERVICES_OK (NETCONF, P4Runtime e Thrift)."
  else
    info "VERIFY_SERVICES_OK (NETCONF e P4Runtime; Thrift opcional indisponível)."
  fi
}

collect_provenance() {
  run "$REPO_DIR/scripts/collect_provenance.sh" "${PROVENANCE_FILE:-$RESULTS_DIR/provenance/environment-provenance.txt}"
}

# -------------------------------
# fluxo completo
# -------------------------------
all() {
  require_repo_layout
  assert_repo_files

  info "REPO_DIR=$REPO_DIR"
  info "NET_SRC_DIR=$NET_SRC_DIR"
  info "VENV_DIR=$VENV_DIR"
  info "MAKE_JOBS=$MAKE_JOBS"
  info "P4_ADDR=$P4_ADDR"

  apt_base
  assert_system_tools
  verify_system_dependency_versions
  ensure_dirs
  python_env
  build_p4
  build_netconf
  configure_netconf
  start_real_services
  verify_python_imports
  verify_services
  collect_provenance

  info "Bootstrap finalizado."
}

# Machine-readable taxonomy audited by tests/test_postcert_s2_operational.py.
# S2_TAXONOMY run_s2_real canonical_scenario
# S2_TAXONOMY run_s2_p4_dataplane_smoke specialized_validation_profile
# S2_TAXONOMY run_s2_p4_qos_contention specialized_validation_profile
# S2_TAXONOMY run_s2_p4_qos_contention_semantic_validation specialized_validation_profile
# S2_TAXONOMY run_s2_p4_state_recovery specialized_validation_profile
# S2_TAXONOMY run_s2_p4_state_recovery_foundation specialized_validation_profile
# S2_TAXONOMY run_s2_p4_state_recovery_validation specialized_validation_profile
# S2_TAXONOMY run_s2_p4_autonomous_assurance specialized_validation_profile
# S2_TAXONOMY run_s2_p4_autonomous_assurance_foundation specialized_validation_profile
# S2_TAXONOMY run_s2_p4_autonomous_assurance_validation specialized_validation_profile
# S2_TAXONOMY run_s2_multidomain_autonomous_assurance specialized_validation_profile
# S2_TAXONOMY run_s2_multidomain_autonomous_assurance_foundation specialized_validation_profile
# S2_TAXONOMY run_s2_multidomain_autonomous_assurance_timing_validation specialized_validation_profile
# S2_TAXONOMY run_s2_multidomain_autonomous_assurance_final_timing_validation specialized_validation_profile
# S2_TAXONOMY run_s2_multidomain_selective_assurance specialized_validation_profile
# S2_TAXONOMY run_s2_multidomain_selective_assurance_foundation specialized_validation_profile
# S2_TAXONOMY run_s2_multidomain_selective_assurance_validation specialized_validation_profile

usage() {
  cat <<EOF
Uso: ./setup_all.sh <acao>

Ações principais:
  all
  apt_base
  python_env
  build_p4
  build_netconf
  configure_netconf
  start_real_services
  stop_real_services
  verify_python_imports
  verify_services
  verify_system_dependency_versions
  collect_provenance
  run_s1_real
  run_s1_mock
  run_s2_real  [canonical_scenario: único cenário/engine S2 canônico]
  cleanup

Ações internas úteis — perfis especializados de validação S2, NONCANONICAL:
  build_sysrepo
  build_libnetconf2
  build_netopeer2
  build_pi
  build_bmv2
  build_p4c
  push_p4_pipeline
  run_s2_p4_dataplane_smoke
  run_s2_p4_qos_contention
  run_s2_p4_qos_contention_semantic_validation
  run_s2_p4_state_recovery
  run_s2_p4_state_recovery_foundation
  run_s2_p4_state_recovery_validation
  run_s2_p4_autonomous_assurance
  run_s2_p4_autonomous_assurance_foundation
  run_s2_p4_autonomous_assurance_validation
  run_s2_multidomain_autonomous_assurance
  run_s2_multidomain_autonomous_assurance_foundation
  run_s2_multidomain_autonomous_assurance_timing_validation
  run_s2_multidomain_autonomous_assurance_final_timing_validation
  run_s2_multidomain_selective_assurance
  run_s2_multidomain_selective_assurance_foundation
  run_s2_multidomain_selective_assurance_validation
  validate_phase19_experiment_contract
  validate_phase19_s1_static
  cleanup_topologies_only

Variáveis úteis:
  L2I_REPO_DIR=$HOME/l2i-dsl
  DEPENDENCY_LOCK=$HOME/l2i-dsl/config/dependencies.env
  NET_SRC_DIR=$HOME/l2i-src
  DEV_DIR=$HOME/l2i-dev
  VENV_DIR=$HOME/l2i-dev/venv
  PYTHON_REQUIREMENTS_LOCK=$HOME/l2i-dsl/requirements/python-runtime.lock
  MAKE_JOBS=$(nproc 2>/dev/null || echo 2)
  NETCONF_USER=netconf
  NETCONF_KEY=$HOME/.ssh/l2i_netconf_key
  NETCONF_PORT=830
  NETCONF_LISTEN_ADDRESS=127.0.0.1
  NETCONF_ENDPOINT_NAME=default-ssh
  P4_PORT=9559
  P4_THRIFT_PORT=9090
  P4_ADDR=127.0.0.1:9559
  P4_START_TIMEOUT=20
  BMV2_LOG_FILE=/tmp/l2i_minimal/bmv2.log
  DRY_RUN=1
EOF
}

if [[ "${L2I_SETUP_LIB_ONLY:-0}" == "1" ]]; then
  return 0 2>/dev/null || exit 0
fi

case "${1:-}" in
  all) all ;;
  apt_base) apt_base ;;
  python_env) python_env ;;
  build_p4) build_p4 ;;
  build_netconf) build_netconf ;;
  build_sysrepo) build_sysrepo ;;
  build_libnetconf2) build_libnetconf2 ;;
  build_netopeer2) build_netopeer2 ;;
  build_pi) build_pi ;;
  build_bmv2) build_bmv2 ;;
  build_p4c) build_p4c ;;
  configure_netconf) configure_netconf ;;
  start_real_services) start_real_services ;;
  stop_real_services) stop_real_services ;;
  start_netconf) start_netconf ;;
  stop_netconf) stop_netconf ;;
  start_p4) start_p4 ;;
  stop_p4) stop_p4 ;;
  push_p4_pipeline) push_p4_pipeline ;;
  verify_python_imports) verify_python_imports ;;
  verify_services) verify_services ;;
  verify_system_dependency_versions) verify_system_dependency_versions ;;
  collect_provenance) collect_provenance ;;
  run_s1_real) run_s1_real ;;
  run_s1_mock) run_s1_mock ;;
  run_s2_real) run_s2_real ;;
  run_s2_p4_dataplane_smoke) run_s2_p4_dataplane_smoke ;;
  run_s2_p4_qos_contention) run_s2_p4_qos_contention ;;
  run_s2_p4_qos_contention_semantic_validation) run_s2_p4_qos_contention_semantic_validation ;;
  run_s2_p4_state_recovery) run_s2_p4_state_recovery ;;
  run_s2_p4_state_recovery_foundation) run_s2_p4_state_recovery_foundation ;;
  run_s2_p4_state_recovery_validation) run_s2_p4_state_recovery_validation ;;
  run_s2_p4_autonomous_assurance) run_s2_p4_autonomous_assurance ;;
  run_s2_p4_autonomous_assurance_foundation) run_s2_p4_autonomous_assurance_foundation ;;
  run_s2_p4_autonomous_assurance_validation) run_s2_p4_autonomous_assurance_validation ;;
  run_s2_multidomain_autonomous_assurance) run_s2_multidomain_autonomous_assurance ;;
  run_s2_multidomain_autonomous_assurance_foundation) run_s2_multidomain_autonomous_assurance_foundation ;;
  run_s2_multidomain_autonomous_assurance_timing_validation) run_s2_multidomain_autonomous_assurance_timing_validation ;;
  run_s2_multidomain_autonomous_assurance_final_timing_validation) run_s2_multidomain_autonomous_assurance_final_timing_validation ;;
  run_s2_multidomain_selective_assurance) run_s2_multidomain_selective_assurance ;;
  run_s2_multidomain_selective_assurance_foundation) run_s2_multidomain_selective_assurance_foundation ;;
  run_s2_multidomain_selective_assurance_validation) run_s2_multidomain_selective_assurance_validation ;;
  validate_phase19_experiment_contract) validate_phase19_experiment_contract ;;
  validate_phase19_s1_static) validate_phase19_s1_static ;;
  cleanup_topologies_only) cleanup_topologies_only ;;
  cleanup) cleanup ;;
  *) usage; exit 1 ;;
esac
