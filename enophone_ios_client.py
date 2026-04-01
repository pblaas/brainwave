#!/usr/bin/env python3
"""
iOS-compatible Enophone client.
Connects to a remote WebSocket server (running on Mac/Linux) instead of
accessing Bluetooth directly (which doesn't work on iOS).

Usage:
    python enophone_ios_client.py --host 192.168.1.100 --port 8765
"""

import asyncio
import json
import time
from datetime import datetime, timezone


class EnophoneClient:
    def __init__(self, host="localhost", port=8765):
        self.host = host
        self.port = port
        self.uri = f"ws://{host}:{port}"
        self.running = False
        self.latest_data = {
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
            "channels": {"A1": 0, "C3": 0, "C4": 0, "A2": 0},
            "means": {"A1": 0, "C3": 0, "C4": 0, "A2": 0},
            "band_powers": {"Delta": 0, "Theta": 0, "Alpha": 0, "Beta": 0, "Gamma": 0},
            "focus_score": 50,
        }

    async def connect(self):
        try:
            self.ws = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=10.0
            )
            self.reader, self.writer = self.ws
            print(f"Connected to {self.uri}")
            return True
        except asyncio.TimeoutError:
            print(f"Timeout connecting to {self.uri}")
            return False
        except Exception as e:
            print(f"Connection failed: {e}")
            return False

    async def receive_loop(self):
        buffer = ""
        while self.running:
            try:
                data = await asyncio.wait_for(self.reader.read(4096), timeout=5.0)
                if not data:
                    break
                buffer += data.decode()
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    try:
                        self.latest_data = json.loads(line)
                    except json.JSONDecodeError:
                        pass
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"Receive error: {e}")
                break
        print("Disconnected from server")

    def get_data(self):
        return self.latest_data.copy()

    async def close(self):
        self.running = False
        if hasattr(self, "writer"):
            self.writer.close()
            await self.writer.wait_closed()


async def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="iOS Enophone client - connects to remote WebSocket server"
    )
    parser.add_argument(
        "--host", default="localhost", help="Server IP address (default: localhost)"
    )
    parser.add_argument(
        "--port", type=int, default=8765, help="WebSocket port (default: 8765)"
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Data refresh interval in seconds (default: 2.0)",
    )
    args = parser.parse_args()

    client = EnophoneClient(host=args.host, port=args.port)

    print(f"Connecting to Enophone server at {args.host}:{args.port}...")

    if not await client.connect():
        print("Failed to connect. Make sure the server is running on your Mac.")
        return

    client.running = True
    receive_task = asyncio.create_task(client.receive_loop())

    try:
        while True:
            data = client.get_data()
            print(f"\n[{data['timestamp']}]")
            print(f"  Focus Score: {data['focus_score']:.1f}")
            print(f"  Band Powers:")
            for band, power in data["band_powers"].items():
                print(f"    {band}: {power:.2f}")
            print(f"  Channels (means):")
            for ch, val in data["means"].items():
                print(f"    {ch}: {val:.2f}")
            await asyncio.sleep(args.interval)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        client.running = False
        await client.close()
        await receive_task


if __name__ == "__main__":
    asyncio.run(main())
