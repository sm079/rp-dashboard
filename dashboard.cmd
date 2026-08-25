@echo off
rem Open the pod dashboard in the browser; starts it in the background if needed.
cd /d "%~dp0"
where pythonw >nul 2>nul && (start "" pythonw dashboard.py) || (start "Pod dashboard" /min python dashboard.py)
