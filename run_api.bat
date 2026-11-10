@echo off
chcp 65001 >nul
cd /d "%~dp0"
if "%API_KEY%"=="" set API_KEY=doi-chuoi-nay-thanh-khoa-bi-mat
if "%HEADLESS%"=="" set HEADLESS=new
.venv\Scripts\python.exe -m uvicorn api:app --host 127.0.0.1 --port 8000
pause
