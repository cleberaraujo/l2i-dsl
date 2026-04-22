# 📂 Estrutura do Repositório

🏠 [README](../README.md) · ⚙️ [Instalação](installation.md) · 🧪 [Teste mínimo](minimal_test.md) · 📊 [Experimentos](experiments.md)

---

## 🎯 Objetivo

Descrever a organização do repositório com foco em:

* separação de responsabilidades
* rastreabilidade entre especificação, execução e resultados
* suporte à reprodutibilidade
* facilidade de navegação e extensão

---

# 📁 Visão geral

```text
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

```text
[ Especificação ] → [ Processamento ] → [ Execução ] → [ Resultados ]
```

---

## 🔗 Mapeamento

| Camada        | Diretórios                                  |
| ------------- | ------------------------------------------- |
| Especificação | `specs/`, `schemas/`                        |
| Processamento | `l2i/`, `profiles/`                         |
| Execução      | `scenarios/`, `scripts/`, `p4src/`, `yang/` |
| Resultados    | `results/`, `figures/`, `logs/`             |

---

# 📦 Componentes principais

## 📐 `l2i/`

Núcleo lógico do sistema:

* DSL declarativa
* validação semântica
* tradução para backends
* composição de intenções

---

## 🧪 `scenarios/`

Define cenários experimentais:

* criação de topologia
* geração de tráfego
* orquestração de eventos
* coleta de métricas

---

## ⚙️ `scripts/`

Scripts auxiliares:

* inicialização do ambiente
* controle de serviços
* automação de execução

---

## 📄 `specs/`

Entradas declarativas:

```text
specs/valid/
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
* dumps (comandos reais utilizados pelas tecnologias)

---

## 📈 `figures/`

Arquivos auxiliares para geração de figuras.

Pode conter:
* scripts gnuplot
* dados agregados
* versões finais de gráficos

---

## 🧾 `logs/`

Registros de execução:
* P4 (bmv2)
* NETCONF
* cenários

---

# 🔄 Fluxo de dados

```text
specs/ → l2i/ → scenarios/ → results/
           ↓
        backends
 (tc / NETCONF / P4)
```

---

# 🧩 Extensibilidade

O sistema pode ser estendido em:

* novos cenários → `scenarios/`
* novas intenções → `specs/`
* novos modelos → `schemas/`
* novos backends → `l2i/`
* novos experimentos → `scripts/`

---

# 📌 Observações

* caminhos são relativos ao repositório
* estrutura favorece isolamento e reprodutibilidade
* diretório `results/` pode crescer rapidamente

---

👉 Próximo: [docs/developers.md](developers.md)
