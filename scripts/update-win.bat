@echo off
REM ============================================================================
REM  update-win.bat  -  pull the latest CAS and rebuild the Windows exe
REM
REM  Run this on the bench whenever a new version has been pushed to GitHub:
REM     1) git pull (latest source + device scripts)
REM     2) build-win.bat  (regenerates dist\cas\cas-gui.exe + cas.exe)
REM     3) link the runtime dirs into dist\cas\ as JUNCTIONS — no multi-GB copy
REM
REM  The runtime dirs (profiles, retroarch-cores, ES-DE\downloaded_media,
REM  provision\root\firmware, windows-kit, Apps) are NOT in git; they live in THIS
REM  folder and are linked in. Apps\gamecove-companion.apk is the GameCove Companion
REM  app installed on every unit during provisioning (install_companion).
REM  folder and are linked into the freshly built dist\cas\. The golden library
REM  = the folder set in Settings -> Library folder (else local profiles\).
REM
REM  Prereqs (one-time): Python 3.14 + `py -3.14 -m pip install "pyinstaller>=6.11"`,
REM  plus git on PATH.
REM ============================================================================
setlocal EnableDelayedExpansion
cd /d "%~dp0.."   REM script lives in scripts\; operate from the repo root

echo(
echo === [1/3] git pull (latest source) ===
where git >nul 2>&1 || (echo ERROR: git not on PATH. & exit /b 1)
git pull --ff-only || (echo ERROR: git pull failed ^(local edits? run `git status`^). & exit /b 1)

echo(
echo === clearing old runtime links so the clean build can't recurse into them ===
if exist "dist\cas\windows-kit" rmdir "dist\cas\windows-kit" 2>nul
for %%D in (retroarch-cores profiles Apps) do if exist "dist\cas\data\%%D" rmdir "dist\cas\data\%%D" 2>nul
if exist "dist\cas\provision\root\firmware" rmdir "dist\cas\provision\root\firmware" 2>nul
if exist "dist\cas\data\ES-DE\downloaded_media" rmdir "dist\cas\data\ES-DE\downloaded_media" 2>nul

echo(
echo === [2/3] build ===
call scripts\build-win.bat || (echo ERROR: build failed. & exit /b 1)

echo(
echo === [3/3] link runtime dirs into dist\cas\ (junctions; skips any not present) ===
if exist "windows-kit" if not exist "dist\cas\windows-kit" ( mklink /J "dist\cas\windows-kit" "%CD%\windows-kit" >nul && echo   linked windows-kit )
if not exist "dist\cas\data" mkdir "dist\cas\data"
for %%D in (retroarch-cores profiles Apps) do (
  if exist "data\%%D" if not exist "dist\cas\data\%%D" ( mklink /J "dist\cas\data\%%D" "%CD%\data\%%D" >nul && echo   linked data\%%D )
)
if exist "provision\root\firmware" (
  if not exist "dist\cas\provision\root" mkdir "dist\cas\provision\root"
  if not exist "dist\cas\provision\root\firmware" ( mklink /J "dist\cas\provision\root\firmware" "%CD%\provision\root\firmware" >nul && echo   linked provision\root\firmware )
)
if exist "data\ES-DE\downloaded_media" (
  if not exist "dist\cas\data\ES-DE" mkdir "dist\cas\data\ES-DE"
  if not exist "dist\cas\data\ES-DE\downloaded_media" ( mklink /J "dist\cas\data\ES-DE\downloaded_media" "%CD%\data\ES-DE\downloaded_media" >nul && echo   linked data\ES-DE\downloaded_media )
)

echo(
echo === DONE — updated + rebuilt.  Run:  dist\cas\cas-gui.exe ===
echo   (golden library = the folder set in Settings -^> Library folder; else local profiles\)
endlocal
