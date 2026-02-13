# Startup Script for Local Testing (No Twilio Costs)
Write-Host ">>> Opening Chat Interface in Browser..."
Start-Process "http://localhost:8085/chat-ui"

Write-Host ">>> Starting Server..."
python run_server.py
