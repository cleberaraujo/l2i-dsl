# 🌐 L2 Intent (L2i) DSL — Artefato Experimental

🏠 **Home** · 📐 [Arquitetura](docs/architecture.md) · ⚙️ [Instalação](docs/installation.md) · 🧪 [Teste mínimo](docs/minimal_test.md) · 📊 [Experimentos](docs/experiments.md) · 🧠 [Reivindicações](docs/claims.md) · 👩‍💻 [Notas técnicas](docs/developers.md) · 📂 [Estrutura](docs/repository_structure.md) · 🔁 [Pós-reboot](docs/runtime_after_reboot.md) · 🖥️ [VM](docs/vm.md)

---

# 📌 Título do projeto

**Uma Abordagem Declarativa e Modular para Adaptação Dinâmica da Camada de Enlace de Redes Heterogêneas**

---

# 📝 Resumo

Este repositório contém o artefato experimental associado ao artigo acima, publicado na Trilha Principal do SBRC 2026, cujo objetivo é investigar como intenções de comunicação podem ser expressas de forma declarativa em L2 e materializadas dinamicamente em ambientes heterogêneos.

A proposta introduz a linguagem **L2i (Layer-2 Intent)**, que permite declarar requisitos como largura de banda, latência, prioridade e multicast, desacoplando especificação semântica de mecanismos de implementação.

A avaliação é realizada em um ambiente baseado em:

* namespaces de rede Linux
* controle de tráfego (`tc`)
* NETCONF/YANG (`sysrepo`, `Netopeer2`)
* P4 (`bmv2`, `P4Runtime`)

---

## 🎯 Objetivo do repositório

Este repositório contém o **artefato experimental completo** associado ao artigo, com foco em:

* reprodução de cenários experimentais
* validação do pipeline declarativo L2i
* execução em infraestrutura heterogênea real
* análise de comportamento sob diferentes modos de operação

O artefato é projetado para ser:

* executável localmente
* reproduzível
* modular
* independente de ferramentas externas complexas

---

## 🏅 Selos considerados

Este artefato foi estruturado para solicitar os seguintes selos do CTA:

- 🟢 **SeloD** — Artefato Disponível
- 🟢 **SeloF** — Artefato Funcional
- 🟢 **SeloS** — Artefato Sustentável
- 🎯 **SeloR** — Experimentos Reprodutíveis (**objetivo principal**)

---

# 📂 Estrutura deste README

Este documento está organizado conforme boas práticas de avaliação de artefatos:

1. Informações básicas
2. Dependências
3. Preocupações com segurança
4. Instalação
5. Teste mínimo
6. Experimentos
7. Licença

Detalhes adicionais estão distribuídos nos arquivos em `docs/`.

---

# ℹ️ Informações básicas

O artefato implementa:

* uma DSL declarativa para L2 (L2i)
* um pipeline de adaptação multidomínio
* execução sobre três domínios heterogêneos:

  * Linux (`tc`)
  * NETCONF/YANG
  * P4

---

# 📦 Dependências

## Sistema

* Ubuntu 24.04 LTS
* Python 3.12
* `cmake`, `build-essential`, `pkg-config`
* `iproute2`, `iperf3`, `fping`, `graphviz`

## Stack P4

* PI
* behavioral-model
* p4c

## Stack NETCONF

* libyang
* sysrepo
* libnetconf2
* Netopeer2

## Python

* jsonschema
* grpcio
* protobuf==3.20.3
* ncclient

📖 Detalhes em: [docs/installation.md](docs/installation.md)

---

# 🔐 Preocupações com segurança

Este artefato modifica o ambiente do sistema hospedeiro, incluindo:

* Criação de usuário dedicado (`netconf`)
* Configuração de autenticação SSH por chave pública
* Aplicação de política NACM permissiva
* Criação e manipulação de namespaces de rede e interfaces virtuais (veth)

## ⚠️ Riscos potenciais

* Exposição de serviços via SSH
* Controle de acesso excessivamente permissivo
* Interferência na configuração de rede do hospedeiro
* Persistência de configurações após a execução

## 🛡️ Recomendações

* Executar apenas em ambiente isolado (máquina virtual ou host dedicado)
* Evitar uso em sistemas de produção ou com dados sensíveis
* Revisar configurações de autenticação e controle de acesso
* Remover ou limpar namespaces e interfaces criados após o uso (`./scripts/s1_topology_cleanup.sh` `./scripts/s2_topology_cleanup.sh` `./scripts/cleanup_net.sh`), ou ainda reiniciar o host

> 🚨 **Recomendação:** ratificamos que a execução em ambiente isolado (VM ou host dedicado) é fortemente indicada para mitigar riscos e garantir a reprodutibilidade dos experimentos.


---

# 🚀 Instalação

## Automática

```bash
chmod +x setup_all.sh
./setup_all.sh all
```

## Manual

📖 Detalhes em: [docs/installation.md](docs/installation.md)


---

## Organização do ambiente

```bash
~/l2i-dsl
~/l2i-src
~/l2i-dev/venv
```

---

# 🧪 Teste mínimo

```bash
source ~/l2i-dev/venv/bin/activate
./setup_all.sh start_real_services
./setup_all.sh run_s1_real
```

Validação:

```bash
grep -R '"backend_apply"' results/ -n
```

Resultado esperado:

```json
"backend_apply": {
  "A": true,
  "B": true,
  "C": true
}
```

---

# 📊 Experimentos

## 🔀 Execução automatizada

```bash
./setup_all.sh run_s1_real
./setup_all.sh run_s2_real
```

---

## 🧪 Execução manual (controle completo)

### Ordem obrigatória

```text
NETCONF → P4 → pipeline → topologia → experimento
```

---

### 1. Subir NETCONF

```bash
sudo /usr/local/sbin/netopeer2-server -d
```

---

### 2. Ambiente

```bash
source ~/l2i-dev/venv/bin/activate
cd ~/l2i-dsl
```

---

### 3. Subir P4

```bash
./scripts/p4_build_and_run.sh
```

---

### 4. Carregar pipeline

```bash
python scripts/p4_push_pipeline.py --addr 127.0.0.1:9559
```

---

### 5. Criar topologia

```bash
sudo ./scripts/s1_topology_setup.sh
```

---

### 6. Executar S1

```bash
sudo ~/l2i-dev/venv/bin/python -m scenarios.multidomain_s1 \
  --spec specs/valid/s1_unicast_qos.json \
  --duration 30 \
  --bwA 100 --bwB 50 --bwC 100 \
  --delay-ms 1 \
  --be-mbps 60 \
  --mode adapt \
  --backend real
```

---

### 7. Executar S2

```bash
sudo ~/l2i-dev/venv/bin/python -m scenarios.multicast_s2_recovery_stable5 \
  --spec specs/valid/s2_multicast_source_oriented.json \
  --duration 30 \
  --be-mbps 80 \
  --bwA 40 --bwB 100 --bwC 100 \
  --delay-ms 1 \
  --mode adapt \
  --backend real \
  --phase-splits 10 15 \
  --event-name join \
  --rtt-interval-ms 50 \
  --recovery-bin-ms 500 \
  --stable-k-bins 3
```

---

## 📊 Resultados

```bash
results/S1/
results/S2/
```

Incluem:

* JSON
* CSV
* dumps de configuração
* logs

---

# 🧠 Reivindicações principais

* inicialização multidomínio
* execução fim a fim
* materialização declarativa
* suporte a multicast dinâmico

📖 Detalhes em: [docs/claims.md](docs/claims.md)

---

# 🔁 Execução após reboot

```bash
source ~/l2i-dev/venv/bin/activate
./setup_all.sh start_real_services
```

---

# 🖥️ Máquina virtual

Uma VM pré-configurada pode ser utilizada para evitar o processo de instalação completo.

📖 Detalhes em: [docs/vm.md](docs/vm.md)

---

# 👥 Autores

* Antônio Cleber de Sousa Araújo (antoniocleber@ifba.edu.br)
* Leobino N. Sampaio (leobino@ufba.br)

---

## 🙏 Agradecimentos

Os autores agradecem o apoio do Conselho Nacional de Desenvolvimento Científico e Tecnológico (CNPq) e da Fundação de Amparo à Pesquisa do Estado da Bahia (FAPESB). Este material é baseado em trabalho apoiado pelo Escritório de Pesquisa Básica da Força Aérea (*Air Force Office of Scientific Research*) dos EUA sob o número de concessão **FA9550-23-1-0631**.

---

# 📜 Licença

Apache License 2.0

---
