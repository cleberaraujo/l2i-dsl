# 🧠 Reivindicações e Evidências

🏠 [README](../README.md) · ⚙️ [Instalação](installation.md) · 🧪 [Teste mínimo](minimal_test.md) · 📊 [Experimentos](experiments.md)

---

## 🎯 Objetivo

Este documento apresenta as principais reivindicações do artefato, estabelecendo uma ligação explícita entre:

* execução experimental
* evidência observável
* interpretação dos resultados

Cada reivindicação pode ser validada de forma independente.

---

# 📌 Reivindicação 1 — Inicialização multidomínio

## Enunciado

A infraestrutura composta por múltiplos domínios heterogêneos pode ser inicializada simultaneamente em um único ambiente.

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

## ✅ Resultado esperado

* porta 830 ativa → NETCONF
* porta 9559 ativa → P4

---

## 🧠 Interpretação

A ativação simultânea dos serviços indica que os domínios estão prontos para execução coordenada.

---

# 📌 Reivindicação 2 — Execução fim a fim do pipeline declarativo

## Enunciado

Uma especificação declarativa pode ser processada e materializada em múltiplos domínios.

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

## ✅ Resultado esperado

```json id="c2_json"
"backend_apply": {
  "A": true,
  "B": true,
  "C": true
}
```

---

## 🧠 Interpretação

Indica execução completa do pipeline:

* validação da especificação
* tradução
* aplicação nos três domínios

---

# 📌 Reivindicação 3 — Materialização multidomínio

## Enunciado

Uma única intenção declarativa é materializada de forma consistente em múltiplas tecnologias.

---

## ▶️ Procedimento

```bash id="c3_proc"
./setup_all.sh run_s1_real
```

---

## 📊 Evidência

Arquivos em:

```bash id="c3_ev"
results/S1/
```

---

## ✅ Resultado esperado

Presença de artefatos por domínio:

* `domain_A.json`
* `domain_B.json`
* `domain_C.json`

---

## 🧠 Interpretação

Demonstra:

* decomposição da intenção
* tradução heterogênea
* preservação semântica

---

# 📌 Reivindicação 4 — Separação entre validação e execução

## Enunciado

O sistema separa claramente validação lógica e execução real.

---

## ▶️ Procedimento

```bash id="c4_proc"
./setup_all.sh run_s1_mock
./setup_all.sh run_s1_real
```

---

## 📊 Evidência

Diferença entre:

* modo `mock` → validação sem efeitos
* modo `real` → aplicação efetiva

---

## 🧠 Interpretação

Permite:

* depuração controlada
* isolamento de erros
* validação incremental

---

# 📌 Reivindicação 5 — Adaptação dinâmica

## Enunciado

O comportamento da rede varia conforme o modo de execução.

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

Diferenças observáveis em:

* vazão
* latência
* isolamento

---

## 🧠 Interpretação

Evidencia a capacidade de adaptação dinâmica do modelo declarativo.

---

# 📌 Reivindicação 6 — Suporte a multicast orientado à origem

## Enunciado

O sistema suporta multicast com adaptação a eventos dinâmicos.

---

## ▶️ Procedimento

```bash id="c6_proc"
./setup_all.sh run_s2_real
```

---

## 📊 Evidência

Arquivos em:

```bash id="c6_ev"
results/S2/
```

---

## 🧠 Interpretação

Indica:

* suporte a múltiplos receptores
* adaptação a eventos (join)
* convergência do sistema

---

# 📌 Reivindicação 7 — Reprodutibilidade

## Enunciado

Os experimentos produzem resultados consistentes sob repetição.

---

## ▶️ Procedimento

```bash id="c7_proc"
./setup_all.sh run_s1_real
./setup_all.sh run_s1_real
```

---

## 📊 Evidência

Comparação de arquivos em:

```bash id="c7_ev"
results/S1/
```

---

## 🧠 Interpretação

Indica:

* baixa variabilidade
* comportamento previsível
* ambiente controlado

---

# 📌 Resumo

As reivindicações demonstram que o artefato:

* executa pipeline declarativo completo
* opera em múltiplos domínios
* suporta adaptação dinâmica
* apresenta comportamento reprodutível

---

👉 Execução detalhada: [docs/experiments.md](experiments.md)
