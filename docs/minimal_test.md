# 🧪 Teste Mínimo

🏠 [README](../README.md) · ⚙️ [Instalação](installation.md) · 📊 [Experimentos](experiments.md) · 🔁 [Pós-reboot](runtime_after_reboot.md)

---

## 🎯 Objetivo

Este teste valida, de forma rápida e determinística, que:

* o ambiente foi instalado corretamente
* os serviços reais estão operacionais
* o pipeline declarativo (L2i) executa fim a fim
* a materialização ocorre em todos os domínios

---

## ✅ Pré-requisitos

Antes de iniciar, é necessário que a instalação tenha sido concluída:

```bash
./setup_all.sh all
```

ou processo manual equivalente.

---

## 🚀 Passo 1 — Ativar ambiente Python

```bash
source ~/l2i-dev/venv/bin/activate
```

Verificação opcional:

```bash
which python
python -V
```

---

## 🚀 Passo 2 — Subir serviços reais

```bash
./setup_all.sh start_real_services
```

Este comando:

* inicia o servidor NETCONF (porta 830)
* inicia o switch P4 (porta 9559)
* aplica o pipeline P4

---

## 🔍 Passo 3 — Validar serviços

```bash
ss -ltnp | grep 830
ss -ltnp | grep 9559
```

### Resultado esperado

* `*:830` em LISTEN → NETCONF ativo
* `*:9559` em LISTEN → P4 ativo

---

## 🚀 Passo 4 — Executar cenário S1 (modo real)

```bash
./setup_all.sh run_s1_real
```

Durante a execução, o sistema irá:

* criar a topologia (namespaces + veth)
* aplicar configurações em:

  * domínio A (Linux tc)
  * domínio B (NETCONF/YANG)
  * domínio C (P4)
* gerar tráfego (iperf3)
* coletar métricas e artefatos

---

## 🔍 Passo 5 — Validar resultados

```bash
grep -R '"backend_apply"' results/ -n
```

---

## ✅ Resultado esperado

No arquivo de sumário do S1:

```json
"backend_apply": {
  "A": true,
  "B": true,
  "C": true
}
```

---

## 📌 Interpretação

Se todos os domínios retornarem `true`, então:

* a especificação declarativa foi interpretada corretamente
* o pipeline de tradução foi executado
* a aplicação ocorreu em múltiplos domínios reais
* a integração fim a fim está funcional

---

## ⚠️ Problemas comuns

### ❌ Porta 830 não ativa

➡️ NETCONF não iniciou corretamente

Solução:

```bash
./setup_all.sh start_real_services
```

---

### ❌ Porta 9559 não ativa

➡️ P4 não iniciou corretamente

Solução:

```bash
./setup_all.sh start_real_services
```

---

### ❌ backend_apply B = false

➡️ falha no domínio NETCONF

Verificar:

* usuário `netconf`
* chave SSH
* módulo YANG `l2i-qos`
* política NACM

---

### ❌ backend_apply C = false

➡️ falha no domínio P4

Verificar:

```bash
ss -ltnp | grep 9559
```

---

## ⏱️ Tempo esperado

| Etapa               | Tempo    |
| ------------------- | -------- |
| start_real_services | ~5–10 s  |
| execução S1         | ~10–30 s |

---

## 🧠 Observação

Este teste foi projetado para validar o sistema **ponta a ponta**, com mínima intervenção do usuário.

Para exploração completa dos cenários e parâmetros:

👉 [docs/experiments.md](experiments.md)

---
