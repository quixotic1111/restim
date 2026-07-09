#!/bin/bash
# Second restim instance ("device B") — fully isolated from the primary via
# RESTIM_CONFIG_DIR: its own restim.ini (ports 12356-12358 / T-code UDP
# 12357), its own restim.log, its own calibration.json, all in ~/.restim-b.
# First launch: pick device B's serial port in Preferences (left blank on
# purpose so it can't grab device A's tty).
cd "$(dirname "$0")"
export RESTIM_CONFIG_DIR="$HOME/.restim-b"
mkdir -p "$RESTIM_CONFIG_DIR"
exec ./venv/bin/python ./restim.py
