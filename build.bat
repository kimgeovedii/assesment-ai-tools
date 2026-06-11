@echo off
echo ============================================
echo  Build ExAI - Screen AI Assistant
echo ============================================

:: Install dependencies
pip install pyinstaller mss pystray pillow google-generativeai openai keyboard --quiet

:: Build EXE via spec file (UPX sudah disabled di Redis.spec)
python -m PyInstaller --clean ExAI.spec

echo.
echo ============================================
echo  Selesai! File EXE ada di folder: dist\
echo  Copy 15.ico ke folder tsb jika perlu
echo ============================================
pause