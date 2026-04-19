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

PYTHON_BIN="${PYTHON_BIN:-$VENV_DIR/bin/python}"
PIP_BIN="${PIP_BIN:-$VENV_DIR/bin/pip}"

NETCONF_PORT="${NETCONF_PORT:-830}"
P4_PORT="${P4_PORT:-9559}"
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

# Commits que já foram usados no ambiente de reprodutibilidade anterior.
PI_REF="${PI_REF:-5689c91a8a7423781267b27d8b166c49a53904ff}"
BMV2_REF="${BMV2_REF:-e6f4501a63ccb040d21a6d0c4dc333c593c77677}"
P4C_REF="${P4C_REF:-8c4420e21f38554e568c2028db9254e71cf9d87f}"

# Sysrepo/libnetconf2/Netopeer2 podem ser pinados externamente, se desejado.
SYSREPO_REF="${SYSREPO_REF:-master}"
LIBNETCONF2_REF="${LIBNETCONF2_REF:-master}"
NETOPEER2_REF="${NETOPEER2_REF:-master}"

export DEBIAN_FRONTEND=noninteractive

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

  if git -C "$repo_dir" rev-parse --verify "$repo_ref" >/dev/null 2>&1; then
    run git -C "$repo_dir" checkout "$repo_ref"
  else
    run git -C "$repo_dir" checkout "origin/$repo_ref"
  fi

  run git -C "$repo_dir" submodule update --init --recursive
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
  ss -ltn "( sport = :$port )" | tail -n +2 | grep -q ":$port"
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
apt_base() {
  run sudo apt update
  run sudo apt install -y \
    ca-certificates curl git rsync \
    build-essential cmake ninja-build pkg-config \
    autoconf automake libtool libtool-bin \
    python3 python3-dev python3-venv python3-pip \
    iproute2 iputils-ping net-tools iperf3 fping graphviz \
    openssh-client openssl \
    protobuf-compiler protobuf-compiler-grpc \
    libprotobuf-dev libprotobuf-c-dev protobuf-c-compiler \
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
python_env() {
  ensure_dirs

  if [[ -x "$PYTHON_BIN" ]] && ! "$PYTHON_BIN" -V >/dev/null 2>&1; then
    warn "Venv inválida detectada em $VENV_DIR. Recriando."
    run rm -rf "$VENV_DIR"
  fi

  if [[ ! -x "$PYTHON_BIN" ]]; then
    run python3 -m venv "$VENV_DIR"
  fi

  run "$PIP_BIN" install --upgrade pip setuptools wheel
  run "$PIP_BIN" install \
    jsonschema pyyaml \
    grpcio protobuf==3.20.3 \
    ncclient cryptography paramiko \
    p4runtime-shell

  run "$PYTHON_BIN" - <<'PY'
import jsonschema, yaml, grpc, ncclient
from p4runtime_sh.shell import P4RuntimeClient
print("PYTHON_ENV_OK")
PY
}

# -------------------------------
# builds por código-fonte
# -------------------------------
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

build_p4c() {
  local src="$NET_SRC_DIR/p4c"
  if command -v p4c >/dev/null 2>&1; then
    info "p4c já encontrado — pulando build do p4c."
    return 0
  fi

  clone_or_update_git "$P4C_REPO" "$src" "$P4C_REF"
  cmake_build_install "$src" "$BUILD_DIR_P4C" \
    -DCMAKE_BUILD_TYPE=Release \
    -DENABLE_GTESTS=OFF \
    -DENABLE_P4TEST=OFF \
    -DENABLE_BMV2=ON
}

build_p4() {
  if command -v simple_switch_grpc >/dev/null 2>&1 && command -v p4c >/dev/null 2>&1; then
    info "Stack P4 já instalada — pulando build."
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

configure_netconf_server_xml() {
  local xml_file="/tmp/l2i-netconf-user.xml"
  cat > "$xml_file" <<XML
<netconf-server xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-server">
  <listen>
    <endpoints>
      <endpoint>
        <name>default-ssh</name>
        <ssh>
          <tcp-server-parameters>
            <local-address>0.0.0.0</local-address>
            <local-port>${NETCONF_PORT}</local-port>
          </tcp-server-parameters>
          <ssh-server-parameters>
            <server-identity>
              <host-key>
                <name>default-key</name>
                <public-key>
                  <keystore-reference xmlns="urn:ietf:params:xml:ns:yang:ietf-keystore">genkey</keystore-reference>
                </public-key>
              </host-key>
            </server-identity>
            <client-authentication>
              <users>
                <user>
                  <name>${NETCONF_USER}</name>
                  <public-keys>
                    <use-system-keys xmlns="urn:cesnet:libnetconf2-netconf-server"/>
                  </public-keys>
                </user>
              </users>
            </client-authentication>
          </ssh-server-parameters>
        </ssh>
      </endpoint>
    </endpoints>
  </listen>
</netconf-server>
XML
  echo "$xml_file"
}

configure_netconf() {
  require_repo_layout
  assert_repo_files

  if ! id -u "$NETCONF_USER" >/dev/null 2>&1; then
    run sudo useradd --system --shell /usr/sbin/nologin \
      --home-dir "$NETCONF_HOME" --create-home "$NETCONF_USER"
  else
    info "Usuário NETCONF já existe: $NETCONF_USER"
  fi

  if [[ ! -f "$NETCONF_KEY" ]]; then
    run ssh-keygen -t rsa -b 2048 -f "$NETCONF_KEY" -N ""
  fi

  run sudo mkdir -p "$NETCONF_HOME/.ssh"
  run sudo install -o "$NETCONF_USER" -g "$NETCONF_USER" -m 700 -d "$NETCONF_HOME/.ssh"
  run sudo install -o "$NETCONF_USER" -g "$NETCONF_USER" -m 600 "$NETCONF_KEY.pub" "$NETCONF_HOME/.ssh/authorized_keys"
  run sudo chown -R "$NETCONF_USER:$NETCONF_USER" "$NETCONF_HOME"

  if ! command -v sysrepoctl >/dev/null 2>&1; then
    err "sysrepoctl não encontrado. Execute build_netconf antes."
    exit 1
  fi

  if ! sudo sysrepoctl -l | awk '{print $1}' | grep -Fxq "l2i-qos"; then
    run sudo sysrepoctl -i "$REPO_DIR/yang/l2i-qos.yang" -s "$REPO_DIR/yang"
  else
    info "Módulo YANG l2i-qos já instalado."
  fi

  local server_xml
  server_xml="$(configure_netconf_server_xml)"
  run sudo sysrepocfg --edit="$server_xml" -d running -f xml -m ietf-netconf-server
  run sudo sysrepocfg --edit="$server_xml" -d startup -f xml -m ietf-netconf-server

  write_nacm_file
  run sudo sysrepocfg --import="$REPO_DIR/l2i-nacm-netconf-permit.xml" -f xml -d running -m ietf-netconf-acm
  run sudo sysrepocfg --import="$REPO_DIR/l2i-nacm-netconf-permit.xml" -f xml -d startup -m ietf-netconf-acm

  info "NETCONF configurado com usuário=$NETCONF_USER, chave=$NETCONF_KEY e módulo l2i-qos."
}

start_netconf() {
  local netopeer_bin
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

  run sudo pkill -f netopeer2-server || true
  if [[ "$DRY_RUN" == "1" ]]; then
    return 0
  fi

  sudo "$netopeer_bin" -d >/tmp/netopeer2-server.log 2>&1 &
  sleep 2

  port_listening "$NETCONF_PORT" || {
    err "NETCONF não abriu a porta $NETCONF_PORT. Verifique /tmp/netopeer2-server.log"
    exit 1
  }
}

# -------------------------------
# p4
# -------------------------------
push_p4_pipeline() {
  require_repo_layout
  assert_repo_files
  run "$PYTHON_BIN" "$REPO_DIR/scripts/p4_push_pipeline.py" --addr "$P4_ADDR"
}

start_p4() {
  require_repo_layout
  assert_repo_files

  if port_listening "$P4_PORT"; then
    info "P4 já está em execução na porta $P4_PORT."
  else
    run bash -lc "cd '$REPO_DIR' && ./scripts/p4_build_and_run.sh"
    port_listening "$P4_PORT" || {
      err "P4 não abriu a porta $P4_PORT."
      exit 1
    }
  fi
}

start_real_services() {
  require_repo_layout
  start_netconf
  start_p4
  push_p4_pipeline

  port_listening "$NETCONF_PORT" || { err "NETCONF não está em escuta."; exit 1; }
  port_listening "$P4_PORT" || { err "P4 não está em escuta."; exit 1; }

  info "Serviços reais ativos. NETCONF=:$NETCONF_PORT P4=:$P4_PORT"
}

# -------------------------------
# limpeza e execução de cenários
# -------------------------------
cleanup_topologies_only() {
  require_repo_layout
  run sudo "$REPO_DIR/scripts/s1_topology_cleanup.sh" || true
  run sudo "$REPO_DIR/scripts/s2_topology_cleanup.sh" || true
  run sudo "$REPO_DIR/scripts/cleanup_net.sh" || true
}

cleanup() {
  cleanup_topologies_only
  run sudo pkill -f netopeer2-server || true
}

run_python_module_as_root() {
  local module="$1"
  shift
  sudo -E \
    PYTHONPATH="$REPO_DIR${PYTHONPATH:+:$PYTHONPATH}" \
    PATH="$VENV_DIR/bin:$PATH" \
    "$PYTHON_BIN" -m "$module" "$@"
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
      --event-name join \
      --rtt-interval-ms "${S2_RTT_INTERVAL_MS:-50}" \
      --recovery-bin-ms "${S2_RECOVERY_BIN_MS:-500}" \
      --stable-k-bins "${S2_STABLE_K_BINS:-3}"
}

# -------------------------------
# verificações rápidas
# -------------------------------
verify_python_imports() {
  run "$PYTHON_BIN" - <<'PY'
import ncclient
from p4runtime_sh.shell import P4RuntimeClient
print("VERIFY_PYTHON_OK")
PY
}

verify_services() {
  port_listening "$NETCONF_PORT" || { err "NETCONF fora de escuta."; exit 1; }
  port_listening "$P4_PORT" || { err "P4 fora de escuta."; exit 1; }
  info "VERIFY_SERVICES_OK"
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
  ensure_dirs
  python_env
  build_p4
  build_netconf
  configure_netconf
  start_real_services
  verify_python_imports
  verify_services

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
  verify_python_imports
  verify_services
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
  cleanup_topologies_only

Variáveis úteis:
  L2I_REPO_DIR=$HOME/l2i-dsl
  NET_SRC_DIR=$HOME/l2i-src
  DEV_DIR=$HOME/l2i-dev
  VENV_DIR=$HOME/l2i-dev/venv
  MAKE_JOBS=$(nproc 2>/dev/null || echo 2)
  NETCONF_USER=netconf
  NETCONF_KEY=$HOME/.ssh/l2i_netconf_key
  NETCONF_PORT=830
  P4_PORT=9559
  P4_ADDR=127.0.0.1:9559
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
  push_p4_pipeline) push_p4_pipeline ;;
  verify_python_imports) verify_python_imports ;;
  verify_services) verify_services ;;
  run_s1_real) run_s1_real ;;
  run_s1_mock) run_s1_mock ;;
  run_s2_real) run_s2_real ;;
  cleanup_topologies_only) cleanup_topologies_only ;;
  cleanup) cleanup ;;
  *) usage; exit 1 ;;
esac
