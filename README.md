# 🌐 L2 Intent (L2i) DSL — Artefato Experimental

🏠 **Home** · ⚙️ [Instalação](docs/installation.md) · 🧪 [Teste mínimo](docs/minimal_test.md) · 📊 [Experimentos](docs/experiments.md) · 🧠 [Reivindicações](docs/claims.md) · 👩‍💻 [Notas técnicas](docs/developers.md) · 📂 [Estrutura](docs/repository_structure.md) · 🔁 [Pós-reboot](docs/runtime_after_reboot.md) · 🖥️ [VM](docs/vm.md)

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

# 🎯 Objetivo do artefato

Este repositório contém o **artefato experimental completo** associado ao artigo, com foco em:

* reprodução de cenários experimentais
* validação do pipeline declarativo L2i
* execução em infraestrutura heterogênea real
* análise de comportamento sob diferentes modos de operação

Este artefato foi projetado para:

* reproduzir cenários experimentais do artigo
* validar o pipeline declarativo L2i
* demonstrar materialização multidomínio (Linux, NETCONF, P4)
* permitir análise controlada de comportamento

---

# 🏅 Selos considerados

Este artefato foi estruturado para solicitar:

* 🟢 **SeloD** — Disponível
* 🟢 **SeloF** — Funcional
* 🟢 **SeloS** — Sustentável
* 🎯 **SeloR** — Reprodutível (**objetivo principal**)

---

# 📂 Estrutura do README

Este documento segue os requisitos mínimos do CTA :

1. Informações básicas
2. Dependências
3. Segurança
4. Instalação
5. Teste mínimo
6. Experimentos

Detalhes completos estão em `docs/`.

---

# ℹ️ Informações básicas

O artefato implementa:

* DSL declarativa para L2 (L2i)
* pipeline de adaptação multidomínio
* execução sobre três domínios heterogêneos: Linux (`tc`), NETCONF/YANG e P4

---

# 📦 Dependências

## Sistema

* Ubuntu Server 24.04 LTS
* Python 3.12

## Componentes principais

* P4: PI, bmv2, p4c
* NETCONF: libyang, sysrepo, Netopeer2
* Python: jsonschema, grpcio, protobuf, ncclient

📖 Detalhes completos: [docs/installation.md](docs/installation.md)

---

# 🔐 Preocupações com segurança

Este artefato modifica o ambiente do sistema hospedeiro, incluindo:

* criação de usuário dedicado (`netconf`)
* autenticação SSH por chave pública
* política NACM permissiva
*  Criação e manipulação de namespaces de rede e interfaces virtuais (veth)

## ⚠️ Riscos potenciais

* exposição de serviços SSH
* controle de acesso excessivamente permissivo no NETCONF
* interferência na configuração de rede do hospedeiro
* persistência de configurações após a execução

## 🛠️ Boas práticas

* utilizar ambiente isolado (VM recomendada ou host dedicado)
* evitar execução em sistemas com dados sensíveis
* remover artefatos de rede após uso

> 🚨 Recomenda-se execução em máquina virtual. Veja: [docs/vm.md](docs/vm.md)

---

# 🚀 Instalação

## Execução automática (recomendada)

```bash
chmod +x setup_all.sh
./setup_all.sh all
```

O script:

* instala dependências
* compila stacks P4 e NETCONF
* configura autenticação
* valida o ambiente

📖 Detalhes: [docs/installation.md](docs/installation.md)

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

📖 Detalhes: [docs/minimal_test.md](docs/minimal_test.md)

---

# 📊 Experimentos

Os experimentos completos incluem:

* S1 — Unicast QoS
* S2 — Multicast orientado à origem

Execução automatizada:

```bash
./setup_all.sh run_s1_real
./setup_all.sh run_s2_real
```

📖 Execução detalhada e controle fino:
👉 [docs/experiments.md](docs/experiments.md)

---

# 🧠 Reivindicações principais

O artefato demonstra:

* inicialização multidomínio
* execução fim a fim
* materialização declarativa
* adaptação dinâmica
* reprodutibilidade

📖 Evidências detalhadas:
👉 [docs/claims.md](docs/claims.md)

---

# 🔁 Execução após reboot

```bash
source ~/l2i-dev/venv/bin/activate
./setup_all.sh start_real_services
```

📖 Detalhes: [docs/runtime_after_reboot.md](docs/runtime_after_reboot.md)

---

# 🖥️ Máquina virtual

Uma VM pré-configurada está disponível para evitar o tempo gasto pelo processo de compilação e instalação completo.

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
