@echo off
REM console-auto-setup - Windows launcher (runs the PowerShell engine run.ps1).
REM   run.bat <mode> [args]
REM   modes: inspect | getcfg <emu> | checklist <emu> | setpath <emu> <key> <value>
REM          | grant <emu> | clonetest <emu> | backup <emu> | backup-all | restore <emu>
REM          | restore-all | root-check
REM
REM ONE-TIME setup (only if PowerShell refuses to run the script - see README):
REM   powershell -Command "Set-ExecutionPolicy -Scope CurrentUser RemoteSigned"
REM If you copied this folder from a USB stick and Windows 'blocks' it, also run once:
REM   powershell -Command "Get-ChildItem '%~dp0*.ps1' | Unblock-File"
REM Double-clicked with no mode? Show usage instead of silently running 'inspect'
REM (each double-click was dumping another inspect_*.txt into results\).
if "%~1"=="" (
  echo usage: run.bat ^<mode^> [args]   -- open a cmd prompt in this folder and type the mode
  echo   modes: inspect ^| getcfg ^<emu^> ^| checklist ^<emu^> ^| setpath ^<emu^> ^<key^> ^<value^>
  echo          grant ^<emu^> ^| clonetest ^<emu^> ^| backup ^<emu^> ^| backup-all ^| restore ^<emu^>
  echo          restore-all ^| root-check
  echo   example: run.bat clonetest flycast
  pause
  exit /b 1
)
powershell -NoProfile -File "%~dp0run.ps1" %*
