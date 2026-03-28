#!/bin/bash
echo "================================================"
echo "  Reltio Intelligence Agent — Startup"
echo "================================================"

cd "$(dirname "$0")"

# Install deps
echo "[1/3] Installing Python dependencies..."
cd backend
pip install -r requirements.txt -q

echo "[2/3] Starting FastAPI backend on http://localhost:8000 ..."
uvicorn main:app --host 0.0.0.0 --port 8000 --reload &
BACKEND_PID=$!

echo "[3/3] Opening frontend in browser..."
sleep 2

# Try to open browser
if command -v xdg-open &>/dev/null; then
  xdg-open http://localhost:8000
elif command -v open &>/dev/null; then
  open http://localhost:8000
else
  echo "Open http://localhost:8000 in your browser."
fi

echo ""
echo "================================================"
echo "  Backend running (PID $BACKEND_PID)"
echo "  Frontend served at http://localhost:8000"
echo "  Press Ctrl+C to stop"
echo "================================================"

wait $BACKEND_PID
