# 🖥️ Máquina Virtual (VM)

🏠 [README](../README.md) · ⚙️ [Instalação](installation.md) · 🔁 [Pós-reboot](runtime_after_reboot.md) · 🧪 [Teste mínimo](minimal_test.md) · 📊 [Experimentos](experiments.md)

---

## 🎯 Objetivo

Este documento descreve a máquina virtual disponibilizada para execução do artefato em ambiente previamente configurado.

A VM reduz o esforço de preparação ao evitar:

* compilação do stack P4
* compilação do stack NETCONF
* configuração manual do ambiente

---

## 📦 Download

👉 **Link para download:**
https://doi.org/10.5281/zenodo.19656440

Formato:

* VirtualBox (`.ova`)

---

## 🧩 Especificações

| Recurso             | Valor                        |
| ------------------- | ---------------------------- |
| Sistema operacional | Ubuntu Server 24.04 LTS      |
| CPU                 | 2 vCPUs (mínimo recomendado) |
| RAM                 | 4 GB                         |
| Disco               | ~20–30 GB                    |

---

## 🔐 Acesso

```text
user: sbrc
pass: sbrc
```

---

## 📁 Estrutura interna

Dentro da VM:

```bash id="vmpath01"
/home/sbrc/l2i-dsl
```

Ambiente configurado:

* repositório clonado (`l2i-dsl`)
* ambiente Python (`~/l2i-dev/venv`)
* dependências Python instaladas
* stack P4 instalado (PI, bmv2, p4c)
* stack NETCONF instalado (libyang, sysrepo, Netopeer2)
* módulo YANG `l2i-qos`
* política NACM aplicada

---

## 🧠 Observação importante

A VM foi construída seguindo exatamente os passos descritos em:

* [docs/installation.md](installation.md)

Portanto, seu comportamento é equivalente ao ambiente instalado manualmente.

---

# 🚀 Uso básico

## 1) Iniciar VM

Importar o `.ova` no VirtualBox e iniciar a máquina.

---

## 2) Acessar repositório

```bash id="vmcmd01"
cd ~/l2i-dsl
```

---

## 3) Ativar ambiente Python

```bash id="vmcmd02"
source ~/l2i-dev/venv/bin/activate
```

---

## 4) Subir serviços

```bash id="vmcmd03"
./setup_all.sh start_real_services
```

---

## 5) Executar teste mínimo

```bash id="vmcmd04"
./setup_all.sh run_s1_real
```

---

# 🧪 Execução manual (controle completo)

A VM permite executar exatamente o mesmo fluxo manual descrito no artefato.

---

## Ordem obrigatória

```text id="vmorder01"
NETCONF → P4 → pipeline → topologia → experimento
```

---

## Subir NETCONF

```bash id="vmcmd05"
sudo /usr/local/sbin/netopeer2-server -d
```

---

## Subir P4

```bash id="vmcmd06"
./scripts/p4_build_and_run.sh
```

---

## Carregar pipeline

```bash id="vmcmd07"
python scripts/p4_push_pipeline.py --addr 127.0.0.1:9559
```

---

## Criar topologia

```bash id="vmcmd08"
sudo ./scripts/s1_topology_setup.sh
```

---

## Executar S1

```bash id="vmcmd09"
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

## Executar S2

```bash id="vmcmd10"
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

# 📊 Resultados

```bash id="vmcmd11"
results/
```

Contém:

* sumários JSON
* séries temporais (CSV)
* dumps de configuração
* logs

---

# 🧹 Limpeza

```bash id="vmcmd12"
sudo ./scripts/s1_topology_cleanup.sh
sudo ./scripts/cleanup_net.sh
```

---

# ⚠️ Observações

* nenhum componente precisa ser recompilado
* serviços são iniciados sob demanda
* execução é independente de conectividade externa

---

# 📌 Quando utilizar a VM

A VM é recomendada quando:

* o ambiente local possui recursos limitados
* deseja-se evitar longos tempos de compilação
* deseja-se execução rápida dos cenários

A instalação manual é recomendada quando:

* há interesse em compreender o ambiente em detalhe
* deseja-se modificar ou estender o sistema

---

# 📌 Fluxo mínimo

```bash id="vmcmd13"
cd ~/l2i-dsl
source ~/l2i-dev/venv/bin/activate
./setup_all.sh start_real_services
./setup_all.sh run_s1_real
```

---
