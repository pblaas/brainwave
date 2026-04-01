#!/usr/bin/env python3
"""
Simple HTTP server for Enophone metrics that Grafana can poll.
No plugin needed - use Grafana's built-in JSON/API data source or Infinity plugin.
"""

import json
import threading
import time
import numpy as np
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone

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
        self.focus_score = 50
        self.band_powers = {"Delta": 0, "Theta": 0, "Alpha": 0, "Beta": 0, "Gamma": 0}

    def connect(self):
        self.board.prepare_session()

    def start(self):
        self.board.start_stream()
        self.running = True

    def _collect(self):
        from collections import deque

        self.data_accumulator = deque(maxlen=self.sampling_rate * 2)

        while self.running:
            try:
                data = self.board.get_current_board_data(32)
                if data.shape[1] > 0:
                    ch = self.channels[0]
                    if ch < data.shape[0]:
                        self.data_accumulator.extend(data[ch].tolist())
                    self._calculate_metrics()
            except Exception as e:
                print(f"Collect error: {e}")
            time.sleep(0.05)

    def _calculate_metrics(self):
        if len(self.data_accumulator) < 256:
            return

        data = np.array(self.data_accumulator)

        try:
            nfft = 64
            if len(data) < nfft * 2:
                return
            psd = DataFilter.get_psd_welch(
                data,
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
                self.band_powers[band] = (
                    float(np.mean(psd_data[mask])) if np.any(mask) else 0
                )

            alpha_mask = (freqs >= 8) & (freqs < 13)
            beta_mask = (freqs >= 13) & (freqs < 30)
            alpha = np.mean(psd_data[alpha_mask]) if np.any(alpha_mask) else 1
            beta = np.mean(psd_data[beta_mask]) if np.any(beta_mask) else 0
            self.focus_score = (
                min(100, max(0, (beta / alpha) * 100)) if alpha > 0 else 50
            )

        except Exception as e:
            print(f"Metrics error: {e}")

    def get_metrics(self):
        return {
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
            "focus_score": round(self.focus_score, 1),
            "band_powers": self.band_powers,
        }

    def stop(self):
        self.running = False
        self.board.stop_stream()
        self.board.release_session()


class RequestHandler(BaseHTTPRequestHandler):
    monitor = None

    def do_GET(self):
        if self.path == "/metrics" or self.path == "/":
            metrics = self.monitor.get_metrics() if self.monitor else {}

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(metrics).encode())

        elif self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {args[0]}")


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Enophone HTTP Server")
    parser.add_argument("--mac", required=True, help="Enophone MAC address")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port")
    args = parser.parse_args()

    monitor = EnophoneMonitor(mac_address=args.mac)
    RequestHandler.monitor = monitor

    print(f"Connecting to Enophone at {args.mac}...")
    monitor.connect()
    monitor.start()

    collect_thread = threading.Thread(target=monitor._collect, daemon=True)
    collect_thread.start()

    server = HTTPServer(("0.0.0.0", args.port), RequestHandler)
    print(f"HTTP server running at http://localhost:{args.port}/metrics")
    print(f"Grafana query URL: http://host.docker.internal:{args.port}/metrics")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        monitor.stop()
        server.shutdown()


if __name__ == "__main__":
    main()
