# 📊 Experimentos

🏠 [README](../README.md) · ⚙️ [Instalação](installation.md) · 🧪 [Teste mínimo](minimal_test.md) · 🧠 [Reivindicações](claims.md) · 🔁 [Pós-reboot](runtime_after_reboot.md)

---

## 🎯 Objetivo

Este documento descreve como executar e analisar os experimentos do artefato, com foco em:

* reprodução dos cenários do artigo
* controle dos parâmetros experimentais
* coleta de evidências
* interpretação dos resultados

---

# 🧠 1. Modelo experimental

A avaliação é estruturada em dois eixos ortogonais:

## 🎛️ Controle

* **baseline**: comportamento tradicional
* **adapt**: adaptação via L2i

## 🧪 Backend

* **mock**: execução lógica
* **real**: aplicação em `tc`, NETCONF e P4

---

## 📊 Combinação

| Modo            | Controle   | Backend |
| --------------- | ---------- | ------- |
| baseline + mock | estático   | lógico  |
| baseline + real | estático   | real    |
| adapt + mock    | adaptativo | lógico  |
| adapt + real    | adaptativo | real    |

---

# 🚀 2. Execução rápida (recomendada)

```bash id="exp_fast01"
./setup_all.sh run_s1_real
./setup_all.sh run_s2_real
```

---

## ⏱️ Tempo esperado

| Cenário | Tempo típico |
| ------- | ------------ |
| S1      | ~10–30 s     |
| S2      | ~20–40 s     |

---

# 🧪 3. Cenários experimentais

---

## 🔹 S1 — Unicast com QoS

Objetivo:

* validar controle de largura de banda
* observar isolamento entre fluxos
* avaliar comportamento baseline vs adapt

---

## 🔹 S2 — Multicast orientado à origem

Objetivo:

* validar adaptação dinâmica
* observar convergência após eventos
* analisar estabilidade temporal

---

# ⚙️ 4. Execução controlada (manual)

Esta seção descreve a execução com controle fino, equivalente aos experimentos do artigo.

---

## 4.1 Pré-requisitos

```bash id="exp_manual01"
source ~/l2i-dev/venv/bin/activate
./setup_all.sh start_real_services
```

---

## 4.2 Cenário S1

```bash id="exp_s1_manual"
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

## 4.3 Cenário S2

```bash id="exp_s2_manual"
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

# 📊 5. Resultados

Os resultados são armazenados em:

```bash id="exp_results01"
results/
```

---

## Estrutura típica

```text id="exp_struct01"
results/
├── S1/
│   ├── S1_<timestamp>.json
│   ├── S1_<timestamp>_domain_A.json
│   ├── S1_<timestamp>_domain_B.json
│   └── S1_<timestamp>_domain_C.json
└── S2/
    └── ...
```

---

## Conteúdo

* sumário global
* dados por domínio
* métricas auxiliares
* dumps de configuração

---

# 🔍 6. O que observar

---

## ✔️ Execução bem-sucedida

```json id="exp_success01"
"backend_apply": {
  "A": true,
  "B": true,
  "C": true
}
```

---

## ✔️ Domínio A (Linux tc)

* aplicação de classes
* limitação de banda

---

## ✔️ Domínio B (NETCONF)

* envio de configuração YANG
* alteração do estado running

---

## ✔️ Domínio C (P4)

* pipeline carregado
* comportamento coerente com intenção

---

# 📈 7. Interpretação científica

Os experimentos demonstram:

* separação entre intenção e implementação
* tradução consistente entre domínios
* capacidade de adaptação dinâmica
* previsibilidade sob contenção

---

# 🔁 8. Repetição e variabilidade

Para avaliar estabilidade:

```bash id="exp_repeat01"
./setup_all.sh run_s1_real
./setup_all.sh run_s1_real
```

Comparar resultados em:

```bash id="exp_repeat02"
results/S1/
```

---

# ⚠️ 9. Problemas comuns

---

## ❌ backend_apply = false

Indica falha em algum domínio.

---

### NETCONF

```bash id="exp_troubleshoot01"
ss -ltnp | grep 830
```

---

### P4

```bash id="exp_troubleshoot02"
ss -ltnp | grep 9559
```

---

## ❌ resultados inconsistentes

Possíveis causas:

* recursos limitados
* interferência de processos
* execução concorrente

---

# 📌 10. Conclusão

Este conjunto de experimentos permite:

* reproduzir os cenários do artigo
* validar o pipeline declarativo
* observar comportamento multidomínio
* analisar adaptação dinâmica

---

👉 Evidências formais: [docs/claims.md](claims.md)

---

# 🎯 10. Perfil calibrado do emissor S2/P4

O smoke test multicast no dataplane P4 utiliza, por padrão, o perfil calibrado
`phase13-tailspin-900us-affinity-v1`. A seleção foi realizada no caminho
completo `h1 → BMv2 → h3/h4`, e não apenas em loopback.

O perfil combina espera passiva repetida com uma espera ativa final limitada a
`900 µs`, fixa somente o emissor em uma CPU permitida e preserva a regra de não
compensar atrasos do escalonador com rajadas de pacotes.

```bash id="exp_s2_p4_calibrated_smoke"
./setup_all.sh run_s2_p4_dataplane_smoke
```

Os padrões aplicados são:

```text id="exp_s2_p4_profile_defaults"
S2_DP_PACING_MODE=repeated_sleep_spin
S2_DP_SPIN_THRESHOLD_US=900
S2_DP_SENDER_CPU=auto
S2_DP_REQUIRE_RATE_VALIDATION=1
S2_DP_MAX_ABS_RATE_ERROR_PCT=5
S2_DP_MIN_INTER_SEND_RATIO=0.98
```

O perfil completo e a proveniência da calibração estão registrados em
[`profiles/s2_sender_profile.json`](../profiles/s2_sender_profile.json).

Uma execução de diagnóstico exclusivamente voltada à replicação pode desativar
o gate de taxa de forma explícita:

```bash id="exp_s2_p4_replication_only"
S2_DP_REQUIRE_RATE_VALIDATION=0 \
./setup_all.sh run_s2_p4_dataplane_smoke
```

Essa opção não deve ser usada para sustentar afirmações sobre precisão da carga
oferecida. O perfil calibra a taxa do emissor e a replicação multicast; ele não
constitui, isoladamente, validação de QoS sob contenção nem de recuperação.


---

# 🎛️ 11. Perfil validado de contenção multicast S2/P4

O experimento de contenção multicast utiliza o perfil
`phase14-s2-p4-qos-contention-v1`. O BMv2 realiza a replicação multicast e o
encaminhamento unicast do tráfego concorrente, enquanto o Linux `tc` materializa
a contenção e a diferenciação no egresso compartilhado `s2b-h3`. O perfil não
atribui ao pipeline P4 mecanismos internos de filas, escalonamento ou garantia
de banda.

A matriz completa, com quatro repetições por modo e ordem contrabalanceada, pode
ser executada por:

```bash
./setup_all.sh run_s2_p4_qos_contention_semantic_validation
```

Os padrões promovidos para o experimento por modo são:

```text
S2_QOS_PROFILE_ID=phase14-s2-p4-qos-contention-v1
S2_QOS_CAPACITY_MBPS=3
S2_QOS_MULTICAST_RATE_MBPS=2
S2_QOS_MULTICAST_RESERVED_MBPS=2.1
S2_QOS_BACKGROUND_RATE_MBPS=2
S2_QOS_PACKET_SIZE=1200
S2_QOS_TC_OVERHEAD_BYTES=42
S2_QOS_QUEUE_LIMIT_PACKETS=64
S2_QOS_DURATION=12
S2_QOS_BACKGROUND_DURATION=18
S2_QOS_BACKGROUND_PREFILL_S=1.5
S2_QOS_BACKGROUND_DRAIN_S=1.5
```

A reserva de `2.1 Mbit/s` está no domínio de contabilização do `tc`. Para um
datagrama de payload de 1.200 bytes, os 42 bytes adicionais representam os
cabeçalhos Ethernet, IPv4 e UDP observados no ponto de enfileiramento, resultando
em 1.242 bytes contabilizados por pacote e em uma taxa esperada de
aproximadamente `2.07 Mbit/s` para uma carga útil de `2 Mbit/s`.

Na validação contrabalanceada, o baseline apresentou entrega mediana de `0.811`
no receptor B e p95 mediano de `213.210 ms`. O modo adapt apresentou entrega
integral em todas as execuções, p95 mediano de `1.635 ms`, margem mínima de
reserva de `0.032219 Mbit/s` e zero drops na classe multicast. O receptor C,
utilizado como controle não contendido, manteve entrega integral nos dois modos.

O perfil completo, os critérios de validação e o escopo das afirmações estão em
[`profiles/s2_qos_contention_profile.json`](../profiles/s2_qos_contention_profile.json).
A validação abrange diferenciação de QoS no egresso Linux compartilhado. Ela não
valida filas internas do P4 nem mecanismos de recuperação.


---

# ♻️ 12. Perfil validado de recuperação de estado multicast S2/P4

O experimento de recuperação utiliza o perfil
`phase15-s2-p4-state-recovery-v1`. O modelo de falha remove seletivamente, por
P4Runtime, a entrada da tabela multicast e o grupo PRE, confirma a ausência por
readback e rematerializa o estado desejado após uma retenção de falha
controlada. Um fluxo unicast independente permanece ativo durante todo o
ensaio, permitindo distinguir a perda seletiva do estado multicast de uma
reinicialização do BMv2.

A matriz temporal completa pode ser executada por:

```bash
./setup_all.sh run_s2_p4_state_recovery_validation
```

A validação usa oito execuções em ordem espelhada `A → B → C → D → D → C → B
→ A`, variando o instante de injeção entre `3.0 s` e `5.0 s` e a retenção da
falha entre `0.5 s` e `3.0 s`. Os padrões do experimento por execução são:

```text
S2_RECOVERY_PROFILE_ID=phase15-s2-p4-state-recovery-v1
S2_RECOVERY_MULTICAST_RATE_MBPS=2
S2_RECOVERY_CONTROL_RATE_MBPS=0.5
S2_RECOVERY_PACKET_SIZE=1200
S2_RECOVERY_SPIN_THRESHOLD_US=900
S2_RECOVERY_MINIMUM_POST_S=5
S2_RECOVERY_WINDOW_GUARD_S=0.25
S2_RECOVERY_MINIMUM_STABLE_DELIVERY=0.99
S2_RECOVERY_MINIMUM_CONTROL_DELIVERY=0.99
S2_RECOVERY_MAXIMUM_FAULT_DELIVERY=0.05
S2_RECOVERY_MAXIMUM_FIRST_PACKET_MS=50
```

Nas oito execuções, B e C mantiveram entrega integral antes da falha, entrega
nula enquanto o estado multicast esteve ausente e entrega integral após a
rematerialização. O controle unicast permaneceu em `1.0` em todas as janelas. A
detecção de ausência apresentou máximo de `8.873 ms`; a rematerialização com
confirmação por readback, máximo de `10.112 ms`; e o intervalo entre o início da
remediação e o primeiro pacote novamente recebido, máximos de `15.298 ms` em B
e `15.237 ms` em C.

O intervalo deliberado de retenção da falha não é contabilizado como latência de
recuperação. A validação cobre detecção e recuperação acionadas pelo próprio
harness, preservando explicitamente fora do escopo a detecção autônoma pelo MAD,
a recuperação autônoma, a reinicialização do BMv2 e a recarga do pipeline.

O perfil completo, os limiares e a delimitação das afirmações estão em
[`profiles/s2_state_recovery_profile.json`](../profiles/s2_state_recovery_profile.json).


---

# 🛡️ 13. Perfil validado de assurance autônomo S2/P4

O ciclo persistente de assurance utiliza o perfil
`phase16-s2-p4-autonomous-assurance-v1`. Diferentemente do ensaio da Seção 12,
o controlador não recebe o instante da falha nem um comando de recuperação. Um
processo independente remove seletivamente estado P4Runtime após uma barreira de
início do tráfego, enquanto o componente `MADAssuranceController` mantém o
estado desejado, realiza readback periódico, confirma a deriva, classifica os
componentes ausentes, rematerializa somente os componentes divergentes e
confirma a convergência por novas observações.

A matriz completa pode ser executada por:

```bash
./setup_all.sh run_s2_p4_autonomous_assurance_validation
```

A validação usa oito execuções em ordem espelhada `A → B → C → D → D → C → B
→ A`. A condição A não injeta falha e verifica falsos positivos. A condição B
remove somente o grupo multicast PRE; a condição C remove somente a entrada da
tabela multicast; e a condição D remove ambos os componentes e rejeita
sinteticamente a primeira tentativa de remediação para exercitar retry e
backoff limitados.

Os padrões promovidos para uma execução direta são:

```text
S2_ASSURANCE_PROFILE_ID=phase16-s2-p4-autonomous-assurance-v1
S2_ASSURANCE_DURATION=12
S2_ASSURANCE_FAULT_AFTER_S=4
S2_ASSURANCE_FAULT_KIND=both
S2_ASSURANCE_FORCED_REMEDIATION_REJECTIONS=0
S2_ASSURANCE_MULTICAST_RATE_MBPS=2
S2_ASSURANCE_CONTROL_RATE_MBPS=0.5
S2_ASSURANCE_PACKET_SIZE=1200
S2_ASSURANCE_SPIN_THRESHOLD_US=900
S2_ASSURANCE_POLL_INTERVAL_S=0.02
S2_ASSURANCE_DRIFT_CONFIRMATIONS=3
S2_ASSURANCE_CONVERGENCE_CONFIRMATIONS=2
S2_ASSURANCE_MAX_REMEDIATION_ATTEMPTS=3
S2_ASSURANCE_INITIAL_BACKOFF_S=0.01
S2_ASSURANCE_BACKOFF_MULTIPLIER=2
S2_ASSURANCE_MAX_BACKOFF_S=0.10
S2_ASSURANCE_MAX_DETECTION_MS=150
S2_ASSURANCE_MAX_CONTROL_PLANE_RECOVERY_MS=250
S2_ASSURANCE_MAX_TOTAL_RECONCILIATION_MS=400
```

Nas duas execuções sem falha, nenhum incidente foi declarado e a entrega
permaneceu integral. Nas seis execuções com deriva, a classificação e a
rematerialização seletiva foram exatas em todas as ocorrências. As duas
execuções da condição D registraram uma rejeição sintética, um evento de
backoff e convergência na segunda tentativa. A detecção apresentou máximo de
`90.053 ms`, a recuperação do plano de controle máximo de `107.633 ms` e a
reconciliação completa máximo de `192.341 ms`. B e C mantiveram entrega integral
antes da falha e após a convergência; o fluxo unicast de controle permaneceu em
`1.0` em todas as janelas.

O perfil valida assurance autônomo em um único domínio P4Runtime. Permanecem
fora do escopo a coordenação de assurance multidomínio, a reinicialização do
BMv2, a recarga do pipeline, a recompilação de intenções e a resolução de
conflitos entre políticas. A rejeição usada para validar retry e backoff é uma
falha sintética do adaptador experimental, não uma falha observada do backend.

O perfil completo e a delimitação das afirmações estão em
[`profiles/s2_autonomous_assurance_profile.json`](../profiles/s2_autonomous_assurance_profile.json).
