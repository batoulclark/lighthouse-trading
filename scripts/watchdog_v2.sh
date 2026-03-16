#!/bin/bash
# Lighthouse Watchdog v2 — Silent unless recovery fails
# Checks every 5 minutes via cron
# ONLY alerts Jean if server is DOWN and can't be restarted

HEALTH_URL="http://localhost:8420/health"
LOG="/home/yaraclawd/lighthouse-trading/data/watchdog.log"
FOUFI_TOKEN="8785179286:AAHO6zpcyl5v2SEo6NgIcxUAZ9AE9vX0xmA"
CHAT_ID="7422563444"

# Check if server responds
if curl -s --max-time 5 "$HEALTH_URL" > /dev/null 2>&1; then
    # Server is up — silent exit
    exit 0
fi

# Server is down — try to restart
echo "[$(date -u)] Lighthouse DOWN — attempting restart" >> "$LOG"

# Kill any zombie processes
pkill -f "uvicorn main:app" 2>/dev/null
sleep 3

# Start fresh
cd /home/yaraclawd/lighthouse-trading
source .venv/bin/activate
nohup python3 -m uvicorn main:app --host 0.0.0.0 --port 8420 > /tmp/lighthouse.log 2>&1 &
disown

# Wait for startup
sleep 8

# Verify recovery
if curl -s --max-time 5 "$HEALTH_URL" > /dev/null 2>&1; then
    echo "[$(date -u)] Lighthouse RECOVERED (silent)" >> "$LOG"
    # NO Telegram alert on successful recovery
    exit 0
fi

# Recovery failed — ALERT JEAN (this is the only alert)
echo "[$(date -u)] Lighthouse FAILED TO RECOVER — alerting Jean" >> "$LOG"
curl -s -X POST "https://api.telegram.org/bot${FOUFI_TOKEN}/sendMessage" \
    -H "Content-Type: application/json" \
    -d "{\"chat_id\": \"${CHAT_ID}\", \"text\": \"🚨 Lighthouse is DOWN and auto-restart failed. Manual intervention needed.\"}" > /dev/null 2>&1
