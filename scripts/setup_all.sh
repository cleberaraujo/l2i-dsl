#!/usr/bin/env bash
set -Eeuo pipefail

# L2i bootstrap script
# Usage examples:
#   ./setup_all.sh all
#   ./setup_all.sh start_real_services
#   ./setup_all.sh run_s1_real
#   ./setup_all.sh run_s2_real
#   MAKE_JOBS=2 ./setup_all.sh build_p4
#   SYSREPO_REF=<sha-or-tag> LIBNETCONF2_REF=<sha-or-tag> NETOPEER2_REF=<sha-or-tag> ./setup_all.sh build_netconf

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR" && pwd)"
NET_DEV_DIR="$(cd "$REPO_DIR/.." && pwd)"
HOME_DIR="${HOME}"
NET_SRC_DIR="${NET_SRC_DIR:-$HOME_DIR/net-src}"
VENV_DIR="${VENV_DIR:-$NET_DEV_DIR/venv}"
MAKE_JOBS="${MAKE_JOBS:-2}"

# Refs pinned from validated setup where available.
PI_REF="${PI_REF:-5689c91a8a7423781267b27d8b166c49a53904ff}"
BMV2_REF="${BMV2_REF:-e6f4501a63ccb040d21a6d0c4dc333c593c77677}"
P4C_REF="${P4C_REF:-8c4420e21f38554e568c2028db9254e71cf9d87f}"
LIBYANG_REF="${LIBYANG_REF:-v5.4.9}"
SYSREPO_REF="${SYSREPO_REF:-}"
LIBNETCONF2_REF="${LIBNETCONF2_REF:-}"
NETOPEER2_REF="${NETOPEER2_REF:-e9f97f152fed551673001315d617d4c197ba0ee0}"

NETCONF_USER="${NETCONF_USER:-netconf}"
NETCONF_HOME="${NETCONF_HOME:-/var/lib/netconf}"
NETCONF_KEY="${NETCONF_KEY:-$HOME_DIR/.ssh/l2i_netconf_key}"
NETCONF_PORT="${NETCONF_PORT:-830}"
P4_ADDR="${P4_ADDR:-127.0.0.1:9559}"
P4_PORT="${P4_PORT:-9559}"
PYTHON_BIN="${PYTHON_BIN:-$VENV_DIR/bin/python}"
PIP_BIN="${PIP_BIN:-$VENV_DIR/bin/pip}"

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '[info] %s\n' "$*"; }
warn() { printf '[warn] %s\n' "$*"; }
err() { printf '[erro] %s\n' "$*" >&2; }
run() { info "$*"; "$@"; }

require_repo_layout() {
  [[ -d "$REPO_DIR/scripts" && -d "$REPO_DIR/scenarios" && -d "$REPO_DIR/l2i" ]] || {
    err "Execute este script dentro do repositório ~/net-dev/dsl."; exit 1;
  }
}

ensure_sudo() {
  sudo -v
}

ensure_apt_base() {
  ensure_sudo
  run sudo apt update
  run sudo apt install -y \
    python3 python3-venv python3-pip \
    git curl build-essential cmake pkg-config autoconf automake libtool \
    iproute2 iputils-ping net-tools iperf3 fping \
    graphviz protobuf-compiler \
    libssh-dev libssl-dev libcurl4-openssl-dev libpcre2-dev \
    libprotobuf-c-dev protobuf-c-compiler libsystemd-dev libavl-dev libev-dev libsqlite3-dev \
    libboost-dev libboost-system-dev libboost-filesystem-dev libboost-program-options-dev \
    libboost-thread-dev libboost-test-dev libboost-iostreams-dev libboost-graph-dev libboost-regex-dev \
    libfl-dev libgc-dev bison flex libreadline-dev libgmp-dev libpcap-dev \
    thrift-compiler libthrift-dev libnanomsg-dev libgrpc++-dev libgrpc-dev
}

ensure_venv() {
  if [[ ! -x "$PYTHON_BIN" ]]; then
    run python3 -m venv "$VENV_DIR"
  fi
  run "$PIP_BIN" install --upgrade pip setuptools wheel
  # Keep runtime working as validated. grpcio-tools may warn about protobuf, but runtime remains functional.
  run "$PIP_BIN" install jsonschema pyyaml grpcio protobuf==3.20.3 ncclient cryptography paramiko
  run "$PIP_BIN" install grpcio-tools
  run "$PIP_BIN" install p4runtime-shell
}

clone_or_update() {
  local url="$1" dir="$2" ref="${3:-}"
  if [[ ! -d "$dir/.git" ]]; then
    run git clone "$url" "$dir"
  fi
  run git -C "$dir" fetch --tags --all
  if [[ -n "$ref" ]]; then
    run git -C "$dir" checkout "$ref"
  fi
  run git -C "$dir" submodule update --init --recursive
}

clone_sources() {
  mkdir -p "$NET_SRC_DIR"
  clone_or_update https://github.com/p4lang/PI.git "$NET_SRC_DIR/PI" "$PI_REF"
  clone_or_update https://github.com/p4lang/behavioral-model.git "$NET_SRC_DIR/behavioral-model" "$BMV2_REF"
  clone_or_update https://github.com/p4lang/p4c.git "$NET_SRC_DIR/p4c" "$P4C_REF"
  clone_or_update https://github.com/CESNET/libyang.git "$NET_SRC_DIR/libyang" "$LIBYANG_REF"
  clone_or_update https://github.com/sysrepo/sysrepo.git "$NET_SRC_DIR/sysrepo" "$SYSREPO_REF"
  clone_or_update https://github.com/CESNET/libnetconf2.git "$NET_SRC_DIR/libnetconf2" "$LIBNETCONF2_REF"
  clone_or_update https://github.com/CESNET/Netopeer2.git "$NET_SRC_DIR/Netopeer2" "$NETOPEER2_REF"
}

build_pi() {
  local d="$NET_SRC_DIR/PI"
  run bash -lc "cd '$d' && ./autogen.sh && ./configure --with-proto && make -j$MAKE_JOBS"
  run sudo bash -lc "cd '$d' && make install"
  run sudo ldconfig
}

build_bmv2() {
  local d="$NET_SRC_DIR/behavioral-model"
  run bash -lc "cd '$d' && ./autogen.sh && ./configure --with-pi && make -j$MAKE_JOBS"
  run sudo bash -lc "cd '$d' && make install"
  run sudo ldconfig
}

build_p4c() {
  local d="$NET_SRC_DIR/p4c"
  run bash -lc "cd '$d' && mkdir -p build && cd build && cmake .. && make -j$MAKE_JOBS"
  run sudo bash -lc "cd '$d/build' && make install"
  run sudo ldconfig
}

validate_p4() {
  command -v simple_switch_grpc >/dev/null || { err "simple_switch_grpc não encontrado."; exit 1; }
  command -v p4c >/dev/null || { err "p4c não encontrado."; exit 1; }
  run simple_switch_grpc --help >/dev/null
  run p4c --version
}

build_libyang() {
  local d="$NET_SRC_DIR/libyang"
  run bash -lc "cd '$d' && mkdir -p build && cd build && cmake .. && make -j$MAKE_JOBS"
  run sudo bash -lc "cd '$d/build' && make install"
  run sudo ldconfig
}

build_sysrepo() {
  local d="$NET_SRC_DIR/sysrepo"
  run bash -lc "cd '$d' && mkdir -p build && cd build && cmake .. && make -j$MAKE_JOBS"
  run sudo bash -lc "cd '$d/build' && make install"
  run sudo ldconfig
}

build_libnetconf2() {
  local d="$NET_SRC_DIR/libnetconf2"
  run bash -lc "cd '$d' && mkdir -p build && cd build && cmake .. && make -j$MAKE_JOBS"
  run sudo bash -lc "cd '$d/build' && make install"
  run sudo ldconfig
}

build_netopeer2() {
  local d="$NET_SRC_DIR/Netopeer2"
  run bash -lc "cd '$d' && mkdir -p build && cd build && cmake .. && make -j$MAKE_JOBS"
  run sudo bash -lc "cd '$d/build' && make install"
  run sudo ldconfig
}

validate_netconf_bins() {
  command -v sysrepoctl >/dev/null || { err "sysrepoctl não encontrado."; exit 1; }
  command -v netopeer2-server >/dev/null || command -v /usr/local/sbin/netopeer2-server >/dev/null || {
    err "netopeer2-server não encontrado."; exit 1;
  }
  run ldconfig -p | grep libyang
  run ldconfig -p | grep libsysrepo
  run ldconfig -p | grep libnetconf2
}

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
  info "Arquivo NACM atualizado em $nacm_file"
}

configure_netconf_auth() {
  ensure_sudo
  local ssh_dir="$NETCONF_HOME/.ssh"
  local key_pub="$NETCONF_KEY.pub"

  if ! getent passwd "$NETCONF_USER" >/dev/null; then
    run sudo useradd --system --shell /usr/sbin/nologin --home-dir "$NETCONF_HOME" --create-home "$NETCONF_USER"
  else
    run sudo usermod -s /usr/sbin/nologin -d "$NETCONF_HOME" "$NETCONF_USER"
    run sudo mkdir -p "$NETCONF_HOME"
  fi

  run sudo passwd -l "$NETCONF_USER" || true
  run sudo mkdir -p "$ssh_dir"
  run sudo chown -R "$NETCONF_USER":"$NETCONF_USER" "$NETCONF_HOME"
  run sudo chmod 700 "$NETCONF_HOME"
  run sudo chmod 700 "$ssh_dir"

  if [[ ! -f "$NETCONF_KEY" ]]; then
    mkdir -p "$(dirname "$NETCONF_KEY")"
    run ssh-keygen -t rsa -b 2048 -f "$NETCONF_KEY" -N ""
  fi

  run sudo install -o "$NETCONF_USER" -g "$NETCONF_USER" -m 600 "$key_pub" "$ssh_dir/authorized_keys"

  # Ensure netconf-server model knows this user and uses system authorized_keys.
  cat > /tmp/l2i-netconf-user.xml <<XML
<netconf-server xmlns="urn:ietf:params:xml:ns:yang:ietf-netconf-server">
  <listen>
    <endpoints>
      <endpoint>
        <name>default-ssh</name>
        <ssh>
          <ssh-server-parameters>
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
  run sudo sysrepocfg --edit=/tmp/l2i-netconf-user.xml -d running -f xml -m ietf-netconf-server
  run sudo sysrepocfg --edit=/tmp/l2i-netconf-user.xml -d startup -f xml -m ietf-netconf-server

  run sudo sysrepoctl -i "$REPO_DIR/yang/l2i-qos.yang" || true
  write_nacm_file
  run sudo sysrepocfg --import="$REPO_DIR/l2i-nacm-netconf-permit.xml" -f xml -d running -m ietf-netconf-acm
  run sudo sysrepocfg --import="$REPO_DIR/l2i-nacm-netconf-permit.xml" -f xml -d startup -m ietf-netconf-acm

  info "NETCONF auth configurado: user=$NETCONF_USER key=$NETCONF_KEY"
}

start_netopeer() {
  ensure_sudo
  if ss -ltn | grep -q ":${NETCONF_PORT} "; then
    info "Há algo escutando na porta ${NETCONF_PORT}; não vou iniciar outro servidor."
    return 0
  fi
  local netopeer_bin
  if command -v netopeer2-server >/dev/null; then
    netopeer_bin="$(command -v netopeer2-server)"
  else
    netopeer_bin="/usr/local/sbin/netopeer2-server"
  fi
  run sudo pkill netopeer2-server || true
  info "Iniciando $netopeer_bin em background..."
  sudo "$netopeer_bin" -d >/tmp/l2i_netopeer2.log 2>&1 &
  sleep 2
  if ! ss -ltn | grep -q ":${NETCONF_PORT} "; then
    err "Netopeer2 não abriu a porta ${NETCONF_PORT}. Veja /tmp/l2i_netopeer2.log"
    return 1
  fi
}

netconf_test() {
  start_netopeer
  "$PYTHON_BIN" - <<PY
from ncclient import manager
m = manager.connect(
    host='127.0.0.1',
    port=${NETCONF_PORT},
    username='${NETCONF_USER}',
    key_filename='${NETCONF_KEY}',
    hostkey_verify=False,
    allow_agent=False,
    look_for_keys=False,
)
m.close_session()
print('NETCONF OK')
PY
}

start_p4() {
  require_repo_layout
  run bash -lc "cd '$REPO_DIR' && ./scripts/p4_build_and_run.sh"
  run ss -ltnp | grep "$P4_PORT"
}

push_p4() {
  require_repo_layout
  run "$PYTHON_BIN" "$REPO_DIR/scripts/p4_push_pipeline.py" --addr "$P4_ADDR"
}

start_real_services() {
  start_netopeer
  start_p4
  push_p4
}

run_s1_real() {
  require_repo_layout
  run sudo "$PYTHON_BIN" "$REPO_DIR/scripts/s1_topology_setup.sh"
  run sudo "$PYTHON_BIN" -m scenarios.multidomain_s1 \
    --spec "$REPO_DIR/specs/valid/s1_unicast_qos.json" \
    --duration "${S1_DURATION:-10}" \
    --be-mbps "${S1_BE_MBPS:-30}" \
    --mode "${S1_MODE:-adapt}" \
    --backend real
}

run_s1_real() {
  require_repo_layout
  run sudo "$REPO_DIR/scripts/s1_topology_setup.sh"
  run sudo "$PYTHON_BIN" -m scenarios.multidomain_s1 \
    --spec "$REPO_DIR/specs/valid/s1_unicast_qos.json" \
    --duration "${S1_DURATION:-10}" \
    --be-mbps "${S1_BE_MBPS:-30}" \
    --mode "${S1_MODE:-adapt}" \
    --backend real
}

run_s1_mock() {
  require_repo_layout
  run sudo "$REPO_DIR/scripts/s1_topology_setup.sh"
  run sudo "$PYTHON_BIN" -m scenarios.multidomain_s1 \
    --spec "$REPO_DIR/specs/valid/s1_unicast_qos.json" \
    --duration "${S1_DURATION:-10}" \
    --be-mbps "${S1_BE_MBPS:-30}" \
    --mode "${S1_MODE:-adapt}" \
    --backend mock
}

run_s2_real() {
  require_repo_layout
  run sudo "$REPO_DIR/scripts/s2_topology_setup.sh"
  run sudo "$PYTHON_BIN" -m scenarios.multicast_s2_recovery_stable5 \
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
    --rtt-interval-ms 50 \
    --recovery-bin-ms 500 \
    --stable-k-bins 3
}

cleanup() {
  require_repo_layout
  run sudo "$REPO_DIR/scripts/s1_topology_cleanup.sh" || true
  run sudo "$REPO_DIR/scripts/s2_topology_cleanup.sh" || true
  run sudo "$REPO_DIR/scripts/cleanup_net.sh" || true
  run sudo pkill netopeer2-server || true
}

build_p4_stack() {
  clone_or_update https://github.com/p4lang/PI.git "$NET_SRC_DIR/PI" "$PI_REF"
  clone_or_update https://github.com/p4lang/behavioral-model.git "$NET_SRC_DIR/behavioral-model" "$BMV2_REF"
  clone_or_update https://github.com/p4lang/p4c.git "$NET_SRC_DIR/p4c" "$P4C_REF"
  build_pi
  build_bmv2
  build_p4c
  validate_p4
}

build_netconf_stack() {
  clone_or_update https://github.com/CESNET/libyang.git "$NET_SRC_DIR/libyang" "$LIBYANG_REF"
  clone_or_update https://github.com/sysrepo/sysrepo.git "$NET_SRC_DIR/sysrepo" "$SYSREPO_REF"
  clone_or_update https://github.com/CESNET/libnetconf2.git "$NET_SRC_DIR/libnetconf2" "$LIBNETCONF2_REF"
  clone_or_update https://github.com/CESNET/Netopeer2.git "$NET_SRC_DIR/Netopeer2" "$NETOPEER2_REF"
  build_libyang
  build_sysrepo
  build_libnetconf2
  build_netopeer2
  validate_netconf_bins
}

all() {
  require_repo_layout
  ensure_apt_base
  ensure_venv
  build_p4_stack
  build_netconf_stack
  configure_netconf_auth
  netconf_test
  info "Bootstrap concluído. Após reboot, use:"
  info "  source '$VENV_DIR/bin/activate'"
  info "  $(basename "$0") start_real_services"
  info "  $(basename "$0") run_s1_real"
}

usage() {
  cat <<USAGE
Uso: $(basename "$0") <acao>

Ações principais:
  all                   Instala base, venv, compila P4 + NETCONF e configura NETCONF.
  apt_base              Instala dependências de sistema.
  python_env            Cria/atualiza a venv e instala dependências Python.
  clone_sources         Clona/atualiza todos os fontes em $NET_SRC_DIR.
  build_p4              Compila PI, behavioral-model e p4c.
  build_netconf         Compila libyang, sysrepo, libnetconf2 e Netopeer2.
  configure_netconf     Cria usuário/chaves, instala l2i-qos e NACM.
  netconf_test          Testa NETCONF real via ncclient.
  start_real_services   Sobe Netopeer2, P4 e carrega o pipeline.
  run_s1_mock           Executa S1 curto em modo mock.
  run_s1_real           Executa S1 curto em modo real.
  run_s2_real           Executa S2 curto em modo real.
  cleanup               Limpa topologias e encerra serviços.

Variáveis úteis:
  MAKE_JOBS=2           Paralelismo de compilação (2 é seguro para pouca RAM).
  NET_SRC_DIR=...       Diretório dos fontes externos.
  VENV_DIR=...          Diretório da venv.
  NETCONF_USER=...      Usuário NETCONF dedicado (padrão: netconf).
  NETCONF_KEY=...       Caminho da chave SSH usada pelo backend B.
USAGE
}

main() {
  local action="${1:-usage}"
  case "$action" in
    all) all ;;
    apt_base) ensure_apt_base ;;
    python_env) ensure_venv ;;
    clone_sources) clone_sources ;;
    build_p4) build_p4_stack ;;
    build_netconf) build_netconf_stack ;;
    configure_netconf) configure_netconf_auth ;;
    netconf_test) netconf_test ;;
    start_real_services) start_real_services ;;
    run_s1_mock) run_s1_mock ;;
    run_s1_real) run_s1_real ;;
    run_s2_real) run_s2_real ;;
    cleanup) cleanup ;;
    usage|-h|--help) usage ;;
    *) err "Ação desconhecida: $action"; usage; exit 1 ;;
  esac
}

main "$@"
