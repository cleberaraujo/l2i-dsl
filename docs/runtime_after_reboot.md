# 🔁 Execução Após Reinicialização

🏠 [README](../README.md) · ⚙️ [Instalação](installation.md) · 🧪 [Teste mínimo](minimal_test.md) · 📊 [Experimentos](experiments.md)

---

## 🎯 Objetivo

Descrever como retomar a execução do artefato após reiniciar o sistema, sem necessidade de reinstalação.

---

# 🧩 Princípio

Após a instalação inicial:

> **nenhum componente precisa ser recompilado**

A execução depende apenas de:

1. ativar o ambiente Python
2. iniciar os serviços
3. executar os cenários

---

# 🚀 Fluxo mínimo

```bash
source ~/l2i-dev/venv/bin/activate
./setup_all.sh start_real_services
./setup_all.sh run_s1_real
```

---

# ⚙️ Passo a passo

## 1) Ambiente Python

```bash
source ~/l2i-dev/venv/bin/activate
```

---

## 2) Iniciar serviços

```bash
./setup_all.sh start_real_services
```

---

## 3) Verificar serviços

```bash
ss -ltnp | grep 830
ss -ltnp | grep 9559
```

---

## 4) Executar cenários

```bash
./setup_all.sh run_s1_real
./setup_all.sh run_s2_real
```

---

# ⚠️ Problemas comuns

## NETCONF não ativo

```bash
./setup_all.sh start_real_services
```

---

## P4 não ativo

```bash
./setup_all.sh start_real_services
```

---

## Falha NETCONF

```bash
./setup_all.sh configure_netconf
```

---

## Ambiente Python inconsistente

```bash
rm -rf ~/l2i-dev/venv
./setup_all.sh python_env
```

---

# 🧠 Observações

* serviços não são persistidos automaticamente
* inicialização é rápida (<10s)
* controle manual favorece previsibilidade

---

👉 Próximo: [docs/vm.md](vm.md)
