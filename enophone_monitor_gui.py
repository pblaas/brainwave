#!/usr/bin/env python3
"""
Real-time Enophone EEG Monitor with Visualization and WebSocket
Uses BrainFlow SDK and Matplotlib for real-time brainwave visualization.
Exposes metrics via WebSocket for external consumers (e.g., Grafana).
"""

import time
import json
import asyncio
import numpy as np
import argparse
import threading
import websockets
import logging
import sys
from collections import deque
from datetime import datetime, timezone

# Suppress websockets library tracebacks for invalid connections
logging.getLogger("websockets.server").setLevel(logging.ERROR)
logging.getLogger("websockets.client").setLevel(logging.ERROR)
logging.getLogger("websockets").setLevel(logging.ERROR)

# Set default excepthook to suppress tracebacks for known errors
_original_excepthook = sys.excepthook


def _custom_excepthook(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, (websockets.exceptions.InvalidMessage, EOFError)):
        # Suppress these common connection errors
        return
    _original_excepthook(exc_type, exc_value, exc_traceback)


sys.excepthook = _custom_excepthook

from brainflow.board_shim import BoardShim, BrainFlowInputParams, BoardIds
from brainflow.data_filter import DataFilter, WindowOperations


class EnophoneMonitor:
    def __init__(self, mac_address=None, sampling_rate=256, history_length=5):
        self.sampling_rate = sampling_rate
        self.board_id = BoardIds.ENOPHONE_BOARD
        self.mac_address = mac_address
        self.history_length = history_length

        params = BrainFlowInputParams()
        if mac_address:
            params.mac_address = mac_address

        self.board = BoardShim(self.board_id, params)
        self.channels = BoardShim.get_eeg_channels(self.board_id)
        self.channel_names = ["A1 (Left Ear)", "C3", "C4", "A2 (Right Ear)"]

        self.eeg_history = {
            ch: deque(maxlen=sampling_rate * history_length) for ch in self.channels
        }
        self.running = False
        self.data_thread = None
        self.websocket_server = None

        self.band_powers = {"Delta": 0, "Theta": 0, "Alpha": 0, "Beta": 0, "Gamma": 0}
        self.focus_score = 50

    def connect(self):
        """Connect to the Enophone device."""
        print(f"Connecting to Enophone...")
        self.board.prepare_session()
        print("Connected!")

    def _data_collection(self):
        """Background thread for continuous data collection."""
        while self.running:
            try:
                data = self.board.get_current_board_data(32)
                for ch in self.channels:
                    if ch < data.shape[0]:
                        self.eeg_history[ch].extend(data[ch].tolist())

                self._calculate_metrics()

            except Exception as e:
                print(f"Data collection error: {e}")
                time.sleep(0.1)

    def _calculate_metrics(self):
        """Calculate band powers and focus score."""
        if len(self.eeg_history[self.channels[0]]) < self.sampling_rate:
            return

        data = np.array(self.eeg_history[self.channels[0]])

        try:
            psd = DataFilter.get_psd_welch(
                data,
                self.sampling_rate,
                self.sampling_rate // 2,
                self.sampling_rate,
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

            for band, (low, high) in bands.items():
                mask = (freqs >= low) & (freqs < high)
                self.band_powers[band] = (
                    float(np.mean(psd_data[mask])) if np.any(mask) else 0
                )

            alpha_mask = (freqs >= 8) & (freqs < 13)
            beta_mask = (freqs >= 13) & (freqs < 30)

            alpha_power = np.mean(psd_data[alpha_mask]) if np.any(alpha_mask) else 1
            beta_power = np.mean(psd_data[beta_mask]) if np.any(beta_mask) else 0

            if alpha_power > 0:
                self.focus_score = min(100, max(0, (beta_power / alpha_power) * 100))
            else:
                self.focus_score = 50

        except Exception as e:
            pass

    def start(self):
        """Start streaming."""
        self.board.start_stream()
        self.running = True
        self.data_thread = threading.Thread(target=self._data_collection, daemon=True)
        self.data_thread.start()
        print(f"Streaming at {self.sampling_rate} Hz...")

    def get_history(self):
        """Get historical EEG data."""
        data = {}
        for ch in self.channels:
            if len(self.eeg_history[ch]) > 0:
                data[ch] = np.array(self.eeg_history[ch])
        return data

    def get_metrics(self):
        """Get current metrics as a dictionary."""
        channel_data = {}
        for i, ch in enumerate(self.channels):
            if len(self.eeg_history[ch]) > 0:
                data = np.array(self.eeg_history[ch])
                channel_data[self.channel_names[i]] = {
                    "mean": float(np.mean(data)),
                    "std": float(np.std(data)),
                    "min": float(np.min(data)),
                    "max": float(np.max(data)),
                }

        return {
            "timestamp": datetime.now(timezone.utc).isoformat() + "Z",
            "band_powers": self.band_powers,
            "focus_score": round(self.focus_score, 2),
            "channels": channel_data,
        }

    def stop(self):
        """Stop streaming."""
        self.running = False
        self.board.stop_stream()
        self.board.release_session()
        print("Stopped.")


class WebSocketServer:
    def __init__(self, host="0.0.0.0", port=8765):
        self.host = host
        self.port = port
        self.monitor = None
        self.clients = set()
        self.running = False

    async def process_request(self, path, headers):
        """Validate incoming WebSocket requests and reject invalid ones early."""
        return None  # Accept all requests

    async def wrapper(self, websocket):
        """Wrapper to handle exceptions in the WebSocket handler."""
        try:
            await self.handler(websocket)
        except Exception as e:
            print(f"WebSocket handler error: {e}")

    async def handler(self, websocket):
        """Handle a WebSocket client connection."""
        self.clients.add(websocket)
        try:
            print(f"Client connected: {websocket.remote_address}")
            try:
                await websocket.send(
                    json.dumps(
                        {
                            "type": "welcome",
                            "message": "Connected to Enophone Monitor",
                            "metrics": self.monitor.get_metrics()
                            if self.monitor
                            else None,
                        }
                    )
                )
            except Exception as e:
                print(f"Failed to send welcome: {e}")
                return

            while True:
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                    data = json.loads(message)
                    if data.get("type") == "ping":
                        await websocket.send(json.dumps({"type": "pong"}))
                    elif data.get("type") == "query":
                        metrics = self.monitor.get_metrics() if self.monitor else {}
                        await websocket.send(
                            json.dumps({"type": "query_response", "data": metrics})
                        )
                except asyncio.TimeoutError:
                    continue
                except json.JSONDecodeError as e:
                    print(f"JSON decode error: {e}")
                except websockets.exceptions.ConnectionClosed:
                    break
                except Exception as e:
                    print(f"Message handling error: {e}")
                    break
        except websockets.exceptions.InvalidMessage as e:
            print(f"Invalid WebSocket message: {e}")
        except Exception as e:
            print(f"Client error: {e}")
        finally:
            self.clients.discard(websocket)
            print(f"Client disconnected")

    async def broadcast_metrics(self):
        """Broadcast metrics to all connected clients."""
        while self.running:
            if self.monitor and self.clients:
                metrics = self.monitor.get_metrics()
                message = json.dumps({"type": "metrics", "data": metrics})
                dead_clients = set()
                for client in self.clients:
                    try:
                        await client.send(message)
                    except:
                        dead_clients.add(client)
                self.clients -= dead_clients
            await asyncio.sleep(0.5)

    def run(self, monitor):
        """Run the WebSocket server in a new event loop."""
        self.monitor = monitor
        self.running = True

        async def server_task():
            loop = asyncio.get_event_loop()

            # Suppress exception traceback for connection errors
            def exception_handler(loop, context):
                msg = context.get("message", "")
                exc = context.get("exception")
                if exc and "InvalidMessage" in str(type(exc).__name__):
                    print(f"WebSocket connection error (ignored): {exc}")
                elif "connection closed" in msg.lower():
                    pass  # Ignore normal connection closures
                else:
                    loop.set_exception_handler(context)

            loop.set_exception_handler(exception_handler)

            try:
                server = await websockets.serve(
                    self.wrapper,
                    self.host,
                    self.port,
                    ping_interval=None,
                    ping_timeout=None,
                )
                print(f"WebSocket server started at ws://{self.host}:{self.port}")
                await self.broadcast_metrics()
            except Exception as e:
                print(f"WebSocket server error: {e}")

        asyncio.run(server_task())


def run_cli(mac_address=None, duration=None, ws_port=8765):
    """Run command-line monitor with optional WebSocket."""
    from datetime import datetime, timezone

    viz = EnophoneMonitor(mac_address=mac_address)
    ws_server = None

    if ws_port:
        ws_server = WebSocketServer(port=ws_port)
        ws_server.monitor = viz
        ws_thread = threading.Thread(target=ws_server.run, args=(viz,), daemon=True)
        ws_thread.start()
        print(f"WebSocket server running on ws://0.0.0.0:{ws_port}")

    try:
        viz.connect()
        viz.start()

        print("\n" + "=" * 60)
        print("ENOPHONE REAL-TIME MONITOR")
        print("=" * 60 + "\n")

        start = time.time()
        while True:
            if duration and (time.time() - start) >= duration:
                break

            metrics = viz.get_metrics()
            print(f"\r[{datetime.now().strftime('%H:%M:%S')}] ", end="")
            print(f"Focus: {metrics['focus_score']:.0f}% ", end="")
            for band, power in metrics["band_powers"].items():
                print(f"{band[:1]}:{power:.0f} ", end="")

            time.sleep(0.5)

    except KeyboardInterrupt:
        pass
    finally:
        viz.stop()


def run_gui(mac_address=None, ws_port=8765):
    """Run GUI visualization with optional WebSocket."""
    import matplotlib.pyplot as plt

    viz = EnophoneMonitor(mac_address=mac_address)

    if ws_port:
        ws_server = WebSocketServer(port=ws_port)
        ws_server.monitor = viz
        ws_thread = threading.Thread(target=ws_server.run, args=(viz,), daemon=True)
        ws_thread.start()
        print(f"WebSocket server running on ws://0.0.0.0:{ws_port}")

    fig, axes = plt.subplots(2, 2, figsize=(14, 8))
    fig.suptitle(
        "Enophone Real-Time EEG Monitor\n(WebSocket: ws://localhost:8765)",
        fontsize=14,
        fontweight="bold",
    )

    lines = {}
    for idx, (ax, name) in enumerate(zip(axes.flat, viz.channel_names[:4])):
        ax.set_title(name, fontsize=10)
        ax.set_ylim(-100, 100)
        ax.set_xlim(0, viz.sampling_rate * viz.history_length)
        ax.set_xlabel("Samples")
        ax.set_ylabel("µV")
        (line,) = ax.plot([], [], lw=1)
        lines[idx] = line

    ax_info = fig.add_subplot(2, 2, 4)
    ax_info.axis("off")
    info_text = ax_info.text(
        0.1,
        0.9,
        "",
        transform=ax_info.transAxes,
        fontsize=10,
        verticalalignment="top",
        fontfamily="monospace",
    )

    plt.tight_layout()

    def update(frame):
        data = viz.get_history()
        for idx, ch in enumerate(viz.channels[:4]):
            if ch in data and len(data[ch]) > 0:
                lines[idx].set_data(range(len(data[ch])), data[ch])

        metrics = viz.get_metrics()
        info = f"Focus Score: {metrics['focus_score']:.0f}%\n\n"
        info += "Band Powers:\n"
        for band, power in metrics["band_powers"].items():
            bar = "█" * int(power / 100)
            info += f"  {band:6s}: {power:8.1f} {bar[:15]}\n"
        info += f"\nTimestamp: {metrics['timestamp']}"
        info_text.set_text(info)

        return list(lines.values()) + [info_text]

    from matplotlib.animation import FuncAnimation

    try:
        viz.connect()
        viz.start()

        ani = FuncAnimation(fig, update, interval=50, blit=True)
        plt.show()

    except KeyboardInterrupt:
        pass
    finally:
        viz.stop()


def main():
    parser = argparse.ArgumentParser(description="Enophone Real-Time Monitor")
    parser.add_argument("--mac", type=str, default=None, help="Enophone MAC address")
    parser.add_argument(
        "--duration", type=int, default=None, help="Duration in seconds"
    )
    parser.add_argument("--gui", action="store_true", help="Launch GUI visualization")
    parser.add_argument(
        "--ws-port", type=int, default=8765, help="WebSocket server port (0 to disable)"
    )
    parser.add_argument(
        "--ws-host", type=str, default="0.0.0.0", help="WebSocket server host"
    )
    args = parser.parse_args()

    mac = args.mac
    if not mac:
        try:
            import subprocess

            result = subprocess.run(
                ["bluetoothctl", "paired-devices"], capture_output=True, text=True
            )
            for line in result.stdout.split("\n"):
                if "enophone" in line.lower():
                    mac = line.split()[1]
                    break
        except:
            pass

    ws_port = args.ws_port if args.ws_port > 0 else None

    if args.gui:
        run_gui(mac, ws_port)
    else:
        run_cli(mac, args.duration, ws_port)


if __name__ == "__main__":
    main()
