#!/bin/bash
# Watchdog for Meshtastic-Matrix bridge
# Restarts the bridge if BrokenPipeError detected in recent logs.
# Run via systemd timer every 5 minutes.

SERVICE="meshtastic-matrix-bridge"
LOG=$(journalctl -u "$SERVICE" --since "5 minutes ago" --no-pager 2>/dev/null)

if echo "$LOG" | grep -q "BrokenPipeError\|FATAL.*Mesh connection lost"; then
    echo "$(date): Connection error detected, restarting $SERVICE..."
    systemctl restart "$SERVICE"
    echo "$(date): $SERVICE restarted" >> /var/log/mesh-bridge-watchdog.log
fi
