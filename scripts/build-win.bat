@echo off
REM ============================================================================
REM  build-win.bat  -  build the CAS Windows bundle (cas-gui.exe + cas.exe)
REM
REM  Run from a Windows machine with Python 3.14 + PyInstaller installed.
REM  PyInstaller CANNOT cross-compile: this MUST run on Windows to make Windows
REM  binaries (run build-linux / a macOS build separately on those OSes).
REM
REM  Prereqs (one-time):
REM     py -3.14 -m pip install --upgrade pip
REM     py -3.14 -m pip install "pyinstaller>=6.11"     REM 6.11+ supports Python 3.14
REM ============================================================================

setlocal
REM script lives in scripts\ - build from the repo root so cas.spec's relative datas resolve.
cd /d "%~dp0.."

echo(
echo === [1/4] Sanity: Python + PyInstaller ===
where py >nul 2>&1 && (set "PY=py -3.14") || (set "PY=python")
%PY% --version || (echo ERROR: Python not found on PATH. & exit /b 1)
%PY% -m PyInstaller --version >nul 2>&1 || (
    echo ERROR: PyInstaller not installed for this Python.
    echo        Run:  %PY% -m pip install "pyinstaller>=6.11"
    exit /b 1
)

echo(
echo === [2/4] Clean previous build ===
if exist "build" rmdir /s /q "build"
if exist "dist"  rmdir /s /q "dist"

echo(
echo === [3/4] PyInstaller (scripts\cas.spec -^> dist\cas\) ===
%PY% -m PyInstaller --noconfirm --clean scripts\cas.spec || (
    echo ERROR: PyInstaller build failed. & exit /b 1
)

if not exist "dist\cas\cas-gui.exe" (echo ERROR: cas-gui.exe not produced. & exit /b 1)
if not exist "dist\cas\cas.exe"     (echo ERROR: cas.exe not produced.     & exit /b 1)

echo(
echo === [4/4] DONE. Bundle is at: dist\cas\ ===
echo(
echo   dist\cas\cas-gui.exe   the windowed GUI   ( = python -m cas )
echo   dist\cas\cas.exe       the console CLI    ( = python -m cas.cli )
echo   dist\cas\provision\root\*.sh   bundled device-side scripts (read-only)
echo   dist\cas\_internal\            Python runtime + Tcl/Tk (do not touch)
echo(
echo ----------------------------------------------------------------------------
echo  OPERATOR POST-BUILD STEPS  -  create these EXTERNAL dirs BESIDE the exes:
echo ----------------------------------------------------------------------------
echo(
echo   The exes read profiles and adb/fastboot from the folder they live in
echo   (APPDIR), NOT from inside the bundle. After copying dist\cas\ to the rig:
echo(
echo   1) profiles\      -- OPTIONAL. The library is a local/external "CAS Profiles"
echo                        folder set once via Settings -^> Library folder...
echo                        (a local profiles\ here is only the fallback when
echo                        unset). If you want one: ~7.1 GB,
echo                        WRITABLE (capture/new/delete operate on it). Layout:
echo                                  dist\cas\profiles\<name>\profile.meta
echo                                  dist\cas\profiles\<name>\manifest
echo                                  dist\cas\profiles\<name>\golden_root_payload\
echo(
echo   2) platform-tools\  -- put adb.exe + fastboot.exe (and AdbWinApi.dll,
echo                          AdbWinUsbApi.dll, libwinpthread-1.dll) here:
echo                          dist\cas\platform-tools\adb.exe
echo                          dist\cas\platform-tools\fastboot.exe
echo                        Both exes auto-detect this dir; the GUI also falls back
echo                        to a legacy windows-kit\ dir, then adb on PATH. To override
echo                        explicitly (either exe), e.g.:
echo                          cas.exe --adb platform-tools\adb.exe ^
echo                                  --fastboot platform-tools\fastboot.exe list
echo(
echo   Final layout:
echo       dist\cas\
echo         |- cas-gui.exe
echo         |- cas.exe
echo         |- _internal\            (runtime; bundled provision\root\*.sh lives under here)
echo         |- profiles\             (EXTERNAL, writable)   <- you add this
echo         \- platform-tools\       (EXTERNAL, adb/fastboot) <- you add this
echo(
echo ----------------------------------------------------------------------------
echo  SmartScreen / unsigned-binary warning:
echo ----------------------------------------------------------------------------
echo   These exes are UNSIGNED, so Windows SmartScreen will show
echo   "Windows protected your PC" on first run. The operator clicks
echo   "More info" -^> "Run anyway". To suppress it, sign both exes with an
echo   Authenticode cert (EV cert clears SmartScreen reputation instantly):
echo(
echo     signtool sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 ^
echo         /a dist\cas\cas-gui.exe dist\cas\cas.exe
echo(
echo   (signtool ships with the Windows SDK. Run it AFTER this build, BEFORE
echo    zipping/distributing dist\cas\.)
echo(

endlocal
