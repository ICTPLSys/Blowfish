#!/bin/bash
# Kill benchmark / VM helper processes (blowfish).

echo "Looking for run_all.py..."
RUN_PID=$(ps aux | grep "[r]un_all.py" | awk 'NR==1 {print $2}')
if [ -n "$RUN_PID" ]; then
    echo "Killing run_all.py (PID: $RUN_PID)..."
    kill -9 "$RUN_PID"
    echo "run_all.py stopped."
else
    echo "No run_all.py process found."
fi

echo "Looking for run_auto.py..."
RUN_PID=$(ps aux | grep "[r]un_auto.py" | awk 'NR==1 {print $2}')
if [ -n "$RUN_PID" ]; then
    echo "Killing run_auto.py (PID: $RUN_PID)..."
    kill -9 "$RUN_PID"
    echo "run_auto.py stopped."
else
    echo "No run_auto.py process found."
fi

echo "Looking for qemu-system..."
QEMU_PID=$(ps aux | grep "[q]emu-system" | awk 'NR==1 {print $2}')
if [ -n "$QEMU_PID" ]; then
    echo "Killing qemu (PID: $QEMU_PID)..."
    kill -9 "$QEMU_PID"
    echo "QEMU stopped."
else
    echo "No qemu-system process found."
fi

echo "Done."
