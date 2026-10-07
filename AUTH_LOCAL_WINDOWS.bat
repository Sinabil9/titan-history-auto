@echo off
cd /d "%~dp0"
where py >nul 2>&1
if errorlevel 1 (
  echo Python launcher not found. Install Python 3.11+ first.
  pause
  exit /b 1
)
if not exist .authvenv py -3.11 -m venv .authvenv
call .authvenv\Scripts\activate.bat
python -m pip install --quiet --upgrade pip
pip install --quiet google-auth google-auth-oauthlib
python youtube_auth.py
pause
