"""
L2i – Layer 2 Intent Framework

Autor: Antônio Cleber de Sousa Araújo
Email: antoniocleber@ifba.edu.br

Este código faz parte do artefato experimental associado ao artigo:

"Uma Abordagem Declarativa e Modular para Adaptação Dinâmica da Camada de Enlace de Redes Heterogêneas"

SBRC 2026

Licença: Apache License 2.0

Backend local para materialização de QoS no domínio Linux por meio de
``tc``/HTB, ``netem`` e classificadores ``u32``.

O módulo separa explicitamente duas responsabilidades:

* ``setup_environment`` configura as condições administradas do testbed
  (capacidade do enlace, classe best-effort e atraso ambiental);
* ``apply_qos`` materializa a intenção (banda mínima/máxima, classe relativa
  e seletor de fluxo).

As funções de compatibilidade no fim do arquivo preservam os contratos dos
helpers atualmente incorporados aos cenários. Isso permite validar o backend
em paralelo antes de remover a lógica duplicada dos cenários.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import os
import re
import shlex
import subprocess
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


JsonDict = Dict[str, Any]
BackendReturn = Tuple[bool, JsonDict]

_DEFAULT_ROOT_MAJOR = "1"
_DEFAULT_PARENT_MINOR = "1"
_DEFAULT_BEST_EFFORT_MINOR = "30"
_DEFAULT_FILTER_PRIORITY = 1
_DEFAULT_DST_PORT = 5201

_PRIORITY_TO_MINOR = {
    "critical": "10",
    "high": "10",
    "prio10": "10",
    "10": "10",
    "medium": "20",
    "normal": "20",
    "prio20": "20",
    "20": "20",
    "low": "30",
    "best_effort": "30",
    "best-effort": "30",
    "prio30": "30",
    "30": "30",
}

_PROTOCOL_NUMBERS = {
    "icmp": 1,
    "tcp": 6,
    "udp": 17,
}


class LinuxTCError(RuntimeError):
    """Erro de validação ou execução produzido pelo backend Linux TC."""


class CommandExecutionError(LinuxTCError):
    """Falha em um comando externo, preservando o registro estruturado."""

    def __init__(self, record: JsonDict):
        self.record = record
        super().__init__(
            "command failed: "
            f"{record.get('command', '')} "
            f"(rc={record.get('returncode')})"
        )


@dataclass(frozen=True)
class ResolvedTarget:
    namespace: Optional[str]
    device: str
    dry_run: bool
    use_sudo: bool
    timeout_s: float
    raw: JsonDict


class CommandRunner:
    """Executa comandos de forma segura e registra plano e evidências."""

    def __init__(self, target: ResolvedTarget):
        self.target = target
        self.planned: List[str] = []
        self.executed: List[JsonDict] = []

    def _qualified(self, argv: Sequence[str]) -> List[str]:
        out: List[str] = []
        if self.target.use_sudo and os.geteuid() != 0:
            out.extend(["sudo", "-n"])
        if self.target.namespace:
            out.extend(["ip", "netns", "exec", self.target.namespace])
        out.extend(str(item) for item in argv)
        return out

    def run(
        self,
        argv: Sequence[str],
        *,
        check: bool = True,
        ignore_failure: bool = False,
        plan: bool = True,
    ) -> JsonDict:
        inner = [str(item) for item in argv]
        if plan:
            self.planned.append(shlex.join(inner))

        qualified = self._qualified(inner)
        record: JsonDict = {
            "argv": qualified,
            "command": shlex.join(qualified),
            "inner_argv": inner,
            "inner_command": shlex.join(inner),
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "ignored_failure": bool(ignore_failure),
            "simulated": bool(self.target.dry_run),
        }

        if self.target.dry_run:
            self.executed.append(record)
            return record

        try:
            completed = subprocess.run(
                qualified,
                text=True,
                capture_output=True,
                timeout=self.target.timeout_s,
                check=False,
            )
            record.update(
                returncode=int(completed.returncode),
                stdout=completed.stdout or "",
                stderr=completed.stderr or "",
            )
        except subprocess.TimeoutExpired as exc:
            record.update(
                returncode=124,
                stdout=_text_or_empty(exc.stdout),
                stderr=_text_or_empty(exc.stderr) or "command timed out",
            )
        except OSError as exc:
            record.update(
                returncode=127,
                stderr=str(exc),
            )

        hint = _execution_hint(record)
        if hint:
            record["hint"] = hint

        self.executed.append(record)

        failed = record["returncode"] != 0
        if failed and check and not ignore_failure:
            raise CommandExecutionError(record)
        return record

    def capture(self, argv: Sequence[str], *, check: bool = True) -> JsonDict:
        return self.run(argv, check=check, ignore_failure=not check, plan=False)


def _text_or_empty(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _execution_hint(record: Mapping[str, Any]) -> Optional[str]:
    text = f"{record.get('stdout', '')}\n{record.get('stderr', '')}".lower()
    if "operation not permitted" in text or "permission denied" in text:
        return (
            "tc/netns requires CAP_NET_ADMIN; execute the process with the "
            "required privileges or set target.sudo=true when passwordless "
            "sudo is intentionally configured"
        )
    if "cannot find device" in text or "no such device" in text:
        return "verify target.device and target.namespace"
    if "cannot open network namespace" in text or "network namespace" in text:
        return "verify that target.namespace exists and is accessible"
    return None


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return default


def _first(mapping: Mapping[str, Any], keys: Iterable[str], default: Any = None) -> Any:
    for key in keys:
        if key in mapping and mapping[key] is not None:
            return mapping[key]
    return default


def _domain_name(domain: Any) -> str:
    if isinstance(domain, Mapping):
        value = _first(domain, ("name", "domain", "id"), "A")
        return str(value)
    if domain is None:
        return "A"
    return str(domain)


def _merged_target(domain: Any, target: Optional[Mapping[str, Any]]) -> JsonDict:
    merged: JsonDict = {}
    if isinstance(domain, Mapping):
        nested = domain.get("target")
        if isinstance(nested, Mapping):
            merged.update(nested)
        for key in (
            "namespace",
            "ns",
            "device",
            "dev",
            "ifname",
            "interface",
            "dry_run",
            "sudo",
            "use_sudo",
            "timeout_s",
        ):
            if key in domain:
                merged[key] = domain[key]
    if isinstance(target, Mapping):
        merged.update(target)
    return merged


def _resolve_target(domain: Any, target: Optional[Mapping[str, Any]]) -> ResolvedTarget:
    merged = _merged_target(domain, target)
    device = _first(merged, ("device", "dev", "ifname", "interface"))
    if device is None or not str(device).strip():
        raise LinuxTCError(
            "missing Linux interface; provide target.device (or dev/ifname/interface)"
        )

    namespace = _first(merged, ("namespace", "ns"))
    namespace_text = str(namespace).strip() if namespace is not None else ""

    timeout_raw = _first(merged, ("timeout_s", "timeout"), 10.0)
    try:
        timeout_s = float(timeout_raw)
    except (TypeError, ValueError) as exc:
        raise LinuxTCError(f"invalid target.timeout_s: {timeout_raw!r}") from exc
    if timeout_s <= 0:
        raise LinuxTCError("target.timeout_s must be greater than zero")

    return ResolvedTarget(
        namespace=namespace_text or None,
        device=str(device).strip(),
        dry_run=_as_bool(merged.get("dry_run"), False),
        use_sudo=_as_bool(_first(merged, ("sudo", "use_sudo")), False),
        timeout_s=timeout_s,
        raw=merged,
    )


def _positive_number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise LinuxTCError(f"{label} must be numeric, got {value!r}") from exc
    if number <= 0:
        raise LinuxTCError(f"{label} must be greater than zero, got {number}")
    return number


def _nonnegative_number(value: Any, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise LinuxTCError(f"{label} must be numeric, got {value!r}") from exc
    if number < 0:
        raise LinuxTCError(f"{label} must not be negative, got {number}")
    return number


def _htb_priority(value: Any, label: str) -> int:
    """Validate one Linux HTB priority value.

    Linux HTB accepts priorities from 0 (highest) through 7 (lowest).  The
    helper is intentionally separate from ``_minor`` because a class identifier
    is only an identifier; it does not define scheduler precedence.
    """

    try:
        priority = int(value)
    except (TypeError, ValueError) as exc:
        raise LinuxTCError(f"{label} must be an integer, got {value!r}") from exc
    if not 0 <= priority <= 7:
        raise LinuxTCError(f"{label} must be between 0 and 7, got {priority}")
    return priority


def _number_token(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _mbit(value: float) -> str:
    return f"{_number_token(value)}mbit"


def _milliseconds(value: float) -> str:
    # Preserve the float representation used by the current scenario helpers
    # (for example, 1.0 -> "1.0ms") so dry-run plans remain byte-for-byte
    # comparable during the migration.
    return f"{value}ms"


def _minor(value: Any, *, default: str = "20") -> str:
    if value is None:
        return default
    text = str(value).strip().lower()
    if text.startswith("1:"):
        text = text.split(":", 1)[1]
    text = _PRIORITY_TO_MINOR.get(text, text.removeprefix("prio"))
    if not text.isdigit():
        raise LinuxTCError(f"invalid HTB class/priority value: {value!r}")
    number = int(text)
    if number <= 1 or number > 65534:
        raise LinuxTCError(f"HTB class minor must be between 2 and 65534: {number}")
    return str(number)


def _classid(minor: str, major: str = _DEFAULT_ROOT_MAJOR) -> str:
    return f"{major}:{minor}"


def _handle(minor: str) -> str:
    return f"{minor}:"


def _target_summary(target: ResolvedTarget) -> JsonDict:
    return {
        "namespace": target.namespace,
        "device": target.device,
        "dry_run": target.dry_run,
        "sudo": target.use_sudo,
        "timeout_s": target.timeout_s,
    }


def _command_payload(runner: CommandRunner) -> JsonDict:
    return {
        "ok": all(
            record.get("simulated") or record.get("returncode") == 0 or record.get("ignored_failure")
            for record in runner.executed
        ),
        "commands": runner.executed,
    }


def _readback(runner: CommandRunner, device: str) -> JsonDict:
    if runner.target.dry_run:
        return {
            "qdisc": "",
            "class": "",
            "filter": "",
            "simulated": True,
            "commands": [],
        }

    records = {
        "qdisc": runner.capture(["tc", "qdisc", "show", "dev", device], check=False),
        "class": runner.capture(["tc", "class", "show", "dev", device], check=False),
        "filter": runner.capture(
            ["tc", "filter", "show", "dev", device, "parent", "1:"],
            check=False,
        ),
    }
    return {
        "qdisc": records["qdisc"].get("stdout", "").strip(),
        "class": records["class"].get("stdout", "").strip(),
        "filter": records["filter"].get("stdout", "").strip(),
        "simulated": False,
        "commands": list(records.values()),
    }


def _class_readback_matches(
    classes: str,
    *,
    classid: str,
    rate_mbps: float,
    ceil_mbps: float,
    priority: Optional[int] = None,
) -> bool:
    """Match the effective HTB class values reported by ``tc``."""

    class_line = next(
        (
            line.lower()
            for line in classes.splitlines()
            if line.lower().startswith("class htb ")
            and classid.lower() in line.lower().split()
        ),
        "",
    )
    if not class_line:
        return False

    def rate_matches(label: str, expected_mbps: float) -> bool:
        match = re.search(
            rf"\b{label}\s+([0-9]+(?:\.[0-9]+)?)([kmgt]?bit)\b",
            class_line,
        )
        if not match:
            return False
        value = float(match.group(1))
        factor_to_mbps = {
            "bit": 0.000001,
            "kbit": 0.001,
            "mbit": 1.0,
            "gbit": 1000.0,
            "tbit": 1_000_000.0,
        }[match.group(2)]
        observed_mbps = value * factor_to_mbps
        return math.isclose(
            observed_mbps,
            float(expected_mbps),
            rel_tol=1e-6,
            abs_tol=1e-6,
        )

    checks = (
        rate_matches("rate", rate_mbps),
        rate_matches("ceil", ceil_mbps),
        (
            re.search(rf"\bprio\s+{priority}\b", class_line) is not None
            if priority is not None
            else True
        ),
    )
    return all(checks)


def _preflight_device(runner: CommandRunner, device: str) -> None:
    if runner.target.dry_run:
        return
    runner.capture(["ip", "link", "show", "dev", device], check=True)


def _planned(kind: str, runner: CommandRunner, **extra: Any) -> JsonDict:
    payload: JsonDict = {
        "kind": "linux-tc",
        "operation": kind,
        "cmds": list(runner.planned),
        "cmd_count": len(runner.planned),
    }
    payload.update(extra)
    return payload


def _failure(
    *,
    operation: str,
    domain: Any,
    target: Optional[ResolvedTarget],
    runner: Optional[CommandRunner],
    started: float,
    error: BaseException,
) -> BackendReturn:
    details: JsonDict = {
        "backend": "linux_tc_local",
        "operation": operation,
        "domain": _domain_name(domain),
        "applied": False,
        "simulated": bool(target.dry_run) if target else False,
        "timing_ms": round((time.monotonic() - started) * 1000.0, 3),
        "error": {
            "type": type(error).__name__,
            "message": str(error),
        },
    }
    if target is not None:
        details["target"] = _target_summary(target)
    if runner is not None:
        details["planned"] = _planned(operation, runner)
        details["exec"] = _command_payload(runner)
        if isinstance(error, CommandExecutionError):
            details["error"]["command"] = error.record
    return False, details


def inspect_state(
    domain: Any,
    target: Optional[Mapping[str, Any]] = None,
) -> JsonDict:
    """Coleta o estado ``qdisc/class/filter`` sem modificar a interface."""

    started = time.monotonic()
    resolved: Optional[ResolvedTarget] = None
    runner: Optional[CommandRunner] = None
    try:
        resolved = _resolve_target(domain, target)
        runner = CommandRunner(resolved)
        _preflight_device(runner, resolved.device)
        readback = _readback(runner, resolved.device)
        ok = resolved.dry_run or all(
            record.get("returncode") == 0
            for record in readback.get("commands", [])
        )
        return {
            "ok": bool(ok),
            "backend": "linux_tc_local",
            "operation": "inspect_state",
            "domain": _domain_name(domain),
            "target": _target_summary(resolved),
            "readback": readback,
            "exec": _command_payload(runner),
            "timing_ms": round((time.monotonic() - started) * 1000.0, 3),
        }
    except Exception as exc:
        _, details = _failure(
            operation="inspect_state",
            domain=domain,
            target=resolved,
            runner=runner,
            started=started,
            error=exc,
        )
        details["ok"] = False
        return details


def setup_environment(
    domain: Any,
    environment: Mapping[str, Any],
    target: Optional[Mapping[str, Any]] = None,
) -> BackendReturn:
    """Configura o envelope administrado do enlace no testbed Linux."""

    started = time.monotonic()
    resolved: Optional[ResolvedTarget] = None
    runner: Optional[CommandRunner] = None
    try:
        if not isinstance(environment, Mapping):
            raise LinuxTCError("environment must be a mapping")

        resolved = _resolve_target(domain, target)
        runner = CommandRunner(resolved)
        _preflight_device(runner, resolved.device)

        capacity_raw = _first(
            environment,
            ("bw_mbps", "capacity_mbps", "environment_mbps", "max_mbps"),
            _first(resolved.raw, ("environment_mbps", "capacity_mbps", "bw_mbps")),
        )
        if capacity_raw is None:
            raise LinuxTCError(
                "missing environment capacity; provide environment.bw_mbps "
                "(or capacity_mbps/environment_mbps)"
            )
        capacity = _positive_number(capacity_raw, "environment.bw_mbps")

        default_minor = _minor(
            _first(environment, ("default_class", "default_minor"),
                   _first(resolved.raw, ("default_class", "default_minor"), _DEFAULT_BEST_EFFORT_MINOR)),
            default=_DEFAULT_BEST_EFFORT_MINOR,
        )
        create_default = _as_bool(
            _first(environment, ("create_default_class",),
                   resolved.raw.get("create_default_class")),
            True,
        )

        delay_raw = _first(environment, ("delay_ms",), resolved.raw.get("delay_ms"))
        delay_ms = (
            _nonnegative_number(delay_raw, "environment.delay_ms")
            if delay_raw is not None
            else None
        )
        attach_netem = _as_bool(
            _first(environment, ("attach_netem",), resolved.raw.get("attach_netem")),
            delay_ms is not None,
        )

        # The current artifact and all scenario helpers use the canonical
        # HTB hierarchy 1: -> 1:1. Keep it fixed until a generalized hierarchy
        # is required and validated across all backends.
        root_major = _DEFAULT_ROOT_MAJOR
        parent_minor = _DEFAULT_PARENT_MINOR
        parent_class = _classid(parent_minor, root_major)
        default_class = _classid(default_minor, root_major)
        default_priority_raw = _first(
            environment,
            ("default_priority", "default_htb_priority"),
            _first(
                resolved.raw,
                ("default_priority", "default_htb_priority"),
            ),
        )
        default_priority = (
            _htb_priority(
                default_priority_raw,
                "environment.default_priority",
            )
            if default_priority_raw is not None
            else None
        )
        default_rate_raw = _first(
            environment,
            ("default_rate_mbps",),
            _first(resolved.raw, ("default_rate_mbps",), capacity),
        )
        default_ceil_raw = _first(
            environment,
            ("default_ceil_mbps",),
            _first(resolved.raw, ("default_ceil_mbps",), capacity),
        )
        default_rate = _positive_number(
            default_rate_raw,
            "environment.default_rate_mbps",
        )
        default_ceil = _positive_number(
            default_ceil_raw,
            "environment.default_ceil_mbps",
        )
        if default_rate > default_ceil:
            raise LinuxTCError(
                "environment.default_rate_mbps must not exceed "
                "default_ceil_mbps"
            )
        if default_ceil > capacity:
            raise LinuxTCError(
                "environment.default_ceil_mbps must not exceed link capacity"
            )

        runner.run(
            ["tc", "qdisc", "del", "dev", resolved.device, "root"],
            check=False,
            ignore_failure=True,
        )

        root_cmd = [
            "tc", "qdisc", "add", "dev", resolved.device,
            "root", "handle", f"{root_major}:", "htb",
        ]
        if create_default:
            root_cmd.extend(["default", default_minor])
        r2q = _first(environment, ("r2q",), resolved.raw.get("r2q"))
        if r2q is not None:
            r2q_value = int(_positive_number(r2q, "environment.r2q"))
            root_cmd.extend(["r2q", str(r2q_value)])
        runner.run(root_cmd)

        runner.run([
            "tc", "class", "add", "dev", resolved.device,
            "parent", f"{root_major}:", "classid", parent_class, "htb",
            "rate", _mbit(capacity), "ceil", _mbit(capacity),
        ])

        if create_default:
            default_class_command = [
                "tc", "class", "add", "dev", resolved.device,
                "parent", parent_class, "classid", default_class, "htb",
                "rate", _mbit(default_rate), "ceil", _mbit(default_ceil),
            ]
            if default_priority is not None:
                default_class_command.extend(["prio", str(default_priority)])
            runner.run(default_class_command)
            if attach_netem and delay_ms is not None:
                runner.run([
                    "tc", "qdisc", "add", "dev", resolved.device,
                    "parent", default_class, "handle", _handle(default_minor),
                    "netem", "delay", _milliseconds(delay_ms),
                ])

        readback = _readback(runner, resolved.device)
        if resolved.dry_run:
            checks = {
                "root_htb": True,
                "parent_class": True,
                "default_class": True,
                "default_netem": True,
            }
            ok = True
        else:
            qdisc = str(readback.get("qdisc", ""))
            classes = str(readback.get("class", ""))
            checks = {
                "root_htb": f"htb {root_major}:" in qdisc,
                "parent_class": _class_readback_matches(
                    classes,
                    classid=parent_class,
                    rate_mbps=capacity,
                    ceil_mbps=capacity,
                ),
                "default_class": (
                    _class_readback_matches(
                        classes,
                        classid=default_class,
                        rate_mbps=default_rate,
                        ceil_mbps=default_ceil,
                        priority=default_priority,
                    )
                    if create_default
                    else True
                ),
                "default_netem": (
                    f"netem {default_minor}:" in qdisc
                    if create_default and attach_netem and delay_ms is not None
                    else True
                ),
            }
            ok = all(checks.values())

        details: JsonDict = {
            "backend": "linux_tc_local",
            "operation": "setup_environment",
            "domain": _domain_name(domain),
            "target": _target_summary(resolved),
            "environment": {
                "capacity_mbps": capacity,
                "delay_ms": delay_ms,
                "create_default_class": create_default,
                "default_classid": default_class if create_default else None,
                "default_priority": default_priority,
                "default_rate_mbps": default_rate,
                "default_ceil_mbps": default_ceil,
            },
            "planned": _planned(
                "setup_environment",
                runner,
                root_handle=f"{root_major}:",
                parent_classid=parent_class,
                default_classid=default_class if create_default else None,
            ),
            "exec": _command_payload(runner),
            "readback": readback,
            "readback_checks": checks,
            "applied": bool(ok and not resolved.dry_run),
            "simulated": bool(resolved.dry_run),
            "timing_ms": round((time.monotonic() - started) * 1000.0, 3),
        }
        if not ok:
            details["error"] = {
                "type": "ReadbackMismatch",
                "message": "environment commands completed but readback did not match the plan",
            }
        return bool(ok), details
    except Exception as exc:
        return _failure(
            operation="setup_environment",
            domain=domain,
            target=resolved,
            runner=runner,
            started=started,
            error=exc,
        )


def _intent_bandwidth(intent: Mapping[str, Any]) -> Tuple[float, float]:
    nested = intent.get("bandwidth")
    bandwidth = nested if isinstance(nested, Mapping) else {}

    minimum_raw = _first(
        intent,
        ("min_mbps", "bandwidth_min_mbps"),
        _first(bandwidth, ("min_mbps",)),
    )
    maximum_raw = _first(
        intent,
        ("max_mbps", "bandwidth_max_mbps"),
        _first(bandwidth, ("max_mbps",)),
    )
    if minimum_raw is None:
        raise LinuxTCError("intent.min_mbps is required")
    minimum = _positive_number(minimum_raw, "intent.min_mbps")
    maximum = (
        _positive_number(maximum_raw, "intent.max_mbps")
        if maximum_raw is not None
        else minimum
    )
    if maximum < minimum:
        raise LinuxTCError(
            f"intent.max_mbps ({maximum}) must be >= intent.min_mbps ({minimum})"
        )
    return minimum, maximum


def _intent_minor(intent: Mapping[str, Any], target: ResolvedTarget) -> str:
    priority = _first(
        intent,
        ("class", "priority", "priority_level", "level"),
        _first(target.raw, ("class", "priority", "priority_level"), "prio20"),
    )
    if isinstance(priority, Mapping):
        priority = _first(priority, ("class", "level"), "prio20")
    return _minor(priority, default="20")


def _filter_command(
    *,
    device: str,
    classid: str,
    protocol: str,
    filter_priority: int,
    dst_port: Optional[int],
    dst_ip: Optional[str],
) -> List[str]:
    command = [
        "tc", "filter", "add", "dev", device,
        "protocol", "ip", "parent", "1:",
        "prio", str(filter_priority), "u32",
        "match", "ip", "protocol", str(_PROTOCOL_NUMBERS[protocol]), "0xff",
    ]
    if dst_ip:
        command.extend(["match", "ip", "dst", dst_ip])
    if protocol in {"tcp", "udp"}:
        if dst_port is None:
            raise LinuxTCError(f"target.dst_port is required for {protocol} classification")
        command.extend(["match", "ip", "dport", str(dst_port), "0xffff"])
    command.extend(["flowid", classid])
    return command


def _resolve_classifiers(
    *,
    intent: Mapping[str, Any],
    target: ResolvedTarget,
) -> List[JsonDict]:
    """Normalize one or more selectors that map traffic to an HTB class.

    Historical callers provide one selector through top-level target fields.
    Canonical scenarios may provide ``target.classifiers`` to classify all
    traffic used to observe one requirement.  S1 uses this capability to place
    both the sensitive TCP flow and its ICMP latency probes in the same class.
    """

    raw_classifiers = target.raw.get("classifiers")
    if raw_classifiers is None:
        raw_items: List[Mapping[str, Any]] = [target.raw]
    else:
        if (
            not isinstance(raw_classifiers, Sequence)
            or isinstance(raw_classifiers, (str, bytes))
            or not raw_classifiers
        ):
            raise LinuxTCError(
                "target.classifiers must be a non-empty sequence of mappings"
            )
        if not all(isinstance(item, Mapping) for item in raw_classifiers):
            raise LinuxTCError(
                "every target.classifiers item must be a mapping"
            )
        raw_items = list(raw_classifiers)

    classifiers: List[JsonDict] = []
    priorities: set[int] = set()
    for index, raw in enumerate(raw_items):
        protocol = str(
            _first(
                raw,
                ("filter_protocol", "protocol"),
                _first(intent, ("filter_protocol", "protocol"), "tcp"),
            )
        ).strip().lower()
        if protocol not in _PROTOCOL_NUMBERS:
            raise LinuxTCError(
                f"unsupported classifier protocol {protocol!r}; "
                "use tcp, udp or icmp"
            )

        default_priority = _DEFAULT_FILTER_PRIORITY + index
        priority_raw = _first(
            raw,
            ("filter_priority", "filter_prio"),
            default_priority,
        )
        try:
            filter_priority = int(priority_raw)
        except (TypeError, ValueError) as exc:
            raise LinuxTCError(
                f"invalid classifier priority: {priority_raw!r}"
            ) from exc
        if filter_priority <= 0:
            raise LinuxTCError(
                "classifier priority must be greater than zero"
            )
        if filter_priority in priorities:
            raise LinuxTCError(
                f"duplicate classifier priority: {filter_priority}"
            )
        priorities.add(filter_priority)

        port_raw = _first(
            raw,
            ("dst_port", "be_port", "port"),
            _first(intent, ("dst_port", "be_port", "port"), _DEFAULT_DST_PORT),
        )
        dst_port: Optional[int] = None
        if protocol in {"tcp", "udp"}:
            try:
                dst_port = int(port_raw)
            except (TypeError, ValueError) as exc:
                raise LinuxTCError(
                    f"invalid classifier destination port: {port_raw!r}"
                ) from exc
            if not 1 <= dst_port <= 65535:
                raise LinuxTCError(
                    f"classifier destination port out of range: {dst_port}"
                )

        dst_ip_raw = _first(raw, ("dst_ip",), intent.get("dst_ip"))
        dst_ip = str(dst_ip_raw).strip() if dst_ip_raw else None
        classifiers.append(
            {
                "protocol": protocol,
                "filter_priority": filter_priority,
                "dst_port": dst_port,
                "dst_ip": dst_ip,
            }
        )
    return classifiers


def apply_qos(
    domain: Any,
    intent: Mapping[str, Any],
    target: Optional[Mapping[str, Any]] = None,
) -> BackendReturn:
    """Materializa uma intenção de QoS em uma árvore HTB já existente."""

    started = time.monotonic()
    resolved: Optional[ResolvedTarget] = None
    runner: Optional[CommandRunner] = None
    try:
        if not isinstance(intent, Mapping):
            raise LinuxTCError("intent must be a mapping")

        resolved = _resolve_target(domain, target)
        runner = CommandRunner(resolved)
        _preflight_device(runner, resolved.device)

        minimum, maximum = _intent_bandwidth(intent)
        class_minor = _intent_minor(intent, resolved)
        class_id = _classid(class_minor)
        default_minor = _minor(
            _first(resolved.raw, ("default_class", "default_minor"), _DEFAULT_BEST_EFFORT_MINOR),
            default=_DEFAULT_BEST_EFFORT_MINOR,
        )

        classifiers = _resolve_classifiers(intent=intent, target=resolved)
        htb_priority_raw = _first(
            resolved.raw,
            ("htb_priority", "scheduler_priority"),
        )
        htb_priority = (
            _htb_priority(htb_priority_raw, "target.htb_priority")
            if htb_priority_raw is not None
            else None
        )

        delay_raw = _first(resolved.raw, ("priority_delay_ms", "delay_ms"))
        delay_ms = (
            _nonnegative_number(delay_raw, "target.priority_delay_ms")
            if delay_raw is not None
            else None
        )
        attach_netem = _as_bool(
            _first(resolved.raw, ("attach_priority_netem", "attach_netem")),
            delay_ms is not None,
        )
        idempotent_cleanup = _as_bool(resolved.raw.get("idempotent_cleanup"), True)
        remove_leaf_qdisc = _as_bool(resolved.raw.get("remove_leaf_qdisc"), True)

        if not resolved.dry_run:
            initial = _readback(runner, resolved.device)
            if "htb 1:" not in str(initial.get("qdisc", "")):
                raise LinuxTCError(
                    "HTB environment is missing; call setup_environment before apply_qos"
                )

        if idempotent_cleanup:
            for classifier in classifiers:
                runner.run([
                    "tc", "filter", "del", "dev", resolved.device,
                    "parent", "1:", "protocol", "ip", "prio",
                    str(classifier["filter_priority"]),
                ], check=False, ignore_failure=True)
            if class_minor != default_minor or _as_bool(resolved.raw.get("allow_default_class_replace"), False):
                if remove_leaf_qdisc:
                    runner.run([
                        "tc", "qdisc", "del", "dev", resolved.device,
                        "parent", class_id,
                    ], check=False, ignore_failure=True)
                runner.run([
                    "tc", "class", "del", "dev", resolved.device,
                    "classid", class_id,
                ], check=False, ignore_failure=True)

        if class_minor == default_minor:
            class_command = [
                "tc", "class", "replace", "dev", resolved.device,
                "parent", "1:1", "classid", class_id, "htb",
                "rate", _mbit(minimum), "ceil", _mbit(maximum),
            ]
        else:
            class_command = [
                "tc", "class", "add", "dev", resolved.device,
                "parent", "1:1", "classid", class_id, "htb",
                "rate", _mbit(minimum), "ceil", _mbit(maximum),
            ]
        if htb_priority is not None:
            class_command.extend(["prio", str(htb_priority)])
        runner.run(class_command)

        if attach_netem and delay_ms is not None:
            runner.run([
                "tc", "qdisc", "add", "dev", resolved.device,
                "parent", class_id, "handle", _handle(class_minor),
                "netem", "delay", _milliseconds(delay_ms),
            ], check=False, ignore_failure=True)

        for classifier in classifiers:
            runner.run(_filter_command(
                device=resolved.device,
                classid=class_id,
                protocol=str(classifier["protocol"]),
                filter_priority=int(classifier["filter_priority"]),
                dst_port=classifier.get("dst_port"),
                dst_ip=classifier.get("dst_ip"),
            ))

        readback = _readback(runner, resolved.device)
        if resolved.dry_run:
            checks = {
                "priority_class": True,
                "priority_filter": True,
                "priority_filters": True,
                "priority_netem": True,
            }
            ok = True
        else:
            qdisc = str(readback.get("qdisc", ""))
            classes = str(readback.get("class", ""))
            filters = str(readback.get("filter", ""))
            checks = {
                "priority_class": _class_readback_matches(
                    classes,
                    classid=class_id,
                    rate_mbps=minimum,
                    ceil_mbps=maximum,
                    priority=htb_priority,
                ),
                "priority_filter": class_id in filters,
                "priority_filters": filters.count(class_id) >= len(classifiers),
                "filter_priorities": all(
                    f"pref {classifier['filter_priority']} " in filters
                    for classifier in classifiers
                ),
                "priority_netem": (
                    f"netem {class_minor}:" in qdisc
                    if attach_netem and delay_ms is not None
                    else True
                ),
            }
            ok = all(checks.values())

        details: JsonDict = {
            "backend": "linux_tc_local",
            "operation": "apply_qos",
            "domain": _domain_name(domain),
            "target": _target_summary(resolved),
            "intent": {
                "class": f"prio{class_minor}",
                "classid": class_id,
                "min_mbps": minimum,
                "max_mbps": maximum,
                "htb_priority": htb_priority,
                "classifiers": classifiers,
            },
            "planned": _planned(
                "apply_qos",
                runner,
                classid=class_id,
                classifiers=classifiers,
            ),
            "exec": _command_payload(runner),
            "readback": readback,
            "readback_checks": checks,
            "applied": bool(ok and not resolved.dry_run),
            "overlay_applied": bool(ok and not resolved.dry_run),
            "simulated": bool(resolved.dry_run),
            "timing_ms": round((time.monotonic() - started) * 1000.0, 3),
        }
        if not ok:
            details["error"] = {
                "type": "ReadbackMismatch",
                "message": "QoS commands completed but readback did not match the plan",
            }
        return bool(ok), details
    except Exception as exc:
        return _failure(
            operation="apply_qos",
            domain=domain,
            target=resolved,
            runner=runner,
            started=started,
            error=exc,
        )


def remove_qos(
    domain: Any,
    intent: Mapping[str, Any],
    target: Optional[Mapping[str, Any]] = None,
) -> BackendReturn:
    """Remove a classe/filtro da intenção, preservando o ambiente HTB."""

    started = time.monotonic()
    resolved: Optional[ResolvedTarget] = None
    runner: Optional[CommandRunner] = None
    try:
        if not isinstance(intent, Mapping):
            raise LinuxTCError("intent must be a mapping")
        resolved = _resolve_target(domain, target)
        runner = CommandRunner(resolved)
        _preflight_device(runner, resolved.device)

        class_minor = _intent_minor(intent, resolved)
        class_id = _classid(class_minor)
        default_minor = _minor(
            _first(resolved.raw, ("default_class", "default_minor"), _DEFAULT_BEST_EFFORT_MINOR),
            default=_DEFAULT_BEST_EFFORT_MINOR,
        )
        filter_priority = int(
            _first(resolved.raw, ("filter_priority", "filter_prio"), _DEFAULT_FILTER_PRIORITY)
        )

        runner.run([
            "tc", "filter", "del", "dev", resolved.device,
            "parent", "1:", "protocol", "ip", "prio", str(filter_priority),
        ], check=False, ignore_failure=True)

        protected_default = (
            class_minor == default_minor
            and not _as_bool(resolved.raw.get("remove_default_class"), False)
        )
        if not protected_default:
            runner.run([
                "tc", "qdisc", "del", "dev", resolved.device,
                "parent", class_id,
            ], check=False, ignore_failure=True)
            runner.run([
                "tc", "class", "del", "dev", resolved.device,
                "classid", class_id,
            ], check=False, ignore_failure=True)

        readback = _readback(runner, resolved.device)
        if resolved.dry_run:
            checks = {"filter_removed": True, "class_removed": True}
            ok = True
        else:
            classes = str(readback.get("class", ""))
            filters = str(readback.get("filter", ""))
            checks = {
                "filter_removed": class_id not in filters,
                "class_removed": (class_id not in classes) if not protected_default else True,
            }
            ok = all(checks.values())

        details: JsonDict = {
            "backend": "linux_tc_local",
            "operation": "remove_qos",
            "domain": _domain_name(domain),
            "target": _target_summary(resolved),
            "planned": _planned("remove_qos", runner, classid=class_id),
            "exec": _command_payload(runner),
            "readback": readback,
            "readback_checks": checks,
            "default_class_preserved": protected_default,
            "applied": bool(ok and not resolved.dry_run),
            "simulated": bool(resolved.dry_run),
            "timing_ms": round((time.monotonic() - started) * 1000.0, 3),
        }
        return bool(ok), details
    except Exception as exc:
        return _failure(
            operation="remove_qos",
            domain=domain,
            target=resolved,
            runner=runner,
            started=started,
            error=exc,
        )


def cleanup_environment(
    domain: Any,
    target: Optional[Mapping[str, Any]] = None,
) -> BackendReturn:
    """Remove idempotentemente a raiz HTB e todo o estado abaixo dela."""

    started = time.monotonic()
    resolved: Optional[ResolvedTarget] = None
    runner: Optional[CommandRunner] = None
    try:
        resolved = _resolve_target(domain, target)
        runner = CommandRunner(resolved)
        _preflight_device(runner, resolved.device)
        runner.run([
            "tc", "qdisc", "del", "dev", resolved.device, "root",
        ], check=False, ignore_failure=True)
        readback = _readback(runner, resolved.device)
        ok = resolved.dry_run or "htb 1:" not in str(readback.get("qdisc", ""))
        details: JsonDict = {
            "backend": "linux_tc_local",
            "operation": "cleanup_environment",
            "domain": _domain_name(domain),
            "target": _target_summary(resolved),
            "planned": _planned("cleanup_environment", runner),
            "exec": _command_payload(runner),
            "readback": readback,
            "readback_checks": {"root_htb_removed": bool(ok)},
            "applied": bool(ok and not resolved.dry_run),
            "simulated": bool(resolved.dry_run),
            "timing_ms": round((time.monotonic() - started) * 1000.0, 3),
        }
        return bool(ok), details
    except Exception as exc:
        return _failure(
            operation="cleanup_environment",
            domain=domain,
            target=resolved,
            runner=runner,
            started=started,
            error=exc,
        )


# ---------------------------------------------------------------------------
# Compatibility helpers for the current scenario-local contracts.
# They make it possible to replace local helpers incrementally after regression
# tests establish equivalent plans and readback.
# ---------------------------------------------------------------------------


def _raise_compat(details: Mapping[str, Any]) -> None:
    error = details.get("error")
    if isinstance(error, Mapping):
        message = str(error.get("message", error))
    else:
        message = str(error or "Linux TC backend failed")
    raise RuntimeError(message)


def tc_apply_h1(
    min_mbps: float,
    max_mbps: float,
    ns: str,
    dev: str,
    prio_cls: str,
    delay_ms: float,
    be_port: int,
    overlay: bool,
    dry_run: bool = False,
) -> JsonDict:
    """Compatibilidade com ``scenarios.multicast_s2.tc_apply_h1``."""

    started = time.monotonic()
    target: JsonDict = {
        "namespace": ns,
        "device": dev,
        "dry_run": dry_run,
        "delay_ms": delay_ms,
        "default_class": "30",
        "class": f"prio{prio_cls}",
        "filter_protocol": "tcp",
        "dst_port": be_port,
        "filter_priority": 1,
        "attach_priority_netem": True,
        "idempotent_cleanup": False,
    }
    env_ok, env = setup_environment(
        {"name": "A"},
        {
            "bw_mbps": max_mbps,
            "delay_ms": delay_ms,
            "create_default_class": True,
        },
        target,
    )
    if not env_ok:
        _raise_compat(env)

    commands = list((env.get("planned") or {}).get("cmds", []))
    overlay_details: Optional[JsonDict] = None
    if overlay:
        qos_ok, qos = apply_qos(
            {"name": "A"},
            {
                "class": f"prio{prio_cls}",
                "min_mbps": min_mbps,
                "max_mbps": max_mbps,
            },
            target,
        )
        if not qos_ok:
            _raise_compat(qos)
        overlay_details = qos
        commands.extend((qos.get("planned") or {}).get("cmds", []))
        readback = qos.get("readback", {})
    else:
        readback = env.get("readback", {})

    return {
        "commands": commands,
        "readback": {
            "qdisc": str(readback.get("qdisc", "")),
            "class": str(readback.get("class", "")),
            "filter": str(readback.get("filter", "")),
        },
        "timing_ms": int(round((time.monotonic() - started) * 1000.0)),
        "overlay_applied": bool(
            overlay and overlay_details and overlay_details.get("applied")
        ),
        "simulated": bool(dry_run),
        "backend_details": {
            "environment": env,
            "qos": overlay_details,
        },
    }


def tc_env_setup(
    ns: str,
    dev: str,
    bw_mbps: float,
    dry_run: bool = False,
) -> JsonDict:
    """Compatibilidade com os helpers ``tc_env_setup`` dos cenários S2."""

    ok, details = setup_environment(
        {"name": "A"},
        {
            "bw_mbps": bw_mbps,
            "create_default_class": False,
        },
        {
            "namespace": ns,
            "device": dev,
            "dry_run": dry_run,
        },
    )
    if not ok:
        _raise_compat(details)
    readback = details.get("readback", {})
    return {
        "commands": list((details.get("planned") or {}).get("cmds", [])),
        "readback": {
            "qdisc": str(readback.get("qdisc", "")),
            "class": str(readback.get("class", "")),
            "filter": str(readback.get("filter", "")),
        },
        "backend_details": details,
    }


def tc_apply_adaptation(
    ns: str,
    dev: str,
    intent: Mapping[str, Any],
    dry_run: bool = False,
) -> JsonDict:
    """Compatibilidade com os helpers ``tc_apply_adaptation`` dos cenários S2."""

    normalized = dict(intent)
    # The current recovery helpers intentionally use class 1:10 and round
    # bandwidth values down to integer Mbps. Preserve that behavior until
    # the scenarios are migrated to the generic apply_qos contract.
    minimum = max(1, int(float(normalized.get("min_mbps", 1))))
    maximum = max(minimum, int(float(normalized.get("max_mbps", minimum))))
    normalized.update({"class": "prio10", "min_mbps": minimum, "max_mbps": maximum})
    ok, details = apply_qos(
        {"name": "A"},
        normalized,
        {
            "namespace": ns,
            "device": dev,
            "dry_run": dry_run,
            "filter_protocol": "icmp",
            "filter_priority": 2,
            "attach_priority_netem": False,
            "idempotent_cleanup": True,
            "remove_leaf_qdisc": False,
        },
    )
    if not ok:
        _raise_compat(details)
    readback = details.get("readback", {})
    return {
        "commands": list((details.get("planned") or {}).get("cmds", [])),
        "readback": {
            "qdisc": str(readback.get("qdisc", "")),
            "class": str(readback.get("class", "")),
            "filter": str(readback.get("filter", "")),
        },
        "backend_details": details,
    }


__all__ = [
    "LinuxTCError",
    "apply_qos",
    "cleanup_environment",
    "inspect_state",
    "remove_qos",
    "setup_environment",
    "tc_apply_adaptation",
    "tc_apply_h1",
    "tc_env_setup",
]
