# 📊 Experimentos

🏠 [README](../README.md) · ⚙️ [Instalação](installation.md) · 🧪 [Teste mínimo](minimal_test.md) · 🧠 [Reivindicações](claims.md) · 🔁 [Pós-reboot](runtime_after_reboot.md)

---

## 🎯 Objetivo

Este documento descreve como executar, controlar e interpretar os experimentos do artefato, incluindo:

* execução automatizada e manual
* controle fino de parâmetros
* análise de métricas
* validação das propriedades experimentais

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

# 🧪 2. Testbed

Os experimentos utilizam:

* network namespaces
* enlaces `veth`
* controle via `tc`
* medições com `iperf3` e `ping`

---

# 📐 3. Cenários

## 🔀 S1 — Unicast QoS

Avalia:

* isolamento de fluxos
* garantia de banda
* comportamento sob contenção

---

## 🌳 S2 — Multicast orientado à origem

Avalia:

* eventos dinâmicos (`join`)
* adaptação temporal
* estabilidade pós-recuperação

---

# ▶️ 4. Execução automatizada

```bash id="t_auto01"
./setup_all.sh run_s1_real
./setup_all.sh run_s2_real
```

---

# 🧪 5. Execução manual (controle total)

## Ordem obrigatória

```text id="t_order01"
NETCONF → P4 → pipeline → topologia → experimento
```

---

## Subir ambiente

```bash id="t_manual01"
sudo /usr/local/sbin/netopeer2-server -d
source ~/l2i-dev/venv/bin/activate
cd ~/l2i-dsl
./scripts/p4_build_and_run.sh
python scripts/p4_push_pipeline.py --addr 127.0.0.1:9559
sudo ./scripts/s1_topology_setup.sh
```

---

## Execução S1

```bash id="t_manual02"
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

## Execução S2

```bash id="t_manual03"
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

# ⏱️ 6. Tempo esperado

| Etapa               | Tempo típico |
| ------------------- | ------------ |
| start_real_services | ~5–10 s      |
| S1                  | ~10–30 s     |
| S2                  | ~20–40 s     |

---

# 💻 7. Consumo de recursos

| Recurso | Estimativa |
| ------- | ---------- |
| CPU     | 1–2 cores  |
| RAM     | ~2–4 GB    |
| Disco   | ~1–2 GB    |

Observações:

* build inicial (P4/NETCONF) requer mais recursos
* execução dos cenários é leve

---

# 📊 8. Métricas avaliadas

* latência (percentis, ex.: P99)
* vazão
* impacto de tráfego concorrente
* conformidade semântica
* tempo até estabilidade (S2)

---

# 📂 9. Resultados

```bash id="t_results01"
results/S1/
results/S2/
```

Cada execução gera:

* JSON (sumário)
* CSV (séries temporais)
* dumps de configuração
* logs

---

# 📌 10. Resultado esperado

```json id="t_expected01"
"backend_apply": {
  "A": true,
  "B": true,
  "C": true
}
```

---

# 🧠 11. Interpretação

Esse resultado indica:

* aplicação consistente em múltiplos domínios
* tradução correta da intenção
* execução fim a fim do pipeline

---

# 🔁 12. Reprodutibilidade

Recomenda-se:

* executar cada cenário pelo menos 3 vezes
* comparar variações de throughput e latência
* analisar estabilidade temporal (S2)

---

# 🧪 13. Variações experimentais

Exemplos:

* aumentar `be-mbps` → maior contenção
* reduzir `bwB` → gargalo em domínio intermediário
* alterar `phase-splits` → impacto no tempo de recuperação

---

# 🧹 14. Limpeza

```bash id="t_cleanup01"
sudo ./scripts/s1_topology_cleanup.sh
sudo ./scripts/cleanup_net.sh
```

---

# ✔️ 15. Verificações

```bash id="t_check01"
ss -ltnp | grep 830
ss -ltnp | grep 9559
```

---

👉 Próximo: [docs/claims.md](claims.md)
