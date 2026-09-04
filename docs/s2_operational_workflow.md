# Workflow operacional S2 pós-certificação

O único engine e entrypoint operacional S2 é:

```bash
python -m scenarios.multidomain_s2 \
  --spec specs/valid/s2_multicast_source_oriented.json \
  --execution-id s2-operational-001 \
  --repetition 1 \
  --results-root results/S2 \
  --backend real \
  --mode adapt \
  --duration 30 \
  --packet-interval-ms 50 \
  --recovery-bin-ms 500 \
  --stable-k-bins 3
```

A ação de setup canônica correspondente é `./setup_all.sh run_s2_real`.

## Taxonomia da superfície publicada

- `run_s2_real`: `canonical_scenario`, único cenário/engine S2 canônico.
- `run_s2_p4_dataplane_smoke`: `specialized_validation_profile`.
- `run_s2_p4_qos_contention`: `specialized_validation_profile`.
- `run_s2_p4_qos_contention_semantic_validation`: `specialized_validation_profile`.
- `run_s2_p4_state_recovery`: `specialized_validation_profile`.
- `run_s2_p4_state_recovery_foundation`: `specialized_validation_profile`.
- `run_s2_p4_state_recovery_validation`: `specialized_validation_profile`.
- `run_s2_p4_autonomous_assurance`: `specialized_validation_profile`.
- `run_s2_p4_autonomous_assurance_foundation`: `specialized_validation_profile`.
- `run_s2_p4_autonomous_assurance_validation`: `specialized_validation_profile`.
- `run_s2_multidomain_autonomous_assurance`: `specialized_validation_profile`.
- `run_s2_multidomain_autonomous_assurance_foundation`: `specialized_validation_profile`.
- `run_s2_multidomain_autonomous_assurance_timing_validation`: `specialized_validation_profile`.
- `run_s2_multidomain_autonomous_assurance_final_timing_validation`: `specialized_validation_profile`.
- `run_s2_multidomain_selective_assurance`: `specialized_validation_profile`.
- `run_s2_multidomain_selective_assurance_foundation`: `specialized_validation_profile`.
- `run_s2_multidomain_selective_assurance_validation`: `specialized_validation_profile`.

Todos os perfis especializados são `NONCANONICAL`. Eles não redefinem o engine
do cenário nem convertem seus claims históricos em resultados do engine
canônico.

O ID de execução reserva um diretório exclusivo. Colisões falham; outputs não
são escolhidos por data de modificação.

## Contratos distintos

- **Requisitos do intent:** multicast habilitado, grupo/árvore, source e
  receivers lógicos, P99/limite de latência, banda mínima/máxima e prioridade.
- **Parâmetros do workload:** duração e intervalo de pacotes. Trinta segundos a
  50 ms produzem exatamente 600 oportunidades. Esse workload não é controlado
  implicitamente pelos limites de banda do intent.
- **Parâmetros de ambiente:** backend, modo, repetição e bindings congelados
  A:h1, B:h3, C:h4 e G1 para 239.1.1.1.

O artefato `recovery-observation.json` avalia somente
`PARTIAL_EVALUABLE_S2_RECOVERY_V1`. Banda, prioridade e conformidade integral
ficam explicitamente excluídas. Janelas vazias e tempo insuficiente para K
janelas completas resultam em `NOT_EVALUABLE`.

Os arquivos preexistentes `raw-events.jsonl`, `membership-events.jsonl`,
`readbacks.json`, `summary.json` e `rollback.json` são preservados. O campo
legado `metrics.recovery_ms` continua significando o maior atraso até a primeira
recepção após join/rejoin; ele não significa estabilidade por K bins. O campo
legado `metrics.stability` é apenas um flag grosseiro de entrega/probe; não é
estabilidade por K bins nem conformidade.

Esta capacidade é prospectiva e não recalcula, reinterpreta ou modifica os
resultados P3-R7, P4 ou P4-R1.
