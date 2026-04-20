# 🧠 Reivindicações e Evidências

🏠 [README](../README.md) · ⚙️ [Instalação](installation.md) · 🧪 [Teste mínimo](minimal_test.md) · 📊 [Experimentos](experiments.md)

---

## 🎯 Objetivo

Este documento apresenta as principais reivindicações do artefato, associando:

* procedimento de execução
* evidência observável
* interpretação dos resultados

Cada reivindicação pode ser verificada de forma independente.

---

# 📌 Reivindicação 1 — Inicialização da infraestrutura multidomínio

## Enunciado

A infraestrutura composta por três domínios heterogêneos pode ser inicializada simultaneamente em um único ambiente.

---

## ▶️ Procedimento

```bash id="c1_proc"
./setup_all.sh start_real_services
```

---

## 📊 Evidência

```bash id="c1_ev"
ss -ltnp | grep 830
ss -ltnp | grep 9559
```

---

## ⏱️ Tempo esperado

~5–10 segundos

---

## 💻 Recursos

CPU leve, sem tráfego significativo

---

## 🧠 Interpretação

A ativação simultânea de:

* NETCONF (porta 830)
* P4 (porta 9559)

indica que os domínios estão operacionais e prontos para execução conjunta.

---

# 📌 Reivindicação 2 — Execução fim a fim do pipeline declarativo

## Enunciado

Uma especificação declarativa pode ser processada, traduzida e aplicada em múltiplos domínios de forma consistente.

---

## ▶️ Procedimento

```bash id="c2_proc"
./setup_all.sh run_s1_real
```

---

## 📊 Evidência

```bash id="c2_ev"
grep -R '"backend_apply"' results/ -n
```

---

## Resultado esperado

```json id="c2_json"
"backend_apply": {
  "A": true,
  "B": true,
  "C": true
}
```

---

## ⏱️ Tempo esperado

~10–30 segundos

---

## 💻 Recursos

CPU: 1–2 cores
RAM: ~2 GB

---

## 🧠 Interpretação

Indica que:

* a especificação foi validada
* a tradução foi realizada corretamente
* a materialização ocorreu em todos os domínios

---

# 📌 Reivindicação 3 — Materialização multidomínio da intenção

## Enunciado

Uma única intenção declarativa é materializada de forma consistente em múltiplas tecnologias de L2.

---

## ▶️ Procedimento

Executar S1:

```bash id="c3_proc"
./setup_all.sh run_s1_real
```

---

## 📊 Evidência

Arquivos em:

```bash id="c3_ev"
results/S1/
```

Incluem:

* sumário global
* artefatos por domínio
* dumps de configuração

---

## 🧠 Interpretação

A existência de artefatos distintos por domínio demonstra:

* decomposição da intenção
* tradução para diferentes backends
* preservação semântica entre tecnologias

---

# 📌 Reivindicação 4 — Separação entre validação lógica e execução real

## Enunciado

O sistema distingue claramente entre validação lógica (mock) e execução real.

---

## ▶️ Procedimento

```bash id="c4_proc"
./setup_all.sh run_s1_mock
./setup_all.sh run_s1_real
```

---

## 📊 Evidência

* `mock`: não altera estado da rede
* `real`: altera comportamento observável

---

## 🧠 Interpretação

Permite:

* validar DSL independentemente da infraestrutura
* isolar erros de tradução vs execução

---

# 📌 Reivindicação 5 — Adaptação dinâmica via L2i

## Enunciado

O modo adaptativo altera o comportamento da rede em relação ao baseline.

---

## ▶️ Procedimento

```bash id="c5_proc"
sudo ~/l2i-dev/venv/bin/python -m scenarios.multidomain_s1 \
  --spec specs/valid/s1_unicast_qos.json \
  --mode baseline --backend real
```

e:

```bash id="c5_proc2"
sudo ~/l2i-dev/venv/bin/python -m scenarios.multidomain_s1 \
  --spec specs/valid/s1_unicast_qos.json \
  --mode adapt --backend real
```

---

## 📊 Evidência

Diferenças em:

* vazão
* latência
* isolamento de fluxos

---

## 🧠 Interpretação

Evidencia o impacto direto da abordagem declarativa na adaptação da rede.

---

# 📌 Reivindicação 6 — Suporte a multicast orientado à origem com adaptação temporal

## Enunciado

O sistema suporta multicast orientado à origem com adaptação dinâmica a eventos.

---

## ▶️ Procedimento

```bash id="c6_proc"
./setup_all.sh run_s2_real
```

---

## 📊 Evidência

* execução completa
* geração de séries temporais
* resposta ao evento `join`

---

## ⏱️ Tempo esperado

~20–40 segundos

---

## 💻 Recursos

CPU moderado (tráfego ativo)
RAM ~2–4 GB

---

## 🧠 Interpretação

Indica que:

* o sistema reage a eventos dinâmicos
* a configuração multicast é adaptada em tempo de execução
* há convergência para estado estável

---

# 📌 Reivindicação 7 — Reprodutibilidade experimental

## Enunciado

Os experimentos podem ser reproduzidos de forma consistente com baixa variabilidade.

---

## ▶️ Procedimento

Executar múltiplas vezes:

```bash id="c7_proc"
./setup_all.sh run_s1_real
```

---

## 📊 Evidência

* resultados consistentes em `results/`
* variação limitada em métricas

---

## 🧠 Interpretação

Indica que:

* o ambiente é controlado
* o pipeline é determinístico
* o artefato é reprodutível

---

# 🔁 Fluxo recomendado

```text id="c_flow"
1. instalação
2. start_real_services
3. run_s1_real
4. run_s2_real
```

---

# 📌 Observações finais

* todas as evidências são geradas automaticamente
* não há dependência externa adicional
* os resultados podem ser analisados diretamente a partir dos artefatos gerados

---
