@echo off
REM CAS command-line launcher (headless provisioning). Examples:
REM   cas.bat list
REM   cas.bat provision --profile odin2mini
REM   cas.bat provision-all
REM   cas.bat capture odin2mini
REM   cas.bat seal --profile odin2mini
cd /d "%~dp0"
if exist "windows-kit\adb.exe" (
  python -m cas.cli --adb "windows-kit\adb.exe" --fastboot "windows-kit\fastboot.exe" %*
) else (
  python -m cas.cli %*
)
