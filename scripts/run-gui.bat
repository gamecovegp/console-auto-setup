@echo off
title CAS — Console Auto Setup
cd /d "%~dp0.."
REM Uses adb from windows-kit if present, else expects adb on PATH.
if exist "windows-kit\adb.exe" (
  python -m cas --adb "windows-kit\adb.exe" --fastboot "windows-kit\fastboot.exe" %*
) else (
  python -m cas %*
)
pause
