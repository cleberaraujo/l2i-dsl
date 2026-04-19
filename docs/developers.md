# 👩‍💻 Notas Técnicas e de Desenvolvimento

🏠 [README](../README.md) · ⚙️ [Instalação](installation.md) · 🧪 [Teste mínimo](minimal_test.md) · 📊 [Experimentos](experiments.md) · 📂 [Estrutura](repository_structure.md)

---

## 🎯 Objetivo

Este documento reúne decisões técnicas, suposições de projeto e aspectos práticos relevantes para:

* manutenção do artefato
* extensão de cenários
* análise de comportamento em ambiente real
* depuração de falhas

---

# 🧠 Decisões arquiteturais

## Separação semântica e operacional

A arquitetura distingue explicitamente três níveis:

* **CED** — especificação declarativa
* **MAD** — tradução e adaptação
* **AC** — aplicação nos backends

Essa separação permite:

* independência tecnológica
* validação parcial (modo `mock`)
* extensibilidade para novos domínios

---

## Abordagem multidomínio

O sistema assume três domínios com características distintas:

| Domínio | Tecnologia   | Natureza                   |
| ------- | ------------ | -------------------------- |
| A       | Linux `tc`   | imperativo local           |
| B       | NETCONF/YANG | transacional               |
| C       | P4           | plano de dados programável |

O objetivo não é uniformizar os mecanismos, mas preservar a **intenção semântica** entre eles.

---

# ⚙️ Decisões de implementação

## Uso de ambiente virtual isolado

```bash id="gxj14m"
~/l2i-dev/venv
```

Motivação:

* controle de dependências
* isolamento do sistema
* reprodutibilidade

---

## Organização de código-fonte externo

```bash id="m5rdcc"
~/l2i-src
```

Motivação:

* separar artefato de dependências pesadas
* permitir rebuild independente
* facilitar depuração de build

---

## Dependência de protobuf 3.20.3

Motivação:

* compatibilidade com P4Runtime
* evitar incompatibilidades com versões recentes

---

## Paralelismo controlado (`MAKE_JOBS=2`)

Motivação:

* evitar uso excessivo de memória
* garantir execução em ambientes limitados

---

## Usuário dedicado NETCONF

```text id="kh4r6n"
netconf
```

Motivação:

* isolamento de permissões
* autenticação por chave pública
* separação de contexto de execução

---

## Política NACM permissiva

Motivação:

* simplificar ambiente experimental
* evitar falhas de autorização

Observação: não apropriado para produção.

---

## Uso de namespaces e veth

Motivação:

* isolamento de topologia
* reprodutibilidade local
* independência de ferramentas externas

---

# 🧪 Observabilidade

Os experimentos produzem artefatos em:

```bash id="iq2g9w"
results/
```

Incluem:

* sumários JSON
* dados por domínio
* métricas (RTT, throughput)
* dumps auxiliares

---

# ⚠️ Problemas comuns

## NETCONF — usuário não reconhecido

Causa:

* configuração incompleta

Solução:

```bash id="w9j2yq"
./setup_all.sh configure_netconf
```

---

## NETCONF — falha de autorização

Causa:

* NACM não aplicado

Solução:

* reaplicar política NACM

---

## P4 — porta 9559 não ativa

Causa:

* `simple_switch_grpc` não iniciou

Solução:

```bash id="ktv8cf"
./setup_all.sh start_real_services
```

---

## Build P4 lento ou falhando

Causa:

* pouca memória
* paralelismo alto

Solução:

* usar `MAKE_JOBS=2`
* preferir VM pré-configurada

---

## Ambiente Python inconsistente

Causa:

* conflito de dependências

Solução:

```bash id="l4z3sm"
rm -rf ~/l2i-dev/venv
./setup_all.sh python_env
```

---

# 🔁 Execução após reinicialização

```bash id="6kl0jv"
source ~/l2i-dev/venv/bin/activate
./setup_all.sh start_real_services
```

Nenhum rebuild é necessário.

---

# 🧩 Extensão do artefato

Possíveis pontos de extensão:

* novos cenários → `scenarios/`
* novas intenções → `specs/`
* novos modelos → `schemas/`
* novos backends → `l2i/`
* novos perfis → `profiles/`

---

# 📌 Considerações finais

* o foco do repositório é reprodutibilidade experimental
* o design prioriza clareza sobre automação implícita
* o modo `real` é o principal para validação empírica
* o modo `mock` facilita depuração e validação estrutural

---
