#!/bin/bash
# jarvis_ctl.sh — manage the JARVIS LaunchAgent
# Usage: jarvis_ctl.sh [start|stop|restart|status|log|open]

LABEL="com.jarvis.server"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
URL="http://localhost:5001"

case "${1:-status}" in

  start)
    launchctl load -w "$PLIST"
    echo "JARVIS started."
    ;;

  stop)
    launchctl unload "$PLIST"
    echo "JARVIS stopped."
    ;;

  restart)
    launchctl unload "$PLIST" 2>/dev/null
    sleep 1
    launchctl load -w "$PLIST"
    echo "JARVIS restarted."
    ;;

  status)
    if launchctl list | grep -q "$LABEL"; then
      PID=$(launchctl list | grep "$LABEL" | awk '{print $1}')
      echo "JARVIS is RUNNING (pid=$PID) → $URL"
    else
      echo "JARVIS is STOPPED"
    fi
    ;;

  log)
    tail -f "$HOME/Library/Logs/jarvis.log" "$HOME/Library/Logs/jarvis.error.log"
    ;;

  open)
    open "$URL"
    ;;

  *)
    echo "Usage: $0 [start|stop|restart|status|log|open]"
    exit 1
    ;;

esac
