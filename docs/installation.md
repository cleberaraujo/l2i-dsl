# ⚙️ Instalação e Preparação do Ambiente

🏠 [README](../README.md) · 🧪 [Teste mínimo](minimal_test.md) · 📊 [Experimentos](experiments.md) · 🔁 [Pós-reboot](runtime_after_reboot.md) · 🖥️ [VM](vm.md)

---

## 🎯 Objetivo

Este documento descreve como preparar o ambiente necessário para execução do artefato, incluindo:

* instalação automatizada (recomendada)
* instalação manual (transparência e controle)
* organização do ambiente
* preparação para execução dos cenários

---

# 🚀 1. Caminho recomendado (instalação automática)

```bash id="inst_auto01"
chmod +x setup_all.sh
./setup_all.sh all
```

Este é o método recomendado para avaliação do artefato.

---

## 🔧 O que o script realiza

O script `setup_all.sh` executa automaticamente:

* instalação de dependências do sistema
* criação do ambiente virtual (`~/l2i-dev/venv`)
* instalação de dependências Python
* compilação do stack P4 (PI, bmv2, p4c)
* compilação do stack NETCONF (libyang, sysrepo, libnetconf2, Netopeer2)
* configuração do usuário `netconf`
* configuração de autenticação SSH
* instalação do modelo YANG `l2i-qos`
* aplicação de política NACM permissiva
* validação do ambiente

---

## 🔁 Reexecução segura

O script é **idempotente**:

```bash id="inst_auto02"
./setup_all.sh all
```

Pode ser reexecutado em caso de falha, interrupção ou dúvida sobre o estado do ambiente.

---

## ⏱️ Tempo esperado

| Etapa                | Tempo típico |
| -------------------- | ------------ |
| Dependências sistema | ~2–5 min     |
| Build P4             | ~20–60 min   |
| Build NETCONF        | ~10–30 min   |
| Total                | ~30–90 min   |

⚠️ Observação: em máquinas com poucos recursos, o tempo pode ser significativamente maior.

👉 Alternativa recomendada: uso da VM pré-configurada
📖 [docs/vm.md](vm.md)

---

# 🧩 2. Organização do ambiente

O artefato assume a seguinte estrutura:

```bash id="inst_layout01"
~/l2i-dsl        # repositório clonado
~/l2i-src        # código-fonte de dependências
~/l2i-dev/venv   # ambiente virtual Python
```

Essa separação permite:

* isolamento entre código e dependências
* reinstalações independentes
* maior reprodutibilidade

---

# 🛠️ 3. Instalação manual (controle completo)

A instalação manual é útil para:

* depuração
* inspeção científica
* customização do ambiente

---

## 3.1 Clonar repositório

```bash id="inst_manual01"
git clone https://github.com/cleberaraujo/l2i-dsl.git
cd l2i-dsl
```

---

## 3.2 Dependências do sistema

```bash id="inst_manual02"
sudo apt update
sudo apt install -y \
  python3 python3-venv python3-pip \
  git curl build-essential cmake pkg-config \
  iproute2 iputils-ping net-tools iperf3 fping graphviz \
  protobuf-compiler protobuf-compiler-grpc \
  thrift-compiler libthrift-dev libnanomsg-dev \
  libgrpc++-dev libgrpc-dev
```

---

## 3.3 Ambiente Python

```bash id="inst_manual03"
python3 -m venv ~/l2i-dev/venv
source ~/l2i-dev/venv/bin/activate

pip install --upgrade pip setuptools wheel
pip install jsonschema pyyaml grpcio protobuf==3.20.3 \
            ncclient cryptography paramiko \
            grpcio-tools p4runtime-shell
```
---

## 3.4 Dependências NETCONF

```bash id="inst_manual04"
sudo apt install -y \
  libssh-dev libssl-dev \
  libcurl4-openssl-dev \
  libpcre2-dev \
  libprotobuf-c-dev protobuf-c-compiler \
  libsystemd-dev \
  libavl-dev libev-dev \
  libsqlite3-dev
```
---

## 3.5 Dependências P4
```bash id="inst_manual05"
sudo apt install -y \
  libboost-dev libboost-system-dev libboost-filesystem-dev \
  libboost-program-options-dev libboost-thread-dev \
  libboost-test-dev libboost-iostreams-dev \
  libboost-graph-dev libboost-regex-dev \
  libfl-dev libgc-dev bison flex \
  libreadline-dev libgmp-dev \
  libpcap-dev \
  thrift-compiler libthrift-dev \
  libnanomsg-dev \
  libgrpc++-dev libgrpc-dev
```

---

## 3.6 Stack P4

Executado em `~/l2i-src`:

```bash
mkdir -p ~/l2i-src
cd ~/l2i-src
```

### PI

```bash
git clone https://github.com/p4lang/PI.git
cd PI
./autogen.sh
./configure --with-proto
make -j2
sudo make install
sudo ldconfig
```

---

### Behavioral Model (bmv2)

```bash
cd ~/l2i-src
git clone https://github.com/p4lang/behavioral-model.git
cd behavioral-model
./autogen.sh
./configure --with-pi
make -j2
sudo make install
sudo ldconfig
```

---

### p4c

```bash
cd ~/l2i-src
git clone https://github.com/p4lang/p4c.git
cd p4c
mkdir build && cd build
cmake ..
make -j2
sudo make install
sudo ldconfig
```
---

## 3.7 Stack NETCONF

Executado em `~/l2i-src`:

### libyang

```bash
git clone https://github.com/CESNET/libyang.git
cd libyang
mkdir build && cd build
cmake ..
make -j2
sudo make install
sudo ldconfig
```

---

### sysrepo

```bash
cd ~/l2i-src
git clone https://github.com/sysrepo/sysrepo.git
cd sysrepo
mkdir build && cd build
cmake ..
make -j2
sudo make install
sudo ldconfig
```

---

### libnetconf2

```bash
cd ~/l2i-src
git clone https://github.com/CESNET/libnetconf2.git
cd libnetconf2
mkdir build && cd build
cmake ..
make -j2
sudo make install
sudo ldconfig
```

---

### Netopeer2

```bash
cd ~/l2i-src
git clone https://github.com/CESNET/Netopeer2.git
cd Netopeer2
mkdir build && cd build
cmake ..
make -j2
sudo make install
sudo ldconfig
```

---

## 3.8 Configuração NETCONF

```bash id="inst_netconf01"
sudo useradd --system --shell /usr/sbin/nologin \
             --home-dir /var/lib/netconf \
             --create-home netconf
```

```bash id="inst_netconf02"
ssh-keygen -t rsa -b 2048 -f ~/.ssh/l2i_netconf_key -N ""
```

```bash id="inst_netconf03"
sudo mkdir -p /var/lib/netconf/.ssh
sudo cp ~/.ssh/l2i_netconf_key.pub /var/lib/netconf/.ssh/authorized_keys
sudo chown -R netconf:netconf /var/lib/netconf
```

---

## 3.9 YANG e NACM

```bash id="inst_netconf04"
sudo sysrepoctl -i yang/l2i-qos.yang -s yang/
```

```bash id="inst_netconf05"
sudo sysrepocfg --import=~/l2i-dsl/l2i-nacm-netconf-permit.xml -f xml -d running -m ietf-netconf-acm
sudo sysrepocfg --import=~/l2i-dsl/l2i-nacm-netconf-permit.xml -f xml -d startup -m ietf-netconf-acm
```

---

# 🔁 4. Após instalação

Nenhuma recompilação é necessária após a instalação inicial.

Uso típico:

```bash id="inst_post01"
source ~/l2i-dev/venv/bin/activate
./setup_all.sh start_real_services
```

---

# 🧪 5. Preparação para execução

Antes de executar cenários:

* ambiente Python ativo
* serviços em execução

```bash id="inst_ready01"
./setup_all.sh run_s1_real
```

---

# ⚠️ Observações importantes

* build P4 pode ser demorado
* memória limitada impacta compilação
* uso de `MAKE_JOBS=2` recomendado em ambientes restritos
* VM evita custo de build

---

# 📌 Conclusão

Este processo garante:

* instalação completa
* ambiente isolado
* execução reprodutível
* suporte a execução automática e manual

---

👉 Próximo passo: [docs/minimal_test.md](minimal_test.md)
