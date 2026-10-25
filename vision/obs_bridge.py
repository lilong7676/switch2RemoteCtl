#!/usr/bin/env python3
"""Minimal obs-websocket v5 client used as a camera bridge.

Why this exists: the DSH app that hosts the agent is hardened-runtime signed
without `com.apple.security.device.camera`, so TCC denies camera access to every
process it spawns. OBS already holds the capture card with camera permission, so
we ask OBS for frames over localhost obs-websocket instead.

This module only reads pixels. It never sends controller input and never
changes OBS output state.
"""

import asyncio
import base64
import hashlib
import json
import os
import uuid
from pathlib import Path

import cv2
import numpy as np
from websockets.asyncio.client import connect

DEFAULT_URL = "ws://127.0.0.1:4455"

BASE_DIR = Path(__file__).resolve().parent
LOCAL_CONFIG = BASE_DIR / "local_config.json"
PASSWORD_ENV = "OBS_WS_PASSWORD"


def default_password():
    """Resolve the OBS WebSocket password without committing it.

    Order: ``OBS_WS_PASSWORD`` env var, then ``vision/local_config.json``
    (gitignored), then empty string for OBS instances without auth.
    """
    env = os.environ.get(PASSWORD_ENV)
    if env:
        return env
    try:
        data = json.loads(LOCAL_CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(data.get("obs_password") or "")


class ObsError(RuntimeError):
    pass


class ObsBridge:
    def __init__(self, url=DEFAULT_URL, password=None):
        self.url = url
        self.password = password or ""
        self.ws = None

    # ---------- lifecycle ----------

    async def connect(self, timeout=5.0):
        self.ws = await connect(self.url, open_timeout=timeout, max_size=64 * 1024 * 1024)
        hello = json.loads(await asyncio.wait_for(self.ws.recv(), timeout))
        if hello.get("op") != 0:
            raise ObsError(f"expected Hello, got {hello}")

        payload = {"rpcVersion": 1}
        auth = hello.get("d", {}).get("authentication")
        if auth:
            if not self.password:
                raise ObsError("server requires a password but none was given")
            secret = base64.b64encode(
                hashlib.sha256((self.password + auth["salt"]).encode()).digest()
            ).decode()
            payload["authentication"] = base64.b64encode(
                hashlib.sha256((secret + auth["challenge"]).encode()).digest()
            ).decode()

        await self.ws.send(json.dumps({"op": 1, "d": payload}))
        identified = json.loads(await asyncio.wait_for(self.ws.recv(), timeout))
        if identified.get("op") != 2:
            raise ObsError(f"identify failed: {identified}")
        return self

    async def close(self):
        if self.ws is not None:
            await self.ws.close()
            self.ws = None

    # ---------- requests ----------

    async def request(self, request_type, data=None, timeout=10.0):
        if self.ws is None:
            raise ObsError("not connected")
        request_id = uuid.uuid4().hex
        payload = {
            "op": 6,
            "d": {
                "requestType": request_type,
                "requestId": request_id,
                "requestData": data or {},
            },
        }
        await self.ws.send(json.dumps(payload))
        while True:
            message = json.loads(await asyncio.wait_for(self.ws.recv(), timeout))
            if message.get("op") != 7 or message.get("d", {}).get("requestId") != request_id:
                continue
            status = message["d"].get("requestStatus", {})
            if not status.get("result"):
                raise ObsError(f"{request_type}: {status}")
            return message["d"].get("responseData", {})

    # ---------- helpers ----------

    async def find_capture_input(self):
        """Return the name of the first avcapture input OBS knows about."""
        data = await self.request("GetInputList")
        inputs = data.get("inputs", [])
        for item in inputs:
            kind = item.get("unversionedInputKind") or item.get("inputKind") or ""
            if "avcapture" in kind or "av_capture" in kind:
                return item["inputName"]
        if inputs:
            return inputs[0]["inputName"]
        raise ObsError("OBS has no input sources")

    async def screenshot(self, source_name, width=1920, fmt="jpg", quality=85):
        """Return a BGR frame from one OBS source."""
        data = await self.request(
            "GetSourceScreenshot",
            {
                "sourceName": source_name,
                "imageFormat": fmt,
                "imageWidth": int(width),
                "imageCompressionQuality": int(quality),
            },
        )
        raw = data.get("imageData") or ""
        if raw.startswith("data:"):
            raw = raw.split(",", 1)[1]
        buffer = np.frombuffer(base64.b64decode(raw), np.uint8)
        frame = cv2.imdecode(buffer, cv2.IMREAD_COLOR)
        if frame is None:
            raise ObsError(f"could not decode screenshot of {source_name!r}")
        return frame
