# 🧠 Reivindicações e Evidências

🏠 [README](../README.md) · ⚙️ [Instalação](installation.md) · 🧪 [Teste mínimo](minimal_test.md) · 📊 [Experimentos](experiments.md)

---

## 🎯 Objetivo

Este documento explicita as **reivindicações observáveis do artefato**, associando:

* procedimento de execução
* evidência gerada
* interpretação dos resultados

O foco está em propriedades que podem ser verificadas diretamente a partir da execução dos cenários.

---

# 📌 Reivindicação 1 — Inicialização da infraestrutura heterogênea

## Enunciado

A infraestrutura experimental composta por três domínios heterogêneos pode ser inicializada e operada simultaneamente em um único ambiente.

Domínios considerados:

* A — Linux (`tc`)
* B — NETCONF/YANG
* C — P4

---

## Procedimento

```bash
./setup_all.sh start_real_services
```

---

## Evidência

```bash
ss -ltnp | grep 830
ss -ltnp | grep 9559
```

---

## Resultado esperado

* porta `830` ativa → servidor NETCONF operacional
* porta `9559` ativa → switch P4 operacional

---

## Interpretação

A presença simultânea desses serviços indica que os componentes heterogêneos estão ativos e prontos para execução conjunta.

---

# 📌 Reivindicação 2 — Execução fim a fim do pipeline declarativo

## Enunciado

Uma especificação declarativa pode ser processada, traduzida e aplicada em múltiplos domínios, gerando evidência consistente de execução.

---

## Procedimento

```bash
./setup_all.sh run_s1_real
```

---

## Evidência

```bash
grep -R '"backend_apply"' results/ -n
```

---

## Resultado esperado

```json
"backend_apply": {
  "A": true,
  "B": true,
  "C": true
}
```

---

## Interpretação

A aplicação bem-sucedida em todos os domínios indica que o pipeline declarativo:

* interpretou corretamente a especificação
* gerou configurações válidas
* executou a materialização em ambiente real

---

# 📌 Reivindicação 3 — Materialização multidomínio da intenção

## Enunciado

Uma única intenção declarativa é materializada de forma consistente sobre diferentes tecnologias de camada de enlace.

---

## Procedimento

```bash
./setup_all.sh run_s1_real
```

---

## Evidência

Arquivos gerados em:

```bash
results/
```

Incluem:

* sumário do cenário
* artefatos por domínio
* evidências auxiliares

---

## Interpretação

A presença de artefatos distintos por domínio indica que:

* a intenção foi traduzida para múltiplos contextos
* cada backend recebeu uma materialização compatível com sua semântica

---

# 📌 Reivindicação 4 — Suporte a multicast orientado à origem com evento dinâmico

## Enunciado

O sistema suporta comunicação multicast orientada à origem com adaptação a eventos dinâmicos durante a execução.

---

## Procedimento

```bash
./setup_all.sh run_s2_real
```

---

## Evidência

* execução completa sem falhas
* geração de artefatos do cenário S2
* presença de atividade no domínio P4
* registro do evento de `join`

---

## Interpretação

O comportamento observado indica que o sistema:

* trata múltiplos receptores
* adapta a configuração dinamicamente
* mantém consistência durante mudanças no grupo multicast

---

# 📌 Reivindicação 5 — Separação entre validação lógica e materialização

## Enunciado

O sistema permite distinguir entre validação lógica do pipeline e aplicação real em infraestrutura.

---

## Procedimento

```bash
./setup_all.sh run_s1_mock
./setup_all.sh run_s1_real
```

---

## Evidência

* execução em `mock` não altera o estado da infraestrutura
* execução em `real` produz efeitos observáveis

---

## Interpretação

Essa distinção permite:

* validar a DSL independentemente do ambiente
* isolar erros de tradução de erros de execução

---

# 📌 Reivindicação 6 — Comparação entre comportamento tradicional e adaptativo

## Enunciado

O sistema permite comparar comportamento sem adaptação declarativa (`baseline`) e com adaptação (`adapt`).

---

## Procedimento

Execução manual:

```bash
sudo ~/l2i-dev/venv/bin/python -m scenarios.multidomain_s1 \
  --spec specs/valid/s1_unicast_qos.json \
  --mode baseline \
  --backend real
```

e:

```bash
sudo ~/l2i-dev/venv/bin/python -m scenarios.multidomain_s1 \
  --spec specs/valid/s1_unicast_qos.json \
  --mode adapt \
  --backend real
```

---

## Evidência

Diferenças observáveis em:

* comportamento de tráfego
* aplicação de políticas
* métricas coletadas

---

## Interpretação

A comparação evidencia o impacto da abordagem declarativa na adaptação da rede.

---

# 🔁 Fluxo recomendado

```text
1. ./setup_all.sh all
2. ./setup_all.sh start_real_services
3. ./setup_all.sh run_s1_real
4. ./setup_all.sh run_s2_real
```

---

# 📎 Relação com os experimentos

As reivindicações aqui descritas correspondem diretamente aos cenários apresentados em:

👉 [docs/experiments.md](experiments.md)

---

# 📌 Observações finais

* As evidências são geradas automaticamente durante a execução
* Não há dependência de ferramentas externas adicionais
* O comportamento observado é consistente em execuções repetidas

---
