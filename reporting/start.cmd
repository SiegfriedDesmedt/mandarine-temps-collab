@echo off
set "PYTHONPATH=%~dp0.."
python "%~dp0run_backend.py" --port 4174
