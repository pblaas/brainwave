#!/usr/bin/env python3
"""
WebSocket server for Enophone EEG data broadcasting.
Serves on ws://localhost:8765 for real-time brainwave visualization.
"""

import asyncio
import json
import threading
import time
import numpy as np
from datetime import datetime, timezone
import websockets

from brainflow.board_shim import BoardShim, BrainFlowInputParams, BoardIds
from brainflow.data_filter import DataFilter, WindowOperations


class EnophoneMonitor:
    def __init__(self, mac_address=None, sampling_rate=256):
        self.sampling_rate = sampling_rate
        self.board_id = BoardIds.ENOPHONE_BOARD
        self.mac_address = mac_address

        params = BrainFlowInputParams()
        if mac_address:
            params.mac_address = mac_address

        self.board = BoardShim(self.board_id, params)
        self.channels = BoardShim.get_eeg_channels(self.board_id)
        self.channel_names = ["A1", "C3", "C4", "A2"]

        self.running = False
        self.latest_data = {
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
            "channels": {"A1": 0, "C3": 0, "C4": 0, "A2": 0},
            "means": {"A1": 0, "C3": 0, "C4": 0, "A2": 0},
            "band_powers": {"Delta": 0, "Theta": 0, "Alpha": 0, "Beta": 0, "Gamma": 0},
            "focus_score": 50,
        }

        self._last_means_update = 0
        self._means_update_interval = 1.0
        self._cached_means = {"A1": 0, "C3": 0, "C4": 0, "A2": 0}

    def connect(self):
        self.board.prepare_session()

    def start(self):
        self.board.start_stream()
        self.running = True

    def _collect(self):
        from collections import deque

        self.data_accumulator = {
            ch: deque(maxlen=self.sampling_rate * 2) for ch in self.channels
        }

        while self.running:
            try:
                data = self.board.get_current_board_data(32)
                if data.shape[1] > 0:
                    for i, ch in enumerate(self.channels):
                        if ch < data.shape[0]:
                            self.data_accumulator[ch].extend(data[ch].tolist())
                    self._calculate_metrics()
            except Exception as e:
                print(f"Collect error: {e}")
            time.sleep(0.05)

    def _calculate_metrics(self):
        data_by_channel = {}
        for i, ch in enumerate(self.channels):
            if len(self.data_accumulator[ch]) > 0:
                data_by_channel[self.channel_names[i]] = np.array(
                    self.data_accumulator[ch]
                )

        if not data_by_channel:
            return

        means = {name: float(np.mean(data)) for name, data in data_by_channel.items()}

        current_time = time.time()
        if current_time - self._last_means_update > self._means_update_interval:
            self._last_means_update = current_time
            self._cached_means = means.copy()

        self.latest_data["timestamp"] = datetime.now(timezone.utc).isoformat() + "Z"
        self.latest_data["means"] = self._cached_means.copy()
        self.latest_data["channels"] = self._cached_means.copy()

        main_data = (
            list(data_by_channel.values())[0] if data_by_channel else np.array([])
        )
        if len(main_data) < 256:
            return

        try:
            nfft = 64
            if len(main_data) < nfft * 2:
                return
            psd = DataFilter.get_psd_welch(
                main_data,
                nfft,
                nfft // 2,
                256,
                WindowOperations.HANNING.value,
            )
            freqs = psd[1]
            psd_data = psd[0]

            bands = {
                "Delta": (0.5, 4),
                "Theta": (4, 8),
                "Alpha": (8, 13),
                "Beta": (13, 30),
                "Gamma": (30, 50),
            }
            for band, (l, h) in bands.items():
                mask = (freqs >= l) & (freqs < h)
                self.latest_data["band_powers"][band] = (
                    float(np.mean(psd_data[mask])) if np.any(mask) else 0
                )

            alpha_mask = (freqs >= 8) & (freqs < 13)
            beta_mask = (freqs >= 13) & (freqs < 30)
            alpha = np.mean(psd_data[alpha_mask]) if np.any(alpha_mask) else 1
            beta = np.mean(psd_data[beta_mask]) if np.any(beta_mask) else 0
            self.latest_data["focus_score"] = (
                min(100, max(0, (beta / alpha) * 100)) if alpha > 0 else 50
            )

        except Exception as e:
            print(f"Metrics error: {e}")

    def get_data(self):
        return self.latest_data.copy()

    def stop(self):
        self.running = False
        self.board.stop_stream()
        self.board.release_session()


class SimulatedMonitor:
    def __init__(self):
        self.running = False
        self.latest_data = {
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
            "channels": {"A1": 0, "C3": 0, "C4": 0, "A2": 0},
            "means": {"A1": 0, "C3": 0, "C4": 0, "A2": 0},
            "band_powers": {"Delta": 0, "Theta": 0, "Alpha": 0, "Beta": 0, "Gamma": 0},
            "focus_score": 50,
        }

    def connect(self):
        print("Simulated monitor ready")

    def start(self):
        self.running = True

    def get_data(self):
        current_time = time.time()
        if not hasattr(self, "_last_update") or current_time - self._last_update > 1.0:
            self._last_update = current_time
            self._t = current_time

        t = self._t
        self.latest_data["timestamp"] = datetime.now(timezone.utc).isoformat() + "Z"
        self.latest_data["means"] = {
            "A1": 100 + 50 * np.sin(t * 2),
            "C3": 120 + 40 * np.sin(t * 2.3 + 1),
            "C4": 110 + 45 * np.sin(t * 1.8 + 2),
            "A2": 90 + 55 * np.sin(t * 2.5 + 3),
        }
        self.latest_data["channels"] = self.latest_data["means"].copy()
        self.latest_data["focus_score"] = 50 + 30 * np.sin(t * 0.5)
        return self.latest_data.copy()

    def stop(self):
        self.running = False


monitor = None
clients = set()


async def broadcast(websocket):
    global clients
    clients.add(websocket)
    try:
        while True:
            if monitor:
                data = monitor.get_data()
                await websocket.send(json.dumps(data))
            await asyncio.sleep(2.0)
    except Exception:
        pass
    finally:
        clients.remove(websocket)


async def run_websocket_server(port=8765):
    server = await websockets.serve(broadcast, "0.0.0.0", port)
    return server


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Enophone WebSocket Server")
    parser.add_argument("--mac", help="Enophone MAC address (required for real device)")
    parser.add_argument("--port", type=int, default=8765, help="WebSocket port")
    parser.add_argument(
        "--simulate", action="store_true", help="Run with simulated data"
    )
    args = parser.parse_args()

    global monitor

    if args.simulate:
        monitor = SimulatedMonitor()
    else:
        if not args.mac:
            parser.error("--mac is required unless using --simulate")
        monitor = EnophoneMonitor(mac_address=args.mac)

    print(f"Starting server on port {args.port}...")
    monitor.connect()
    monitor.start()

    if not args.simulate:
        collect_thread = threading.Thread(target=monitor._collect, daemon=True)
        collect_thread.start()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    server = loop.run_until_complete(run_websocket_server(args.port))

    print(f"WebSocket server running at ws://localhost:{args.port}")
    print("WebGL client can connect to view real-time brainwave art")

    try:
        loop.run_forever()
    except KeyboardInterrupt:
        monitor.stop()
        loop.run_until_complete(server.close())


if __name__ == "__main__":
    main()
