#!/bin/bash
# Lighthouse watchdog — restarts server if down
if ! curl -s --max-time 5 http://localhost:8420/health > /dev/null 2>&1; then
    echo "[$(date -u)] Lighthouse DOWN — restarting"
    pkill -f "uvicorn main:app" 2>/dev/null
    sleep 2
    cd /home/yaraclawd/lighthouse-trading
    source .venv/bin/activate
    setsid python3 -m uvicorn main:app --host 0.0.0.0 --port 8420 > /tmp/lighthouse.log 2>&1 &
    disown
    sleep 5
    if curl -s --max-time 5 http://localhost:8420/health > /dev/null 2>&1; then
        echo "[$(date -u)] Lighthouse RECOVERED"
        curl -s -X POST "https://api.telegram.org/bot8785179286:AAHO6zpcyl5v2SEo6NgIcxUAZ9AE9vX0xmA/sendMessage" \
          -H "Content-Type: application/json" \
          -d "{\"chat_id\": \"7422563444\", \"text\": \"🔦 Lighthouse was DOWN — watchdog restarted it successfully.\"}" > /dev/null 2>&1
    else
        echo "[$(date -u)] Lighthouse FAILED TO RECOVER"
        curl -s -X POST "https://api.telegram.org/bot8785179286:AAHO6zpcyl5v2SEo6NgIcxUAZ9AE9vX0xmA/sendMessage" \
          -H "Content-Type: application/json" \
          -d "{\"chat_id\": \"7422563444\", \"text\": \"🚨 Lighthouse is DOWN and watchdog FAILED to restart it. Manual intervention needed.\"}" > /dev/null 2>&1
    fi
fi
