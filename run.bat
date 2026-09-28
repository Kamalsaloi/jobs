@echo off
cd /d "%~dp0"
python job_hunter.py >> logs\scheduler.log 2>&1
