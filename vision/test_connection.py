#!/usr/bin/env python3
"""One-shot Socket.IO connectivity check for the nuxbt webapp.

Mode A only: never creates a controller and never calls shutdown.
The only macro sent is the harmless "0.1s" wait macro, and only if a
controller is already connected.
"""

import json
import os
import sys
import traceback

from switch_client import SwitchClient


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("PI_URL")
    if not url:
        print("usage: python vision/test_connection.py http://<PI_IP>:8000")
        print("   or: PI_URL=http://<PI_IP>:8000 python vision/test_connection.py")
        return 2
    print(f"URL: {url}")
    print("transport: polling only")

    client = SwitchClient(url)
    try:
        try:
            client.connect(wait_timeout=5)
        except Exception:
            print("SOCKET_CONNECT_FAILED")
            traceback.print_exc()
            return 2

        print("socket connected")

        try:
            state = client.refresh_state(timeout=5.0)
        except Exception:
            print("STATE_READ_FAILED")
            traceback.print_exc()
            return 2

        print("state:")
        print(json.dumps(state, indent=2, ensure_ascii=False, sort_keys=True))

        connected = [
            int(key)
            for key, value in state.items()
            if isinstance(value, dict) and value.get("state") == "connected"
        ]

        if not connected:
            print("NOT_CONNECTED")
            print(f"controller states: {client.controller_states()}")
            print(
                "需要 Switch 2 进入 Change Grip/Order，"
                "并在浏览器点击 Create Pro Controller"
            )
            return 3

        index = connected[0]
        print(f"controller index: {index}")

        macro_id, ack_seconds = client.macro_ack("0.1s", index=index)
        print(f"macro id: {macro_id}")
        print(f"macro ack latency: {ack_seconds * 1000:.1f} ms")

        completed = client.wait_macro(macro_id, index=index, timeout=5.0)
        print(f"macro completed: {completed}")
        return 0
    finally:
        client.close()
        print("socket closed")


if __name__ == "__main__":
    sys.exit(main())
