@echo off
rem Double-click or run from an elevated prompt: installs the Predator control tasks.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_predator_control.ps1" %*
