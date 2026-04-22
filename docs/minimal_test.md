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

# 🚀 Fluxo mínimo (recomendado)

```bash
source ~/l2i-dev/venv/bin/activate
./setup_all.sh start_real_services
./setup_all.sh run_s1_real
```

---

## ⏱️ Tempo esperado

| Etapa               | Tempo típico |
| ------------------- | ------------ |
| start_real_services | ~5–10 s      |
| execução S1         | ~10–30 s     |

---

# 🔍 Verificação

Após a execução:

```bash
grep -R '"backend_apply"' results/ -n
```

---

## ✅ Resultado esperado

No arquivo de sumário do cenário S1:

```json
"backend_apply": {
  "A": true,
  "B": true,
  "C": true
}
```

---

# 🧠 Interpretação

Este resultado indica que:

* a especificação declarativa foi interpretada corretamente
* o pipeline de tradução foi executado
* a materialização ocorreu em três domínios distintos
* a execução fim a fim foi bem-sucedida

Isso constitui uma evidência direta de funcionamento do modelo proposto no artigo.

---

# 🔎 Execução passo a passo (opcional)

Para inspeção detalhada:

---

## 1) Ativar ambiente Python

```bash
source ~/l2i-dev/venv/bin/activate
```

---

## 2) Subir serviços reais

```bash
./setup_all.sh start_real_services
```

---

## 3) Verificar serviços

```bash
ss -ltnp | grep 830
ss -ltnp | grep 9559
```

Resultado esperado:

* `*:830` → NETCONF ativo
* `*:9559` → P4 ativo

---

## 4) Executar cenário S1

```bash
./setup_all.sh run_s1_real
```

---

# ⚠️ Problemas comuns

---

## ❌ Porta 830 não ativa

➡️ NETCONF não iniciou corretamente

```bash
./setup_all.sh start_real_services
```

---

## ❌ Porta 9559 não ativa

➡️ P4 não iniciou corretamente

```bash
./setup_all.sh start_real_services
```

---

## ❌ backend_apply B = false

➡️ falha no domínio NETCONF

Verificar:

* usuário `netconf`
* chave SSH (`~/.ssh/l2i_netconf_key`)
* módulo YANG `l2i-qos`
* política NACM

---

## ❌ backend_apply C = false

➡️ falha no domínio P4

```bash
ss -ltnp | grep 9559
```

---

# 📌 Conclusão

Se o teste mínimo for bem-sucedido, o artefato:

* está corretamente instalado
* possui serviços operacionais
* executa o pipeline declarativo
* produz materialização multidomínio

---

👉 Para execução completa e controle experimental:

📊 [docs/experiments.md](experiments.md)
