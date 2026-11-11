@echo off
cd /d "%~dp0"
echo ============================================
echo   IU Arabic Courses Assistant
echo ============================================
set /p GEMINI_API_KEY=Paste your Gemini API key (from Google AI Studio) here, then press Enter: 
python -m pip install -q -r requirements.txt
start "" http://127.0.0.1:5000
python app.py
pause
