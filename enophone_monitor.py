#!/usr/bin/env python3
"""
Real-time Enophone EEG Monitoring Application
Uses BrainFlow SDK to connect to Enophone via Bluetooth and display real-time brainwave data.
"""

import time
import numpy as np
import argparse
from datetime import datetime

from brainflow.board_shim import BoardShim, BrainFlowInputParams, BoardIds
from brainflow.data_filter import DataFilter, FilterTypes, WindowOperations
from brainflow.ml_model import MLModel, BrainFlowMetrics, BrainFlowClassifiers


class EnophoneMonitor:
    def __init__(self, mac_address=None, sampling_rate=256):
        self.sampling_rate = sampling_rate
        self.board_id = BoardIds.ENOPHONE_BOARD
        self.mac_address = mac_address

        params = BrainFlowInputParams()
        if mac_address:
            params.mac_address = mac_address

        self.board = BoardShim(self.board_id, params)
        self.channels = None
        self.channel_names = None

    def connect(self):
        """Connect to the Enophone device."""
        print(f"Connecting to Enophone (MAC: {self.mac_address or 'auto-discover'})...")
        self.board.prepare_session()
        print("Connection successful!")

        self.channels = BoardShim.get_eeg_channels(self.board_id)
        self.channel_names = [
            "A1 (Left Ear)",
            "C3 (Left)",
            "C4 (Right)",
            "A2 (Right Ear)",
        ]

    def start_streaming(self, duration=None):
        """Start streaming EEG data."""
        self.board.start_stream()
        print(f"Streaming EEG data at {self.sampling_rate} Hz...")

        if duration:
            print(f"Will collect data for {duration} seconds...")

    def get_current_data(self, num_samples=256):
        """Get the latest EEG data."""
        data = self.board.get_current_board_data(num_samples)
        return data

    def calculate_band_powers(self, data):
        """Calculate power in different frequency bands."""
        if data.shape[1] < self.sampling_rate:
            return None

        band_powers = {}
        bands = {
            "Delta": (1, 4),
            "Theta": (4, 8),
            "Alpha": (8, 13),
            "Beta": (13, 30),
            "Gamma": (30, 50),
        }

        try:
            psd = DataFilter.get_psd_welch(
                data[self.channels[0]],
                self.sampling_rate,
                self.sampling_rate // 2,
                self.sampling_rate,
                WindowOperations.HANNING.value,
            )
            freq_bands = psd[1]
            psd_data = psd[0]

            for band_name, (low_freq, high_freq) in bands.items():
                band_mask = (freq_bands >= low_freq) & (freq_bands < high_freq)
                if np.any(band_mask):
                    band_powers[band_name] = np.mean(psd_data[band_mask])
                else:
                    band_powers[band_name] = 0
        except Exception as e:
            print(f"PSD Error: {e}")
            for band_name in bands:
                band_powers[band_name] = 0

        return band_powers

    def calculate_focus_score(self, data):
        """Calculate a simple focus score based on Beta/Alpha ratio."""
        if data.shape[1] < self.sampling_rate:
            return 50

        try:
            psd = DataFilter.get_psd_welch(
                data[self.channels[0]],
                self.sampling_rate,
                self.sampling_rate // 2,
                self.sampling_rate,
                WindowOperations.HANNING.value,
            )
            freq_bands = psd[1]
            psd_data = psd[0]

            alpha_mask = (freq_bands >= 8) & (freq_bands < 13)
            beta_mask = (freq_bands >= 13) & (freq_bands < 30)

            alpha_power = np.mean(psd_data[alpha_mask]) if np.any(alpha_mask) else 1
            beta_power = np.mean(psd_data[beta_mask]) if np.any(beta_mask) else 0

            if alpha_power > 0:
                focus_score = (beta_power / alpha_power) * 100
                return min(100, max(0, focus_score))
        except:
            pass

        return 50

        try:
            psd = DataFilter.get_psd_welch(
                data[self.channels[0]],
                self.sampling_rate,
                self.sampling_rate // 2,
                self.sampling_rate // 4,
            )
            freq_bands = psd[1]
            psd_data = psd[0]

            alpha_mask = (freq_bands >= 8) & (freq_bands <= 13)
            beta_mask = (freq_bands >= 13) & (freq_bands <= 30)

            alpha_power = np.mean(psd_data[alpha_mask])
            beta_power = np.mean(psd_data[beta_mask])

            if alpha_power > 0:
                focus_score = (beta_power / alpha_power) * 100
                return min(100, max(0, focus_score))
        except:
            pass

        return 50

    def monitor_realtime(self, update_interval=1, duration=None):
        """Monitor and display EEG data in real-time."""
        start_time = time.time()

        print("\n" + "=" * 60)
        print("ENOPHONE REAL-TIME MONITOR")
        print("=" * 60)
        print(f"Channels: {', '.join(self.channel_names)}")
        print("Press Ctrl+C to stop\n")

        try:
            while True:
                if duration and (time.time() - start_time) >= duration:
                    break

                data = self.get_current_data(self.sampling_rate * 2)

                if data.shape[1] > 0:
                    print(f"\n[{datetime.now().strftime('%H:%M:%S')}]")

                    for i, ch in enumerate(self.channels):
                        if ch < data.shape[0]:
                            channel_data = data[ch]
                            mean_val = np.mean(channel_data)
                            std_val = np.std(channel_data)
                            print(
                                f"  {self.channel_names[i]}: {mean_val:8.2f} µV (σ: {std_val:.2f})"
                            )

                    band_powers = self.calculate_band_powers(data)
                    if band_powers:
                        print(f"\n  Band Powers:")
                        for band, power in band_powers.items():
                            bar_len = int(power / 1000)
                            bar = "█" * min(bar_len, 30)
                            print(f"    {band:6s}: {power:10.2f} |{bar}")

                    focus = self.calculate_focus_score(data)
                    print(f"\n  Focus Score: {focus:.0f}/100")

                time.sleep(update_interval)

        except KeyboardInterrupt:
            print("\n\nStopping monitor...")

    def stop(self):
        """Stop streaming and release resources."""
        self.board.stop_stream()
        self.board.release_session()
        print("Session released. Goodbye!")


def find_enophone_mac():
    """Attempt to find Enophone MAC address (Linux)."""
    try:
        import subprocess

        result = subprocess.run(
            ["bluetoothctl", "paired-devices"], capture_output=True, text=True
        )
        for line in result.stdout.split("\n"):
            if "Enophone" in line or "ENO" in line:
                parts = line.split()
                if len(parts) >= 2:
                    return parts[1]
    except:
        pass
    return None


def main():
    parser = argparse.ArgumentParser(description="Real-time Enophone EEG Monitor")
    parser.add_argument(
        "--mac",
        type=str,
        default=None,
        help="Enophone MAC address (optional, auto-detect on Windows/Mac)",
    )
    parser.add_argument(
        "--duration", type=int, default=None, help="Duration to record in seconds"
    )
    parser.add_argument(
        "--interval", type=float, default=1.0, help="Update interval in seconds"
    )
    args = parser.parse_args()

    mac = args.mac
    if not mac:
        mac = find_enophone_mac()
        if mac:
            print(f"Found Enophone MAC: {mac}")
        else:
            print("Note: Enophone MAC not specified. Will attempt auto-discovery.")
            print("On Linux, you may need to provide MAC address manually.")
            print("Find MAC with: bluetoothctl paired-devices")

    monitor = EnophoneMonitor(mac_address=mac)

    try:
        monitor.connect()
        monitor.start_streaming()
        monitor.monitor_realtime(update_interval=args.interval, duration=args.duration)
    finally:
        monitor.stop()


if __name__ == "__main__":
    main()
