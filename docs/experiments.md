# 📊 Experimentos

🏠 [README](../README.md) · ⚙️ [Instalação](installation.md) · 🧪 [Teste mínimo](minimal_test.md) · 🧠 [Reivindicações](claims.md) · 🔁 [Pós-reboot](runtime_after_reboot.md)

---

## 🎯 Objetivo

Este documento descreve como executar e analisar os experimentos do artefato, com foco em:

* reprodução dos cenários do artigo
* controle dos parâmetros experimentais
* coleta de evidências
* interpretação dos resultados

---

# 🧠 1. Modelo experimental

A avaliação é estruturada em dois eixos ortogonais:

## 🎛️ Controle

* **baseline**: comportamento tradicional
* **adapt**: adaptação via L2i

## 🧪 Backend

* **mock**: execução lógica
* **real**: aplicação em `tc`, NETCONF e P4

---

## 📊 Combinação

| Modo            | Controle   | Backend |
| --------------- | ---------- | ------- |
| baseline + mock | estático   | lógico  |
| baseline + real | estático   | real    |
| adapt + mock    | adaptativo | lógico  |
| adapt + real    | adaptativo | real    |

---

# 🚀 2. Execução rápida (recomendada)

```bash id="exp_fast01"
./setup_all.sh run_s1_real
./setup_all.sh run_s2_real
```

---

## ⏱️ Tempo esperado

| Cenário | Tempo típico |
| ------- | ------------ |
| S1      | ~10–30 s     |
| S2      | ~20–40 s     |

---

# 🧪 3. Cenários experimentais

---

## 🔹 S1 — Unicast com QoS

Objetivo:

* validar controle de largura de banda
* observar isolamento entre fluxos
* avaliar comportamento baseline vs adapt

---

## 🔹 S2 — Multicast orientado à origem

Objetivo:

* validar adaptação dinâmica
* observar convergência após eventos
* analisar estabilidade temporal

---

# ⚙️ 4. Execução controlada (manual)

Esta seção descreve a execução com controle fino, equivalente aos experimentos do artigo.

---

## 4.1 Pré-requisitos

```bash id="exp_manual01"
source ~/l2i-dev/venv/bin/activate
./setup_all.sh start_real_services
```

---

## 4.2 Cenário S1

```bash id="exp_s1_manual"
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

## 4.3 Cenário S2

```bash id="exp_s2_manual"
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

# 📊 5. Resultados

Os resultados são armazenados em:

```bash id="exp_results01"
results/
```

---

## Estrutura típica

```text id="exp_struct01"
results/
├── S1/
│   ├── S1_<timestamp>.json
│   ├── S1_<timestamp>_domain_A.json
│   ├── S1_<timestamp>_domain_B.json
│   └── S1_<timestamp>_domain_C.json
└── S2/
    └── ...
```

---

## Conteúdo

* sumário global
* dados por domínio
* métricas auxiliares
* dumps de configuração

---

# 🔍 6. O que observar

---

## ✔️ Execução bem-sucedida

```json id="exp_success01"
"backend_apply": {
  "A": true,
  "B": true,
  "C": true
}
```

---

## ✔️ Domínio A (Linux tc)

* aplicação de classes
* limitação de banda

---

## ✔️ Domínio B (NETCONF)

* envio de configuração YANG
* alteração do estado running

---

## ✔️ Domínio C (P4)

* pipeline carregado
* comportamento coerente com intenção

---

# 📈 7. Interpretação científica

Os experimentos demonstram:

* separação entre intenção e implementação
* tradução consistente entre domínios
* capacidade de adaptação dinâmica
* previsibilidade sob contenção

---

# 🔁 8. Repetição e variabilidade

Para avaliar estabilidade:

```bash id="exp_repeat01"
./setup_all.sh run_s1_real
./setup_all.sh run_s1_real
```

Comparar resultados em:

```bash id="exp_repeat02"
results/S1/
```

---

# ⚠️ 9. Problemas comuns

---

## ❌ backend_apply = false

Indica falha em algum domínio.

---

### NETCONF

```bash id="exp_troubleshoot01"
ss -ltnp | grep 830
```

---

### P4

```bash id="exp_troubleshoot02"
ss -ltnp | grep 9559
```

---

## ❌ resultados inconsistentes

Possíveis causas:

* recursos limitados
* interferência de processos
* execução concorrente

---

# 📌 10. Conclusão

Este conjunto de experimentos permite:

* reproduzir os cenários do artigo
* validar o pipeline declarativo
* observar comportamento multidomínio
* analisar adaptação dinâmica

---

👉 Evidências formais: [docs/claims.md](claims.md)
