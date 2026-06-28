@echo off
echo Starting PlexMatch...

:: Start Flask backend (installs Python deps first so it can't fail silently)
echo [1/2] Starting backend (port 5174)...
start "PlexMatch Backend" cmd /k "cd /d %~dp0backend && python -m pip install -q -r requirements.txt && python app.py"

:: Wait a moment for backend to boot
timeout /t 2 /nobreak > nul

:: Start Vite frontend
echo [2/2] Starting frontend (port 5173)...
start "PlexMatch Frontend" cmd /k "cd /d %~dp0frontend && npm run dev"

:: Open browser after a short delay
timeout /t 3 /nobreak > nul
start http://localhost:5173

echo.
echo PlexMatch is running at http://localhost:5173
echo Close the two terminal windows to stop.
