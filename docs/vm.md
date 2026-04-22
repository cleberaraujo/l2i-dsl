# 🖥️ Máquina Virtual (VM)

🏠 [README](../README.md) · ⚙️ [Instalação](installation.md) · 🧪 [Teste mínimo](minimal_test.md) · 📊 [Experimentos](experiments.md)

---

## 🎯 Objetivo

Disponibilizar um ambiente pré-configurado para execução imediata do artefato, evitando etapas de instalação e compilação.

---

# 📦 Download

👉 **Link:** https://doi.org/10.5281/zenodo.19656440

Formato:

* VirtualBox (`.ova`)

---

# 🔐 Acesso

```text
user: sbrc
pass: sbrc
```

---

# 🧩 Especificações

| Recurso | Valor               |
| ------- | ------------------- |
| SO      | Ubuntu Server 24.04 |
| CPU     | 2 vCPUs             |
| RAM     | ≥ 4 GB              |
| Disco   | 20–30 GB            |

---

# 📁 Ambiente interno

```bash
/home/sbrc/l2i-dsl
```

Inclui:
* repositório configurado
* ambiente Python (`~/l2i-dev/venv`)
* stack P4
* stack NETCONF
* modelo YANG
* política NACM

---

# 🚀 Uso rápido

```bash
cd ~/l2i-dsl
source ~/l2i-dev/venv/bin/activate
./setup_all.sh start_real_services
./setup_all.sh run_s1_real
```

---

# 🧪 Execução manual

Fluxo:

```text
NETCONF → P4 → pipeline → topologia → experimento
```

---

## Exemplos

```bash
sudo /usr/local/sbin/netopeer2-server -d
./scripts/p4_build_and_run.sh
python scripts/p4_push_pipeline.py --addr 127.0.0.1:9559
sudo ./scripts/s1_topology_setup.sh
```

---

# 📊 Resultados

```bash
results/
```

---

# 🧹 Limpeza

```bash
sudo ./scripts/s1_topology_cleanup.sh
sudo ./scripts/cleanup_net.sh
```

---

# ⚠️ Observações

* nenhum build necessário
* execução imediata
* ideal para avaliação rápida

---

# 📌 Quando usar

Recomendada quando:
* deseja-se evitar compilação
* ambiente local é limitado
* avaliação rápida é necessária

---

👉 Detalhes completos: [docs/installation.md](installation.md)
