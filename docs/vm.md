# 🖥️ Máquina Virtual (VM)

🏠 [README](../README.md) · ⚙️ [Instalação](installation.md) · 🔁 [Pós-reboot](runtime_after_reboot.md) · 🧪 [Teste mínimo](minimal_test.md)

---

## 🎯 Objetivo

Este documento descreve a máquina virtual disponibilizada para facilitar a execução do artefato.

A VM elimina a necessidade de:

* compilação do stack P4
* compilação do stack NETCONF
* configuração manual do ambiente

---

## 📦 Download

👉 **Link para download:**

<!-- Inserir link -->

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
| Usuário             | `l2i`                        |
| Senha               | `l2i`                        |

---

## 📁 Estrutura interna

Dentro da VM:

```bash id="u9xg62"
/home/l2i/l2i-dsl
```

Ambiente pré-configurado:

* repositório clonado
* ambiente Python (`~/l2i-dev/venv`)
* stack P4 instalado
* stack NETCONF instalado
* módulo YANG `l2i-qos`
* política NACM aplicada

---

## 🚀 Uso básico

### 1) Importar no VirtualBox

* *File → Import Appliance*
* selecionar o arquivo `.ova`

---

### 2) Iniciar a VM

Credenciais:

```text id="apw23h"
user: l2i
pass: l2i
```

---

### 3) Acessar o repositório

```bash id="z9m2n7"
cd ~/l2i-dsl
```

---

### 4) Ativar ambiente Python

```bash id="5xk1ro"
source ~/l2i-dev/venv/bin/activate
```

---

### 5) Iniciar serviços

```bash id="px2yb8"
./setup_all.sh start_real_services
```

---

### 6) Executar teste mínimo

```bash id="a8xvkt"
./setup_all.sh run_s1_real
```

---

## 🧪 Fluxo mínimo

```bash id="r9lz0j"
cd ~/l2i-dsl
source ~/l2i-dev/venv/bin/activate
./setup_all.sh start_real_services
./setup_all.sh run_s1_real
```

Tempo típico: **menos de 2 minutos**

---

## ⚠️ Observações

* não é necessário recompilar nenhum componente
* todos os serviços são iniciados sob demanda
* a VM funciona de forma independente da rede externa

---

## 🔧 Customização

A VM permite:

* edição de especificações em `specs/`
* execução manual de cenários
* geração de novos resultados em `results/`

---

## 📌 Quando utilizar a VM

A VM é recomendada quando:

* o ambiente local possui recursos limitados
* deseja-se evitar longos tempos de compilação
* deseja-se executar rapidamente os cenários

A instalação manual é mais adequada quando:

* há interesse em estudar o ambiente em detalhe
* deseja-se modificar componentes do sistema

---

## 🧠 Integração com o restante do artefato

O uso da VM segue exatamente o mesmo fluxo descrito em:

* [docs/runtime_after_reboot.md](runtime_after_reboot.md)
* [docs/minimal_test.md](minimal_test.md)

---

## 📌 Resumo

```bash id="l2u8f2"
cd ~/l2i-dsl
source ~/l2i-dev/venv/bin/activate
./setup_all.sh start_real_services
./setup_all.sh run_s1_real
```

---
