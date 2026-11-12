@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
    if errorlevel 1 goto :error
)
".venv\Scripts\python.exe" -m pip install -r requirements-build.txt
if errorlevel 1 goto :error
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --onefile --windowed --name TarkovGym --hidden-import pynput.keyboard._win32 --hidden-import pynput.mouse._win32 gym.py
if errorlevel 1 goto :error
echo.
echo Build complete: dist\TarkovGym.exe
pause
exit /b 0
:error
echo Build failed. See the message above.
pause
exit /b 1
