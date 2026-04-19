#!/usr/bin/env python3
"""
Empurra o pipeline P4 (P4Runtime) para o simple_switch_grpc rodando em 127.0.0.1:PORTA.

Compatível com p4runtime-shell instalado via pip no venv, em que:
    FwdPipeConfig é chamado com argumentos POSICIONAIS:
        FwdPipeConfig(<p4info_path>, <bmv2_json_path>)
"""

import argparse
import json
import os
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(
        description="Carrega pipeline P4 no simple_switch_grpc via p4runtime-shell"
    )
    parser.add_argument(
        "--addr",
        default="127.0.0.1:9559",
        help="endereço gRPC do switch (default: 127.0.0.1:9559)",
    )
    parser.add_argument(
        "--device-id",
        type=int,
        default=0,
        help="device_id P4Runtime (default: 0)",
    )
    parser.add_argument(
        "--p4info",
        default="/tmp/l2i_minimal/l2i_minimal.p4info.txtpb",
        help="caminho do arquivo p4info (default: /tmp/l2i_minimal/l2i_minimal.p4info.txtpb)",
    )
    parser.add_argument(
        "--bmv2-json",
        dest="bmv2_json",
        default="/tmp/l2i_minimal/l2i_minimal.json",
        help="caminho do JSON bmv2 (default: /tmp/l2i_minimal/l2i_minimal.json)",
    )
    args = parser.parse_args()

    p4info_path = Path(args.p4info)
    bmv2_json_path = Path(args.bmv2_json)

    result = {
        "address": args.addr,
        "device_id": args.device_id,
        "connected": False,
        "pipeline_loaded": False,
        "p4info": str(p4info_path),
        "bmv2_json": str(bmv2_json_path),
        "error": None,
    }

    # 1) Verifica arquivos
    if not p4info_path.is_file():
        result["error"] = f"Arquivo p4info inexistente: {p4info_path}"
        print(json.dumps(result, indent=2))
        sys.exit(1)

    if not bmv2_json_path.is_file():
        result["error"] = f"Arquivo bmv2_json inexistente: {bmv2_json_path}"
        print(json.dumps(result, indent=2))
        sys.exit(1)

    # 2) Importa p4runtime-shell a partir do venv
    try:
        import p4runtime_sh.shell as sh
    except ImportError as e:
        result["error"] = f"módulo p4runtime_sh não encontrado no venv: {e}"
        print(json.dumps(result, indent=2))
        sys.exit(1)

    # 3) Usa FwdPipeConfig POSICIONAL (sem p4info_path=..., bmv2_json_path=...)
    try:
        FwdPipeConfig = sh.FwdPipeConfig
    except AttributeError as e:
        result["error"] = f"FwdPipeConfig não encontrado em p4runtime_sh.shell: {e}"
        print(json.dumps(result, indent=2))
        sys.exit(1)

    try:
        cfg = FwdPipeConfig(str(p4info_path), str(bmv2_json_path))
    except TypeError as e:
        # Aqui capturamos exatamente casos como:
        #   FwdPipeConfig.__new__() got an unexpected keyword argument ...
        result["error"] = f"Falha ao instanciar FwdPipeConfig (verifique versão da lib): {e}"
        print(json.dumps(result, indent=2))
        sys.exit(1)

    # 4) Faz setup → se não der exceção, consideramos conectado+pipeline carregado
    try:
        sh.setup(
            device_id=args.device_id,
            grpc_addr=args.addr,
            election_id=(0, 1),
            config=cfg,
        )
        result["connected"] = True
        result["pipeline_loaded"] = True
    except Exception as e:  # noqa: BLE001
        result["error"] = f"Falha no setup P4Runtime: {e}"
        print(json.dumps(result, indent=2))
        sys.exit(1)
    finally:
        try:
            sh.teardown()
        except Exception:
            # se teardown falhar, não é crítico para nosso resumo
            pass

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
