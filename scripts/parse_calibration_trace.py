#!/usr/bin/env python3
"""
Parse a FOC-stim notification trace into CSV for calibration tests.

Reads a binary trace file produced by enabling
    Preferences -> "Dump notifications to file"
in restim. Extracts each NotificationSkinResistance reading and joins it
with the most-recently-observed device volume, signal stats, and currents.

Usage:
    cd ~/restim
    venv/bin/python scripts/parse_calibration_trace.py \\
        "trace/focstim-notifications 2026-05-12 143000.binpb" \\
        out.csv

Output columns (one row per SkinResistance reading):
    timestamp_ns, ms_since_first
    R_a, X_a, R_b, X_b, R_c, X_c, R_d, X_d   # raw R + jX (ohms)
    Z_a_mag, Z_b_mag, Z_c_mag, Z_d_mag        # |Z| magnitude (ohms)
    volume_at_capture                         # last NotificationDeviceVolume
    v_drive_at_capture                        # last NotificationSignalStats.v_drive
    transformer_utilization_at_capture
    voltage_utilization_at_capture
    rms_a, rms_b, rms_c, rms_d                # last NotificationCurrents rms
    peak_a, peak_b, peak_c, peak_d
    output_power, output_power_skin

Also prints inter-arrival cadence summary to stdout.
"""
import csv
import math
import sys
from pathlib import Path

import stream  # pystream-protobuf, already in restim venv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from device.focstim.focstim_rpc_pb2 import Notification


def main():
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)

    in_path, out_path = sys.argv[1], sys.argv[2]

    last = {
        'volume': float('nan'),
        'v_drive': float('nan'),
        'tx_util': float('nan'),
        'vlt_util': float('nan'),
        'rms_a': float('nan'), 'rms_b': float('nan'),
        'rms_c': float('nan'), 'rms_d': float('nan'),
        'peak_a': float('nan'), 'peak_b': float('nan'),
        'peak_c': float('nan'), 'peak_d': float('nan'),
        'output_power': float('nan'),
        'output_power_skin': float('nan'),
    }
    rows = []
    t0 = None

    for n in stream.parse(in_path, Notification):
        ts = n.timestamp
        if t0 is None:
            t0 = ts

        if n.HasField('notification_device_volume'):
            last['volume'] = n.notification_device_volume.volume

        elif n.HasField('notification_signal_stats'):
            s = n.notification_signal_stats
            last['v_drive'] = s.v_drive
            last['tx_util'] = s.transformer_utilization
            last['vlt_util'] = s.voltage_utilization

        elif n.HasField('notification_currents'):
            c = n.notification_currents
            last.update(
                rms_a=c.rms_a, rms_b=c.rms_b, rms_c=c.rms_c, rms_d=c.rms_d,
                peak_a=c.peak_a, peak_b=c.peak_b, peak_c=c.peak_c, peak_d=c.peak_d,
                output_power=c.output_power,
                output_power_skin=c.output_power_skin,
            )

        elif n.HasField('notification_skin_resistance'):
            r = n.notification_skin_resistance
            rows.append({
                'timestamp_ns': ts,
                'ms_since_first': (ts - t0) / 1e6,
                'R_a': r.resistance_a, 'X_a': r.reluctance_a,
                'R_b': r.resistance_b, 'X_b': r.reluctance_b,
                'R_c': r.resistance_c, 'X_c': r.reluctance_c,
                'R_d': r.resistance_d, 'X_d': r.reluctance_d,
                'Z_a_mag': math.hypot(r.resistance_a, r.reluctance_a),
                'Z_b_mag': math.hypot(r.resistance_b, r.reluctance_b),
                'Z_c_mag': math.hypot(r.resistance_c, r.reluctance_c),
                'Z_d_mag': math.hypot(r.resistance_d, r.reluctance_d),
                'volume_at_capture': last['volume'],
                'v_drive_at_capture': last['v_drive'],
                'transformer_utilization_at_capture': last['tx_util'],
                'voltage_utilization_at_capture': last['vlt_util'],
                'rms_a': last['rms_a'], 'rms_b': last['rms_b'],
                'rms_c': last['rms_c'], 'rms_d': last['rms_d'],
                'peak_a': last['peak_a'], 'peak_b': last['peak_b'],
                'peak_c': last['peak_c'], 'peak_d': last['peak_d'],
                'output_power': last['output_power'],
                'output_power_skin': last['output_power_skin'],
            })

    if not rows:
        print('No SkinResistance notifications found in trace.', file=sys.stderr)
        sys.exit(2)

    with open(out_path, 'w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    inter = [rows[i]['ms_since_first'] - rows[i-1]['ms_since_first']
             for i in range(1, len(rows))]
    inter.sort()
    n = len(inter)
    print(f'Wrote {len(rows)} rows to {out_path}')
    if n:
        median = inter[n // 2]
        p10 = inter[max(0, n // 10)]
        p90 = inter[min(n - 1, (n * 9) // 10)]
        print(f'SkinResistance cadence: '
              f'median={median:.1f}ms  p10={p10:.1f}ms  p90={p90:.1f}ms  '
              f'(rate ~{1000 / median:.1f} Hz)')


if __name__ == '__main__':
    main()
