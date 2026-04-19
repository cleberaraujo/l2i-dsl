# 🔁 Execução Após Reinicialização

🏠 [README](../README.md) · ⚙️ [Instalação](installation.md) · 🧪 [Teste mínimo](minimal_test.md) · 📊 [Experimentos](experiments.md)

---

## 🎯 Objetivo

Este guia descreve como retomar a execução do artefato após reiniciar a máquina, sem necessidade de recompilação ou reinstalação.

---

## 🧩 Princípio

Após a instalação inicial completa:

> **Nenhum componente precisa ser recompilado após reboot.**

A execução depende apenas de:

1. ativar o ambiente Python
2. iniciar os serviços
3. executar os cenários

---

# 🚀 Passo a passo

## 1) Ativar ambiente Python

```bash id="0f8cql"
source ~/l2i-dev/venv/bin/activate
```

Verificação opcional:

```bash id="8vhr4l"
which python
python -V
```

---

## 2) Iniciar serviços

```bash id="7y27xm"
./setup_all.sh start_real_services
```

Este comando:

* inicia o servidor NETCONF (porta 830), se necessário
* inicia o switch P4 (porta 9559)
* reaplica o pipeline P4

---

## 3) Validar serviços

```bash id="dxq0n2"
ss -ltnp | grep 830
ss -ltnp | grep 9559
```

Resultado esperado:

* `*:830` em LISTEN → NETCONF ativo
* `*:9559` em LISTEN → P4 ativo

---

## 4) Executar cenários

### S1 — Unicast QoS

```bash id="5n3v1r"
./setup_all.sh run_s1_real
```

---

### S2 — Multicast orientado à origem

```bash id="7on0b6"
./setup_all.sh run_s2_real
```

---

# 🧪 Fluxo mínimo

```bash id="5u1rkb"
source ~/l2i-dev/venv/bin/activate
./setup_all.sh start_real_services
./setup_all.sh run_s1_real
```

---

# ⚠️ Problemas comuns

## Porta 830 não ativa

```bash id="dx3fhs"
./setup_all.sh start_real_services
```

---

## Porta 9559 não ativa

```bash id="y6k2gq"
./setup_all.sh start_real_services
```

---

## Falha de autenticação NETCONF

```bash id="r7n7pk"
./setup_all.sh configure_netconf
```

---

## Ambiente Python inconsistente

```bash id="rf2hkv"
rm -rf ~/l2i-dev/venv
./setup_all.sh python_env
```

---

# 🧠 Observações

* os serviços não são persistidos automaticamente
* o controle manual favorece previsibilidade
* o tempo de recuperação é tipicamente inferior a 10 segundos

---

# 📌 Resumo

```bash id="l3r1gd"
source ~/l2i-dev/venv/bin/activate
./setup_all.sh start_real_services
./setup_all.sh run_s1_real
```

---

👉 Próximo: [docs/vm.md](vm.md)
