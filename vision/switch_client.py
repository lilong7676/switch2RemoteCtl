#!/usr/bin/env python3
"""Minimal Socket.IO client for the nuxbt webapp (Mode A).

Mode A: the browser owns the controller. This client only reads state and
sends macros. Controller creation and shutdown are intentionally NOT
implemented here so a Mac-side script cannot remove the browser's controller.

The Pi webapp only supports Engine.IO polling, not WebSocket.
"""

import copy
import json
import threading
import time

import socketio


class SwitchClient:
    def __init__(self, url: str):
        self.url = url
        self.sio = socketio.Client(
            reconnection=False,
            logger=False,
            engineio_logger=False,
        )
        self.state = {}
        self._state_event = threading.Event()
        self._index = None
        self.last_error = None

        self.sio.on("state", self._on_state)
        self.sio.on("error", self._on_error)

    # ---------- lifecycle ----------

    def connect(self, wait_timeout=10):
        self.sio.connect(
            self.url,
            transports=["polling"],
            wait_timeout=wait_timeout,
        )
        return self

    def close(self):
        if self.sio.connected:
            self.sio.disconnect()

    # ---------- event handlers ----------

    def _on_state(self, data):
        self.state = data or {}
        self._state_event.set()

    def _on_error(self, message):
        self.last_error = message
        print(f"[webapp error] {message}")

    # ---------- state ----------

    def refresh_state(self, timeout=2.0):
        """Ask the webapp for state and wait for the reply."""
        self._state_event.clear()
        self.sio.emit("state")
        if not self._state_event.wait(timeout):
            raise TimeoutError("no state event from webapp")
        return self.state

    def controller_states(self):
        """Return {index: state} for every controller in the last state."""
        return {
            int(key): value.get("state")
            for key, value in self.state.items()
            if isinstance(value, dict)
        }

    def connected_index(self):
        # Cache the index: QTE loops must not do a state round-trip per prompt.
        if self._index is not None:
            return self._index
        state = self.refresh_state()
        for key, value in state.items():
            if isinstance(value, dict) and value.get("state") == "connected":
                self._index = int(key)
                return self._index
        raise RuntimeError(f"no connected controller: {state}")

    def wait_connected(self, index=None, timeout=5.0):
        """Poll until a controller is connected. Kept short by default."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.refresh_state()
            if index is None:
                for key, value in state.items():
                    if isinstance(value, dict) and value.get("state") == "connected":
                        self._index = int(key)
                        return self._index
            else:
                info = state.get(str(index), {})
                if info.get("state") == "connected":
                    self._index = int(index)
                    return self._index
                if info.get("state") == "crashed":
                    raise RuntimeError(info.get("errors"))
            time.sleep(0.1)
        raise TimeoutError("controller did not reach connected")

    # ---------- macros ----------

    def macro_ack(self, text, index=None, ack_timeout=5.0):
        """Send a macro and return (macro_id, ack_seconds).

        This measures only the Socket.IO acknowledgement, not macro completion.
        """
        index = index if index is not None else self.connected_index()
        started = time.perf_counter()
        last_error = None
        for attempt in range(3):
            try:
                if not self.sio.connected:
                    self.connect()
                macro_id = self.sio.call(
                    "macro",
                    json.dumps([int(index), text]),
                    timeout=ack_timeout,
                )
                return macro_id, time.perf_counter() - started
            except Exception as exc:  # noqa: BLE001 - transient socket hiccups
                last_error = exc
                time.sleep(0.15 * (attempt + 1))
        raise TimeoutError(f"macro send failed after retries: {last_error}")

    def macro(self, text, index=None, timeout=30.0):
        """Run a macro and wait for it to finish."""
        macro_id, _ = self.macro_ack(text, index=index)
        if not self.wait_macro(macro_id, index=index, timeout=timeout):
            raise TimeoutError(f"macro did not finish: {macro_id}")
        return macro_id

    def macro_async(self, text, index=None):
        """Fire-and-forget macro, useful for fast QTE prompts."""
        index = index if index is not None else self.connected_index()
        self.sio.emit("macro", json.dumps([int(index), text]))

    def wait_macro(self, macro_id, index=None, timeout=30.0):
        index = index if index is not None else self.connected_index()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.refresh_state()
            info = state.get(str(index), {})
            if macro_id in info.get("finished_macros", []):
                return True
            time.sleep(0.03)
        return False

    def stop_all(self):
        self.sio.emit("stop_all_macros")

    # ---------- direct input (optional) ----------

    def idle_packet(self, index=None):
        """Build a full idle packet.

        state["0"]["direct_input"] may still contain the last direct input,
        so explicitly clear every button/stick before sending.
        """
        index = index if index is not None else self.connected_index()
        state = self.refresh_state()
        packet = copy.deepcopy(state[str(index)]["direct_input"])

        for key, value in packet.items():
            if isinstance(value, bool):
                packet[key] = False

        for stick_name in ("L_STICK", "R_STICK"):
            stick = packet[stick_name]
            stick["PRESSED"] = False
            stick["X_VALUE"] = 0
            stick["Y_VALUE"] = 0
            for key in list(stick):
                if key.startswith("LS_") or key.startswith("RS_"):
                    stick[key] = False

        return packet

    def send_input(self, packet, index=None):
        """Send one 120 Hz protocol cycle.

        For a held input, use a macro or send repeatedly at ~120 Hz.
        """
        index = index if index is not None else self.connected_index()
        self.sio.emit("input", json.dumps([int(index), packet]))
