#!/usr/bin/env python3
"""
Enophone WebSocket proxy.
Connects to a remote WebSocket server and re-broadcasts data to local clients.

Usage:
    python enophone_ios_client.py --host <remote-ip> --port 8765
    # Then connect local apps to ws://localhost:8765
"""

import asyncio
import json
from datetime import datetime, timezone
from collections import deque

try:
    import websockets

    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False


class EnophoneProxy:
    def __init__(self, host="localhost", remote_port=8765, local_port=8765):
        self.host = host
        self.remote_port = remote_port
        self.local_port = local_port
        self.remote_uri = f"ws://{host}:{remote_port}"
        self.running = False
        self.latest_data = {
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
            "channels": {"A1": 0, "C3": 0, "C4": 0, "A2": 0},
            "means": {"A1": 0, "C3": 0, "C4": 0, "A2": 0},
            "band_powers": {"Delta": 0, "Theta": 0, "Alpha": 0, "Beta": 0, "Gamma": 0},
            "focus_score": 50,
        }
        self.local_clients = set()

    async def connect_remote(self):
        if not WEBSOCKETS_AVAILABLE:
            print("websockets library required")
            return False
        try:
            self.remote_ws = await asyncio.wait_for(
                websockets.connect(self.remote_uri), timeout=10.0
            )
            print(f"Connected to remote server {self.remote_uri}")
            return True
        except Exception as e:
            print(f"Failed to connect to remote: {e}")
            return False

    async def handle_local_client(self, websocket):
        self.local_clients.add(websocket)
        try:
            await websocket.send(json.dumps(self.latest_data))
            async for _ in websocket:
                pass
        except Exception:
            pass
        finally:
            self.local_clients.remove(websocket)

    async def broadcast_to_local(self):
        while self.running:
            if self.local_clients and self.latest_data:
                msg = json.dumps(self.latest_data)
                dead = set()
                for client in self.local_clients:
                    try:
                        await client.send(msg)
                    except Exception:
                        dead.add(client)
                for client in dead:
                    self.local_clients.discard(client)
            await asyncio.sleep(0.5)

    async def remote_receive_loop(self):
        while self.running:
            try:
                message = await asyncio.wait_for(self.remote_ws.recv(), timeout=5.0)
                self.latest_data = json.loads(message)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"Remote connection lost: {e}")
                break
        print("Remote disconnected, attempting reconnect...")
        while self.running:
            if await self.connect_remote():
                break
            await asyncio.sleep(2)

    async def start_server(self):
        print(f"Starting local WebSocket server on port {self.local_port}")
        async with websockets.serve(
            self.handle_local_client, "0.0.0.0", self.local_port
        ):
            await asyncio.Future()

    async def run(self):
        self.running = True

        if not await self.connect_remote():
            return

        server_task = asyncio.create_task(self.start_server())
        remote_task = asyncio.create_task(self.remote_receive_loop())
        broadcast_task = asyncio.create_task(self.broadcast_to_local())

        await asyncio.gather(server_task, remote_task, broadcast_task)


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Enophone WebSocket proxy")
    parser.add_argument("--host", default="localhost", help="Remote server IP address")
    parser.add_argument(
        "--remote-port",
        type=int,
        default=8765,
        help="Remote server port (default: 8765)",
    )
    parser.add_argument(
        "--local-port", type=int, default=8765, help="Local server port (default: 8765)"
    )
    args = parser.parse_args()

    proxy = EnophoneProxy(
        host=args.host, remote_port=args.remote_port, local_port=args.local_port
    )

    print(
        f"Starting proxy: {args.host}:{args.remote_port} -> localhost:{args.local_port}"
    )
    print("Local apps can connect to ws://localhost:8765")

    try:
        await proxy.run()
    except KeyboardInterrupt:
        print("\nShutting down...")
        proxy.running = False


if __name__ == "__main__":
    if not WEBSOCKETS_AVAILABLE:
        print("Error: websockets library required")
        print("Install with: pip install websockets")
    else:
        asyncio.run(main())
