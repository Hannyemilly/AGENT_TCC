@echo off
echo Iniciando o AI Agent Worker com 1 processo e 2 threads...

.venv\Scripts\python.exe -m dramatiq main --processes 1 --threads 2