# 📊 Experimentos

🏠 [README](../README.md) · ⚙️ [Instalação](installation.md) · 🧪 [Teste mínimo](minimal_test.md) · 🧠 [Reivindicações](claims.md)

---

## 🎯 Objetivo

Este documento descreve como executar e interpretar os experimentos associados ao artefato, com foco em:

* validação do pipeline declarativo L2i
* comportamento em ambiente heterogêneo real
* comparação entre modos de execução
* observação de propriedades emergentes

---

# 🧠 Modelo experimental

Os experimentos são estruturados em duas dimensões independentes:

## 🔀 Modo de operação

### `baseline`

* execução sem adaptação declarativa
* representa comportamento tradicional

### `adapt`

* ativa o pipeline L2i
* realiza tradução e materialização dinâmica

---

## ⚙️ Tipo de backend

### `mock`

* valida apenas o pipeline lógico
* não aplica configurações reais

### `real`

* aplica configurações efetivas em:

  * Linux (`tc`)
  * NETCONF/YANG
  * P4

---

## 🧩 Combinação dos eixos

| Modo     | Backend | Interpretação     |
| -------- | ------- | ----------------- |
| baseline | mock    | referência lógica |
| baseline | real    | rede tradicional  |
| adapt    | mock    | validação da DSL  |
| adapt    | real    | execução completa |

---

# 📂 Especificações declarativas

As intenções são descritas em:

```bash id="6y8u2j"
specs/valid/
```

Principais arquivos:

* `s1_unicast_qos.json`
* `s2_multicast_source_oriented.json`

---

## 🔍 Exemplo

```json id="tq3o4v"
{
  "flow_id": "flow1",
  "bandwidth": 10,
  "priority": "high"
}
```

Essas especificações são:

* validadas via JSON Schema
* interpretadas pela DSL L2i
* traduzidas para múltiplos domínios

---

# 🧪 Cenário S1 — Unicast multidomínio com QoS

## 🎯 Objetivo

Avaliar:

* controle de largura de banda
* isolamento entre fluxos
* previsibilidade sob contenção

---

## ▶️ Execução padrão

```bash id="x5z6hf"
./setup_all.sh run_s1_real
```

---

## ▶️ Execução manual (controle fino)

```bash id="8xgkdn"
sudo ~/l2i-dev/venv/bin/python -m scenarios.multidomain_s1 \
  --spec specs/valid/s1_unicast_qos.json \
  --duration 10 \
  --be-mbps 30 \
  --mode adapt \
  --backend real
```

---

## 📊 Artefatos gerados

```bash id="jql6l1"
results/
```

Arquivos típicos:

* `S1_<timestamp>.json`
* `S1_<timestamp>_domain_A.json`
* `S1_<timestamp>_domain_B.json`
* `S1_<timestamp>_domain_C.json`

---

## 🔍 Métricas observáveis

* throughput por fluxo
* RTT (amostras)
* consistência entre execuções
* aplicação de backend (`backend_apply`)

---

# 🌳 Cenário S2 — Multicast orientado à origem

## 🎯 Objetivo

Avaliar:

* adaptação a eventos dinâmicos (`join`)
* comportamento multicast em L2
* recuperação e estabilidade temporal

---

## ▶️ Execução padrão

```bash id="4k8q3y"
./setup_all.sh run_s2_real
```

---

## ▶️ Execução manual

```bash id="d1y6vt"
sudo ~/l2i-dev/venv/bin/python -m scenarios.multicast_s2_recovery_stable5 \
  --spec specs/valid/s2_multicast_source_oriented.json \
  --duration 10 \
  --be-mbps 80 \
  --bwA 40 \
  --bwB 100 \
  --bwC 100 \
  --delay-ms 1 \
  --mode adapt \
  --backend real \
  --phase-splits 3 6 \
  --event-name join \
  --rtt-interval-ms 50 \
  --recovery-bin-ms 500 \
  --stable-k-bins 3
```

---

## 📊 Métricas observáveis

* throughput por domínio
* RTT ao longo do tempo
* resposta ao evento `join`
* estabilidade após recuperação

---

# 🔬 Comparações experimentais

## Mock vs Real

```bash id="jv8m02"
./setup_all.sh run_s1_mock
./setup_all.sh run_s1_real
```

Permite separar:

* validade lógica do modelo
* materialização tecnológica

---

## Baseline vs Adapt

Execução manual:

```bash id="9y4uaz"
--mode baseline
--mode adapt
```

Permite observar:

* impacto da abordagem declarativa
* diferenças de comportamento sob contenção

---

# 🧠 Interpretação geral

Os experimentos permitem observar que:

* a intenção declarativa é independente do backend
* a materialização ocorre em múltiplos domínios
* há separação clara entre semântica e mecanismo
* o comportamento pode ser controlado via especificação

---

# 🔁 Fluxo recomendado

```text
1. ./setup_all.sh start_real_services
2. ./setup_all.sh run_s1_real
3. ./setup_all.sh run_s2_real
4. analisar results/
```

---

# 📎 Relação com o artigo

Os experimentos aqui descritos correspondem diretamente às avaliações apresentadas no artigo.

🔗 https://github.com/cleberaraujo/link_layer_intent.git

---

# 📌 Observações finais

* resultados podem variar levemente conforme hardware
* o modo `real` é o principal para análise
* o modo `mock` é útil para depuração e validação estrutural

---

👉 Próximo: [docs/claims.md](claims.md)
