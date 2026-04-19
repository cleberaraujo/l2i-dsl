"""
L2i – Layer 2 Intent Framework

Autor: Antônio Cleber de Sousa Araújo
Email: antoniocleber@ifba.edu.br

Este código faz parte do artefato experimental associado ao artigo:

"Uma Abordagem Declarativa e Modular para Adaptação Dinâmica da Camada de Enlace de Redes Heterogêneas"

SBRC 2026

Licença: Apache License 2.0
"""

# l2i/schemas.py
from typing import Any, Dict

# --- Schema para especificações L2I ---
L2I_SPEC_SCHEMA: Dict[str, Any] = {
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://example.org/schemas/l2i-v0.json",
  "title": "L2I-v0 Spec",
  "type": "object",
  "additionalProperties": False,
  "required": ["l2i_version", "tenant", "scope", "flow", "requirements"],

  "properties": {
    "l2i_version": { "type": "string", "const": "0.1" },
    "tenant": { "type": "string", "minLength": 1, "pattern": "^[A-Za-z0-9._:-]{1,128}$" },
    "scope":  { "type": "string", "minLength": 1, "pattern": "^[A-Za-z0-9._:-]{1,128}$" },
    "flow": {
      "type": "object", "additionalProperties": False, "required": ["id"],
      "properties": { "id": { "type": "string", "minLength": 1, "pattern": "^[A-Za-z0-9._:-]{1,128}$" } }
    },

    "requirements": {
      "type": "object", "additionalProperties": False, "minProperties": 1,
      "properties": {
        "latency": {
          "type": "object", "additionalProperties": False, "required": ["max_ms"],
          "properties": {
            "max_ms": { "type": "integer", "minimum": 1, "maximum": 10000 },
            "percentile": { "type": "string", "enum": ["P50","P95","P99"], "default": "P99" }
          }
        },
        "bandwidth": {
          "type": "object", "additionalProperties": False, "required": ["min_mbps"],
          "properties": {
            "min_mbps":  { "type": "number", "exclusiveMinimum": 0, "maximum": 1_000_000 },
            "max_mbps":  { "type": "number", "exclusiveMinimum": 0, "maximum": 1_000_000 },
            "burst_mbps":{ "type": "number", "exclusiveMinimum": 0, "maximum": 1_000_000 }
          }
        },
        "priority": {
          "type": "object", "additionalProperties": False, "required": ["level"],
          "properties": { "level": { "type": "string", "enum": ["critical","high","medium","low"] } }
        },
        "multicast": {
          "type": "object", "additionalProperties": False, "required": ["enabled"],
          "properties": {
            "enabled": { "type": "boolean" },
            "group_id": { "type": "string", "minLength": 1, "pattern": "^[A-Za-z0-9._:-]{1,128}$" }
          }
        }
      }
    },

    # Metadata: manter validação útil, mas permitir campos extras como audience / issued_by
    "metadata": {
      "type": "object",
      # Permitimos propriedades adicionais aqui (audience, issued_by, etc.)
      "additionalProperties": True,
      "properties": {
        "created_at": { "type": "string", "format": "date-time" },
        "labels": { "type": "array", "items": { "type": "string", "minLength": 1 }, "maxItems": 32 },
        # Campos extras que costumam aparecer nos seus specs — validados de forma simples
        "audience": {
          "anyOf": [
            { "type": "string", "minLength": 1 },
            { "type": "array", "items": { "type": "string", "minLength": 1 } }
          ]
        },
        "issued_by": {
          "anyOf": [
            { "type": "string", "minLength": 1 },
            { "type": "object", "additionalProperties": True }
          ]
        }
      }
    },

    "target_profile": { "type": "string", "minLength": 1, "pattern": "^[A-Za-z0-9._:-]{1,128}$" }
  }
}


# --- Schema para perfis de capacidades (hardware/software) ---
CAPABILITY_SCHEMA: Dict[str, Any] = {
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://example.org/schemas/l2i-capability-v0.json",
  "title": "L2I Capability Profile v0",
  "type": "object", "additionalProperties": False,
  "required": ["profile_id","queues","meters","multicast","atomic_commit","telemetry"],
  "properties": {
    "profile_id": { "type": "string", "minLength": 1 },
    "description": { "type": "string" },
    "queues": {
      "type": "object", "additionalProperties": False, "required": ["max_queues","modes"],
      "properties": {
        "max_queues": { "type": "integer", "minimum": 1 },
        "modes": {
          "type": "object", "additionalProperties": False, "required": ["strict","wfq"],
          "properties": {
            "strict": { "type": "boolean" },
            "wfq": {
              "type": "object", "additionalProperties": False,
              "required": ["supported","weights_min","weights_max"],
              "properties": {
                "supported": { "type": "boolean" },
                "weights_min": { "type": "number", "exclusiveMinimum": 0 },
                "weights_max": { "type": "number", "exclusiveMinimum": 0 }
              }
            }
          }
        }
      }
    },
    "meters": {
      "type": "object", "additionalProperties": False, "required": ["supported","types"],
      "properties": {
        "supported": { "type": "boolean" },
        "types": { "type": "array", "items": { "type": "string", "enum": ["tbf","trtcm"] }, "maxItems": 4 },
        "min_rate_mbps": { "type": "number", "exclusiveMinimum": 0 },
        "max_rate_mbps": { "type": "number", "exclusiveMinimum": 0 }
      }
    },
    "multicast": {
      "type": "object", "additionalProperties": False, "required": ["mode"],
      "properties": {
        "mode": { "type": "string", "enum": ["none","vlan_flood","l2mc_static"] },
        "max_groups": { "type": "integer", "minimum": 0 },
        "max_replications_per_group": { "type": "integer", "minimum": 0 }
      }
    },
    "ports": {
      "type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["name","speed_mbps"],
        "properties": { "name": { "type": "string","minLength": 1 }, "speed_mbps": { "type": "integer","minimum": 10 } }
      }
    },
    "atomic_commit": { "type": "boolean" },
    "telemetry": {
      "type": "object","additionalProperties": False,
      "required": ["rtt_percentile","throughput_sustained","queue_occupancy","delivery_ratio"],
      "properties": {
        "rtt_percentile": { "type": "boolean" },
        "throughput_sustained": { "type": "boolean" },
        "queue_occupancy": { "type": "boolean" },
        "delivery_ratio": { "type": "boolean" }
      }
    }
  }
}
