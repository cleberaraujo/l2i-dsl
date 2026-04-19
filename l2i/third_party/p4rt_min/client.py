# -*- coding: utf-8 -*-
"""
Minimal P4Runtime client with robust mastership arbitration.

Why this exists:
- In P4Runtime, only the PRIMARY controller can perform Write().
- Many "Not primary" issues are caused by clients that:
  (i) don't open StreamChannel arbitration, or
  (ii) don't wait for arbitration response, or
  (iii) close the stream too early.

This module provides:
- connect() + open arbitration stream
- ensure_primary() blocking until arbitration response
- write() and read() helpers

Dependencies:
- p4runtime package provides protobuf stubs under p4.v1 and p4.config.v1
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator, Optional, Any, List, Tuple
import queue
import threading
import time

import grpc
from google.protobuf import text_format

try:
    from p4.v1 import p4runtime_pb2, p4runtime_pb2_grpc
    from p4.config.v1 import p4info_pb2
except Exception as e:  # pragma: no cover
    raise ImportError(
        "Failed to import P4Runtime protobuf stubs from the 'p4runtime' package.\n"
        "Expected modules: p4.v1.p4runtime_pb2, p4.v1.p4runtime_pb2_grpc, p4.config.v1.p4info_pb2\n"
        f"Original error: {e}\n"
        "\nFix (recommended):\n"
        "  /home/dev/net-dev/venv/bin/python -m pip install -U p4runtime\n"
    ) from e


@dataclass
class ArbitrationStatus:
    ok: bool
    is_primary: bool
    message: str
    last_update: Optional[p4runtime_pb2.StreamMessageResponse] = None


class P4RTClient:
    """
    A small P4Runtime client with explicit arbitration.
    """

    def __init__(self, address: str, device_id: int = 0, election_id: Tuple[int, int] = (0, 1), timeout_s: float = 5.0):
        self.address = address
        self.device_id = int(device_id)
        self.election_id = (int(election_id[0]), int(election_id[1]))
        self.timeout_s = float(timeout_s)

        self._channel: Optional[grpc.Channel] = None
        self._stub: Optional[p4runtime_pb2_grpc.P4RuntimeStub] = None

        self._req_q: "queue.Queue[p4runtime_pb2.StreamMessageRequest]" = queue.Queue()
        self._resp_q: "queue.Queue[p4runtime_pb2.StreamMessageResponse]" = queue.Queue()

        self._stream_thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()

        self._last_arbitration: Optional[p4runtime_pb2.StreamMessageResponse] = None

    # ---------- channel + stream ----------

    def connect(self) -> None:
        """
        Creates channel, stub, and starts StreamChannel with a background reader.
        """
        if self._channel is not None:
            return
        self._channel = grpc.insecure_channel(self.address)
        self._stub = p4runtime_pb2_grpc.P4RuntimeStub(self._channel)

        # Start stream in background
        self._stop_evt.clear()
        self._stream_thread = threading.Thread(target=self._run_stream, name="p4rt_stream", daemon=True)
        self._stream_thread.start()

        # Send initial arbitration request immediately
        self._send_arbitration_request()

    def close(self) -> None:
        """
        Stops StreamChannel and closes grpc channel.
        """
        self._stop_evt.set()
        try:
            # Put a sentinel request to unblock generator
            self._req_q.put_nowait(p4runtime_pb2.StreamMessageRequest())
        except Exception:
            pass

        if self._stream_thread:
            self._stream_thread.join(timeout=1.0)

        if self._channel:
            try:
                self._channel.close()
            except Exception:
                pass

        self._channel = None
        self._stub = None

    def _request_iter(self) -> Iterator[p4runtime_pb2.StreamMessageRequest]:
        while not self._stop_evt.is_set():
            try:
                req = self._req_q.get(timeout=0.1)
                yield req
            except queue.Empty:
                continue

    def _run_stream(self) -> None:
        assert self._stub is not None
        try:
            for resp in self._stub.StreamChannel(self._request_iter()):
                # Store last arbitration for diagnostics
                if resp.HasField("arbitration"):
                    self._last_arbitration = resp
                self._resp_q.put(resp)
        except Exception as e:
            # Push a synthetic response with error context
            r = p4runtime_pb2.StreamMessageResponse()
            # No official "error" field, keep context in last_update via message string
            # We'll report via ensure_primary()
            self._last_arbitration = None
            # Also put a marker to unblock waiters
            self._resp_q.put(r)

    def _send_arbitration_request(self) -> None:
        """
        Sends MasterArbitrationUpdate request.
        """
        eid_hi, eid_lo = self.election_id
        req = p4runtime_pb2.StreamMessageRequest()
        req.arbitration.device_id = self.device_id
        req.arbitration.election_id.high = eid_hi
        req.arbitration.election_id.low = eid_lo
        self._req_q.put(req)

    # ---------- arbitration ----------

    def ensure_primary(self, wait_s: float = 2.0) -> ArbitrationStatus:
        """
        Blocks waiting for arbitration response.

        Returns:
          ok: stream alive and we received arbitration update
          is_primary: controller has primary role
        """
        t0 = time.time()
        got_any = False
        last: Optional[p4runtime_pb2.StreamMessageResponse] = None

        while time.time() - t0 < wait_s:
            try:
                resp = self._resp_q.get(timeout=0.1)
                got_any = True
                last = resp
                if resp.HasField("arbitration"):
                    # P4Runtime returns a Status inside arbitration
                    st = resp.arbitration.status
                    code = int(st.code)
                    msg = st.message
                    # code == 0 means OK
                    ok = (code == 0)
                    # Some servers set a role / primary indication implicitly; in practice,
                    # if status OK, we treat as primary for bmv2 unless role arbitration differs.
                    # Many servers encode "not primary" in later Write() if arbitration wasn't granted.
                    # We'll still consider "ok" here as "arbitration accepted".
                    return ArbitrationStatus(ok=ok, is_primary=ok, message=f"arbitration_status_code={code} msg={msg}", last_update=resp)
            except queue.Empty:
                continue

        if not got_any:
            return ArbitrationStatus(ok=False, is_primary=False, message="no response on StreamChannel (stream not running?)", last_update=None)

        # got responses but no arbitration
        return ArbitrationStatus(ok=False, is_primary=False, message="no arbitration update received (unexpected)", last_update=last)

    # ---------- pipeline / write / read helpers ----------

    def set_pipeline(self, p4info_path: str, bmv2_json_path: str) -> Tuple[bool, str]:
        """
        SetForwardingPipelineConfig. Requires primary role.
        """
        assert self._stub is not None
        try:
            p4info = p4info_pb2.P4Info()
            txt = open(p4info_path, "r", encoding="utf-8").read()
            text_format.Merge(txt, p4info)

            req = p4runtime_pb2.SetForwardingPipelineConfigRequest()
            req.device_id = self.device_id
            req.action = p4runtime_pb2.SetForwardingPipelineConfigRequest.VERIFY_AND_COMMIT
            req.election_id.high = self.election_id[0]
            req.election_id.low = self.election_id[1]
            req.config.p4info.CopyFrom(p4info)
            req.config.p4_device_config = open(bmv2_json_path, "rb").read()

            self._stub.SetForwardingPipelineConfig(req, timeout=self.timeout_s)
            return True, "pipeline committed"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def write(self, updates: List[p4runtime_pb2.Update]) -> Tuple[bool, str]:
        """
        WriteRequest with proper election_id.
        """
        assert self._stub is not None
        try:
            req = p4runtime_pb2.WriteRequest()
            req.device_id = self.device_id
            req.election_id.high = self.election_id[0]
            req.election_id.low = self.election_id[1]
            req.updates.extend(updates)
            self._stub.Write(req, timeout=self.timeout_s)
            return True, "write ok"
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"

    def read(self, entities: List[p4runtime_pb2.Entity]) -> Tuple[bool, str, List[p4runtime_pb2.ReadResponse]]:
        """
        ReadRequest: returns list of responses.
        """
        assert self._stub is not None
        try:
            req = p4runtime_pb2.ReadRequest()
            req.device_id = self.device_id
            req.entities.extend(entities)
            resps = list(self._stub.Read(req, timeout=self.timeout_s))
            return True, "read ok", resps
        except Exception as e:
            return False, f"{type(e).__name__}: {e}", []
