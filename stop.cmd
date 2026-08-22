@echo off
rem Copy any unsaved files home, then terminate the pod.
cd /d "%~dp0"
python rp.py down
pause
