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
  ss -H -ltn "sport = :$port" 2>/dev/null | grep -q .
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
    iproute2 iputils-ping net-tools iperf3 fping graphviz \
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

stop_netconf() {
  local pid=""
  local i

  if [[ -f "$NETCONF_PIDFILE" ]]; then
    pid="$(cat "$NETCONF_PIDFILE" 2>/dev/null || true)"
  fi

  if [[ "$DRY_RUN" == "1" ]]; then
    info "Encerraria o Netopeer2 e removeria $NETCONF_PIDFILE."
    return 0
  fi

  if [[ "$pid" =~ ^[0-9]+$ ]] && sudo kill -0 "$pid" 2>/dev/null; then
    info "Encerrando Netopeer2 pelo PID file (PID=$pid)."
    sudo kill -TERM "$pid" 2>/dev/null || true

    for ((i=1; i<=30; i++)); do
      if ! sudo kill -0 "$pid" 2>/dev/null; then
        break
      fi
      sleep 0.1
    done

    if sudo kill -0 "$pid" 2>/dev/null; then
      warn "Netopeer2 não encerrou com SIGTERM; enviando SIGKILL."
      sudo kill -KILL "$pid" 2>/dev/null || true
    fi
  fi

  # Fallback para execuções antigas sem PID file ou com wrappers sudo.
  sudo pkill -TERM -f '(^|/)netopeer2-server([[:space:]]|$)' 2>/dev/null || true
  sleep 0.2
  sudo pkill -KILL -f '(^|/)netopeer2-server([[:space:]]|$)' 2>/dev/null || true
  sudo rm -f "$NETCONF_PIDFILE"
}

start_netconf() {
  local netopeer_bin
  local launcher_pid
  local pid=""
  local process_args=""
  local i

  if command -v netopeer2-server >/dev/null 2>&1; then
    netopeer_bin="$(command -v netopeer2-server)"
  elif [[ -x /usr/local/sbin/netopeer2-server ]]; then
    netopeer_bin="/usr/local/sbin/netopeer2-server"
  else
    err "netopeer2-server não encontrado. Execute build_netconf antes."
    exit 1
  fi

  if port_listening "$NETCONF_PORT"; then
    info "NETCONF já está em execução na porta $NETCONF_PORT."
    return 0
  fi

  stop_netconf
  if [[ "$DRY_RUN" == "1" ]]; then
    return 0
  fi

  sudo rm -f "$NETCONF_PIDFILE"

  # O sudo cria processos auxiliares de monitoramento. Portanto, o PID do
  # launcher não é necessariamente o PID real do netopeer2-server. Iniciamos
  # pelo método comprovadamente funcional e, após a abertura da porta 830,
  # descobrimos o PID real pelo socket de escuta.
  nohup sudo -n \
    "$netopeer_bin" \
    -d \
    > "$NETCONF_LOG_FILE" 2>&1 \
    </dev/null &

  launcher_pid=$!

  for ((i=1; i<=50; i++)); do
    if port_listening "$NETCONF_PORT"; then
      break
    fi

    if ! kill -0 "$launcher_pid" 2>/dev/null; then
      break
    fi

    sleep 0.1
  done

  if ! port_listening "$NETCONF_PORT"; then
    err "NETCONF não abriu a porta $NETCONF_PORT. Verifique $NETCONF_LOG_FILE"
    tail -n 80 "$NETCONF_LOG_FILE" >&2 || true
    stop_netconf
    exit 1
  fi

  pid="$(
    sudo ss -H -ltnp "sport = :$NETCONF_PORT" 2>/dev/null \
      | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' \
      | head -n 1
  )"

  if [[ ! "$pid" =~ ^[0-9]+$ ]] \
     || ! sudo kill -0 "$pid" 2>/dev/null; then
    err "Não foi possível identificar o PID real do Netopeer2 na porta $NETCONF_PORT."
    tail -n 80 "$NETCONF_LOG_FILE" >&2 || true
    stop_netconf
    exit 1
  fi

  process_args="$(
    sudo ps -p "$pid" -o args= 2>/dev/null || true
  )"

  if [[ "$process_args" != *netopeer2-server* ]]; then
    err "O PID $pid associado à porta $NETCONF_PORT não corresponde ao Netopeer2: $process_args"
    stop_netconf
    exit 1
  fi

  printf '%s\n' "$pid" \
    | sudo tee "$NETCONF_PIDFILE" >/dev/null

  info "Netopeer2 ativo na porta $NETCONF_PORT (PID real=$pid; launcher=$launcher_pid)."
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
      --duration "${S1_DURATION:-10}" \
      --be-mbps "${S1_BE_MBPS:-30}" \
      --mode "${S1_MODE:-adapt}" \
      --backend real
}

run_s1_mock() {
  require_repo_layout
  run_with_cleanup_trap \
    "$REPO_DIR/scripts/s1_topology_setup.sh" \
    run_python_module_as_root scenarios.multidomain_s1 \
      --spec "$REPO_DIR/specs/valid/s1_unicast_qos.json" \
      --duration "${S1_DURATION:-10}" \
      --be-mbps "${S1_BE_MBPS:-30}" \
      --mode "${S1_MODE:-adapt}" \
      --backend mock
}

run_s2_real() {
  require_repo_layout
  run_with_cleanup_trap \
    "$REPO_DIR/scripts/s2_topology_setup.sh" \
    run_python_module_as_root scenarios.multicast_s2_recovery_stable5 \
      --spec "$REPO_DIR/specs/valid/s2_multicast_source_oriented.json" \
      --duration "${S2_DURATION:-10}" \
      --be-mbps "${S2_BE_MBPS:-80}" \
      --bwA "${S2_BWA:-40}" \
      --bwB "${S2_BWB:-100}" \
      --bwC "${S2_BWC:-100}" \
      --delay-ms "${S2_DELAY_MS:-1}" \
      --mode "${S2_MODE:-adapt}" \
      --backend real \
      --phase-splits "${S2_PHASE1:-3}" "${S2_PHASE2:-6}" \
      --event-name "${S2_EVENT_NAME:-join}" \
      --mcast-group-id "${S2_MCAST_GROUP_ID:-1}" \
      --mcast-dst "${S2_MCAST_DST:-239.1.1.1}" \
      --mcast-ports "${S2_MCAST_PORT0:-0}" "${S2_MCAST_PORT1:-1}" \
      --rtt-interval-ms "${S2_RTT_INTERVAL_MS:-50}" \
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

  # Rate validation is opt-in so that historical smoke invocations retain their
  # original replication-only semantics. Calibration and confirmation runners
  # enable the gate explicitly after supplying a validated pacing profile.
  if [[ "${S2_DP_REQUIRE_RATE_VALIDATION:-0}" == "1" ]]; then
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
      --pacing-mode "${S2_DP_PACING_MODE:-minimum_interval_no_catchup}" \
      --spin-threshold-us "${S2_DP_SPIN_THRESHOLD_US:-0}" \
      --sender-cpu "${S2_DP_SENDER_CPU:-none}" \
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
  run_s2_real
  cleanup

Ações internas úteis:
  build_sysrepo
  build_libnetconf2
  build_netopeer2
  build_pi
  build_bmv2
  build_p4c
  push_p4_pipeline
  run_s2_p4_dataplane_smoke
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
  cleanup_topologies_only) cleanup_topologies_only ;;
  cleanup) cleanup ;;
  *) usage; exit 1 ;;
esac
