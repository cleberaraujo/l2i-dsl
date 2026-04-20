# 📂 Estrutura do Repositório

🏠 [README](../README.md) · ⚙️ [Instalação](installation.md) · 🧪 [Teste mínimo](minimal_test.md) · 📊 [Experimentos](experiments.md)

---

## 🎯 Objetivo

Este documento descreve a organização do repositório, com foco em:

* separação de responsabilidades
* rastreabilidade entre especificação, execução e resultados
* suporte à reprodutibilidade experimental
* facilidade de navegação e extensão

---

# 📁 Visão geral

```text id="7ehhtp"
.
├── README.md
├── LICENSE
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
└── logs/
```

---

# 🧠 Organização conceitual

A estrutura do repositório segue três camadas principais:

```text id="1dq9vr"
[ Especificação ] → [ Processamento ] → [ Execução ] → [ Resultados ]
```

Mapeamento:

| Camada        | Diretórios                                  |
| ------------- | ------------------------------------------- |
| Especificação | `specs/`, `schemas/`                        |
| Processamento | `l2i/`, `profiles/`                         |
| Execução      | `scenarios/`, `scripts/`, `p4src/`, `yang/` |
| Resultados    | `results/`, `figures/`, `logs/`             |

---

# 📦 Componentes principais

## 📐 `l2i/`

Implementa a DSL e o pipeline declarativo.

Inclui:

* modelos de dados
* validação semântica
* tradução para backends
* composição de intenções

Este diretório representa o núcleo lógico do sistema.

---

## 🧪 `scenarios/`

Define os cenários experimentais executáveis.

Principais:

* `multidomain_s1.py`
* `multicast_s2_recovery_stable5.py`

Responsabilidades:

* criação de topologia
* geração de tráfego
* orquestração de eventos
* coleta de métricas

---

## ⚙️ `scripts/`

Contém scripts auxiliares de suporte.

Exemplos:

* inicialização do ambiente P4
* configuração de serviços
* utilitários de execução

---

## 📄 `specs/`

Especificações declarativas utilizadas como entrada da DSL.

```text id="9o5m6o"
specs/
└── valid/
    ├── s1_unicast_qos.json
    └── s2_multicast_source_oriented.json
```

---

## 📐 `schemas/`

Define os esquemas de validação (JSON Schema).

Responsável por:

* validação estrutural
* consistência das especificações

---

## 🎛️ `profiles/`

Define perfis de execução e parâmetros auxiliares.

Pode incluir:

* políticas de tradução
* parâmetros específicos de backend
* configurações experimentais

---

## 🧾 `yang/`

Modelos YANG utilizados no domínio NETCONF.

Principal:

* `l2i-qos.yang`

---

## 🔌 `p4src/`

Código-fonte P4 utilizado no domínio C.

Inclui:

* definição do pipeline
* tabelas e ações
* lógica de encaminhamento

---

## 📊 `results/`

Contém os artefatos gerados pelos experimentos.

Estrutura típica:

```text id="h7hkg7"
results/
├── S1/
│   ├── S1_<timestamp>.json
│   ├── S1_<timestamp>_domain_A.json
│   ├── S1_<timestamp>_domain_B.json
│   └── S1_<timestamp>_domain_C.json
└── S2/
    ├── S2_<timestamp>.json
    └── ...
```

Inclui:

* sumários
* dados por domínio
* métricas auxiliares

---

## 📈 `figures/`

Arquivos auxiliares para geração de figuras.

Pode conter:

* scripts gnuplot
* dados agregados
* versões finais de gráficos

---

## 🧾 `logs/`

Registros de execução.

Exemplos:

* logs do bmv2
* logs do NETCONF
* logs dos cenários

---

# 🔄 Fluxo de dados

```text id="z9qux7"
specs/ → l2i/ → scenarios/ → results/
           ↓
        backends
 (tc / NETCONF / P4)
```

---

# 🔁 Relação com execução

Fluxo típico:

```text id="7clx8p"
specificação → validação → tradução → aplicação → coleta → análise
```

Cada etapa corresponde diretamente a um subconjunto da estrutura do repositório.

---

# 🧩 Extensibilidade

O repositório pode ser estendido em diferentes pontos:

* novos cenários → `scenarios/`
* novas intenções → `specs/`
* novos modelos → `schemas/`
* novos backends → `l2i/`
* novos experimentos → `scripts/`

---

# 📌 Observações finais

* todos os caminhos são relativos ao repositório
* a estrutura favorece reprodutibilidade e isolamento
* diretórios seguem separação clara de responsabilidades
* a pasta `results/` pode crescer rapidamente (uso de `.gitignore` recomendado)

---

👉 Próximo: [docs/developers.md](developers.md)
