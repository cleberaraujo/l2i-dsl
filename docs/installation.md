# ⚙️ Instalação e Preparação do Ambiente

🏠 [README](../README.md) · 🧪 [Teste mínimo](minimal_test.md) · 📊 [Experimentos](experiments.md) · 🔁 [Pós-reboot](runtime_after_reboot.md)

---

## 🎯 Objetivo

Este documento descreve como preparar o ambiente necessário para execução do artefato, incluindo:

* instalação automatizada completa
* instalação manual (transparência e depuração)
* organização do ambiente local
* preparação para execução dos cenários

---

# 🧩 Organização do ambiente

O artefato assume a seguinte estrutura fora do repositório:

```bash
~/l2i-dsl        # repositório clonado
~/l2i-src        # código-fonte de dependências (P4 e NETCONF)
~/l2i-dev/venv   # ambiente virtual Python
```

Essa separação é importante para:

* manter o repositório limpo
* permitir reinstalações independentes
* facilitar depuração e reprodutibilidade

---

# 🚀 1. Instalação automática (recomendada)

```bash
chmod +x setup_all.sh
./setup_all.sh all
```

---

## 🔧 O que o script faz

O script `setup_all.sh` executa automaticamente:

* instalação de dependências do sistema
* criação (ou recriação) da `venv`
* instalação de dependências Python
* compilação do stack P4 (PI, bmv2, p4c)
* compilação do stack NETCONF (libyang, sysrepo, libnetconf2, Netopeer2)
* configuração do usuário `netconf`
* configuração de autenticação SSH
* instalação do módulo YANG `l2i-qos`
* aplicação de política NACM permissiva
* validação básica do ambiente

---

## ⏱️ Tempo esperado

| Etapa                | Tempo      |
| -------------------- | ---------- |
| Dependências sistema | ~2–5 min   |
| Build P4             | ~20–60 min |
| Build NETCONF        | ~10–30 min |
| Total                | ~30–90 min |

Pode variar conforme CPU/RAM.

---

# 🛠️ 2. Instalação manual (detalhada)

A instalação manual é útil para:

* inspeção científica do ambiente
* depuração
* customização do sistema

---

## 2.1 Clonar repositório

```bash
git clone <URL_DO_REPOSITORIO>
cd l2i-dsl
```

---

## 2.2 Dependências

### 2.2.1 Básicas

```bash
sudo apt update
sudo apt install -y \
  python3 python3-venv python3-pip \
  git curl build-essential cmake pkg-config \
  iproute2 iputils-ping net-tools iperf3 fping graphviz \
  protobuf-compiler protobuf-compiler-grpc \
  thrift-compiler libthrift-dev libnanomsg-dev \
  libgrpc++-dev libgrpc-dev
```

### 2.2.2 Dependências NETCONF

```bash
sudo apt install -y \
  libssh-dev libssl-dev \
  libcurl4-openssl-dev \
  libpcre2-dev \
  libprotobuf-c-dev protobuf-c-compiler \
  libsystemd-dev \
  libavl-dev libev-dev \
  libsqlite3-dev
```

### 2.2.3 Dependências P4
```bash
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

## 2.3 Ambiente Python

```bash
python3 -m venv ~/l2i-dev/venv
source ~/l2i-dev/venv/bin/activate

pip install --upgrade pip setuptools wheel
pip install jsonschema pyyaml grpcio protobuf==3.20.3 \
            ncclient cryptography paramiko \
            grpcio-tools p4runtime-shell
```

---

## 2.4 Stack P4 (em ~/l2i-src)

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

## 2.5 Stack NETCONF (em ~/l2i-src)

```bash
cd ~/l2i-src
```

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

## 2.6 Configuração NETCONF

### Criar usuário

```bash
sudo useradd --system --shell /usr/sbin/nologin \
             --home-dir /var/lib/netconf \
             --create-home netconf
```

---

### Gerar chave SSH

```bash
ssh-keygen -t rsa -b 2048 -f ~/.ssh/l2i_netconf_key -N ""
```

---

### Instalar chave

```bash
sudo mkdir -p /var/lib/netconf/.ssh
sudo cp ~/.ssh/l2i_netconf_key.pub /var/lib/netconf/.ssh/authorized_keys
sudo chown -R netconf:netconf /var/lib/netconf
```

---

## 2.7 Instalar YANG e NACM

```bash
sudo sysrepoctl -i yang/l2i-qos.yang -s yang/
```

```bash
sudo sysrepocfg --import=~/l2i-dsl/l2i-nacm-netconf-permit.xml -f xml -d running -m ietf-netconf-acm
sudo sysrepocfg --import=~/l2i-dsl/l2i-nacm-netconf-permit.xml -f xml -d startup -m ietf-netconf-acm
```

A política NACM permissiva está incluída no repositório e pode ser aplicada com `sysrepocfg`.

---

# 🔁 3. Após instalação

Nenhuma compilação adicional é necessária após a instalação inicial.

Para uso contínuo:

```bash
source ~/l2i-dev/venv/bin/activate
./setup_all.sh start_real_services
```

---

# 🧪 4. Preparação para execução

Antes de rodar os cenários:

* ambiente Python deve estar ativo
* serviços devem estar em execução

Execução típica:

```bash
./setup_all.sh run_s1_real
```

---

# ⚠️ Observações importantes

* O build do P4 pode ser demorado
* máquinas com pouca RAM devem usar `MAKE_JOBS=2`
* swap pode impactar significativamente o tempo de build
* a VM fornecida evita esse custo

---

# 📌 Conclusão

Este processo garante:

* instalação completa do ambiente
* isolamento entre componentes
* reprodutibilidade consistente
* execução independente dos cenários

---

👉 Próximo passo: [docs/minimal_test.md](minimal_test.md)
