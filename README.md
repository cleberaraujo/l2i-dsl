# 🌐 L2 Intent (L2i) DSL — Artefato Experimental

🏠 **Home** · 📐 [Arquitetura](docs/architecture.md) · ⚙️ [Instalação](docs/installation.md) · 🧪 [Teste mínimo](docs/minimal_test.md) · 📊 [Experimentos](docs/experiments.md) · 🧠 [Reivindicações](docs/claims.md) · 👩‍💻 [Notas técnicas](docs/developers.md) · 📂 [Estrutura](docs/repository_structure.md) · 🔁 [Pós-reboot](docs/runtime_after_reboot.md) · 🖥️ [VM](docs/vm.md)

---

## 📌 Título do artigo

**Uma Abordagem Declarativa e Modular para Adaptação Dinâmica da Camada de Enlace de Redes Heterogêneas**

---

## 📝 Resumo

A camada de enlace das redes contemporâneas permanece fortemente acoplada a mecanismos específicos e a modelos de configuração pouco expressivos, limitando sua adaptação dinâmica e evolução incremental. Este trabalho propõe uma abordagem declarativa e modular para adaptação dinâmica em L2, baseada na separação entre especificação declarativa e materialização tecnológica.

A linguagem L2i permite que aplicações e protocolos expressem requisitos de comunicação de forma abstrata e independente de tecnologia. A proposta foi avaliada em um ambiente experimental construído com namespaces de rede Linux, enlaces virtuais e componentes reais de controle e plano de dados, envolvendo domínios Linux (`tc`), NETCONF/YANG e P4.

Os resultados indicam maior previsibilidade, isolamento entre fluxos e estabilidade sob contenção, com overhead operacional limitado.

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

## 🧠 Ideia central

> Intenções de comunicação são declaradas de forma abstrata e materializadas dinamicamente na camada de enlace.

A arquitetura é estruturada em três blocos:

* **CED** — Camada de Especificações Declarativas
* **MAD** — Mecanismo de Adaptação Dinâmica
* **AC** — Aplicador de Configurações

📖 Detalhes: [docs/architecture.md](docs/architecture.md)

---

## 🧩 Domínios experimentais

O sistema opera sobre três domínios distintos:

* **A** — Linux (`tc` / HTB)
* **B** — NETCONF/YANG (`sysrepo`, `Netopeer2`)
* **C** — P4 (`bmv2`, `P4Runtime`)

---

## 🧪 Cenários experimentais

### 🔀 S1 — Unicast multidomínio com QoS

Avalia controle de banda, prioridade e isolamento sob contenção.

### 🌳 S2 — Multicast orientado à origem

Avalia adaptação dinâmica a eventos e comportamento multicast em L2.

📖 Guia completo: [docs/experiments.md](docs/experiments.md)

---

## 🚀 Execução rápida

```bash
chmod +x setup_all.sh
./setup_all.sh all
./setup_all.sh start_real_services
./setup_all.sh run_s1_real
```

---

## 📂 Estrutura do repositório

```text
.
├── README.md
├── setup_all.sh
├── docs/
├── l2i/
├── scenarios/
├── scripts/
├── specs/
├── schemas/
├── profiles/
├── p4src/
├── yang/
├── results/
├── figures/
```

📖 Detalhado: [docs/repository_structure.md](docs/repository_structure.md)

---

## ⚙️ Organização do ambiente local

O artefato assume a seguinte organização fora do repositório:

```bash
~/l2i-dsl        # repositório
~/l2i-src        # código-fonte de dependências (P4, NETCONF)
~/l2i-dev/venv   # ambiente Python
```

Esses caminhos são utilizados automaticamente pelo `setup_all.sh`.

---

## 📦 Instalação

### Automática

```bash
./setup_all.sh all
```

### Manual

Processo detalhado em:

👉 [docs/installation.md](docs/installation.md)

---

## 🧪 Teste mínimo

```bash
./setup_all.sh start_real_services
./setup_all.sh run_s1_real
```

Validação:

```bash
grep -R '"backend_apply"' results/ -n
```

📖 Detalhes: [docs/minimal_test.md](docs/minimal_test.md)

---

## 📊 Experimentos

Execução dos cenários:

```bash
./setup_all.sh run_s1_real
./setup_all.sh run_s2_real
```

📖 Guia completo: [docs/experiments.md](docs/experiments.md)

---

## 🧠 Reivindicações

As propriedades experimentais observáveis estão descritas em:

👉 [docs/claims.md](docs/claims.md)

---

## 🔁 Execução após reinicialização

```bash
source ~/l2i-dev/venv/bin/activate
./setup_all.sh start_real_services
```

📖 Guia: [docs/runtime_after_reboot.md](docs/runtime_after_reboot.md)

---

## 🖥️ Máquina virtual

Uma VM pré-configurada pode ser utilizada para evitar o processo de instalação completo.

📖 Detalhes: [docs/vm.md](docs/vm.md)

---

## 🔐 Segurança

Este artefato:

* cria usuário `netconf`
* configura autenticação SSH por chave
* aplica política NACM permissiva
* manipula namespaces de rede

Recomenda-se execução em ambiente isolado (VM ou máquina dedicada).

---

## 👥 Autores

* Antônio Cleber de Sousa Araújo
* Leobino N. Sampaio

---

## 🙏 Agradecimentos

Os autores agradecem o apoio do Conselho Nacional de Desenvolvimento Científico e Tecnológico (CNPq) e da Fundação de Amparo à Pesquisa do Estado da Bahia (FAPESB). Este material é baseado em trabalho apoiado pelo Escritório de Pesquisa Básica da Força Aérea (*Air Force Office of Scientific Research*) dos EUA sob o número de concessão **FA9550-23-1-0631**.

---

## 📜 Licença

Apache License 2.0

---

## 🔗 Repositório do artigo

https://github.com/cleberaraujo/link_layer_intent.git

---
