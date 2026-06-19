# console-auto-setup - Windows engine (PowerShell). Mirror of run.sh; reads the same
# lib/emulators.txt + recipes/*.txt. Invoke via run.bat <mode> [args].
param(
  [Parameter(Position=0)][string]$Mode  = 'inspect',
  [Parameter(Position=1)][string]$Arg1,
  [Parameter(Position=2)][string]$Arg2,
  [Parameter(Position=3)][string]$Arg3
)
$ErrorActionPreference = 'Continue'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# --- locate adb (bundled in odin-provisioning, else next to us, else PATH) ---
$adb = 'adb'
foreach ($c in @("$root\..\odin-provisioning\platform-tools\adb.exe", "$root\platform-tools\adb.exe")) {
  if (Test-Path $c) { $adb = $c; break }
}

function Dev   ([string]$c) { (& $adb shell $c) 2>$null }
function Dev1  ([string]$c) { ((& $adb shell $c) 2>$null | Out-String).Trim() }
function DevExists([string]$p) { (Dev1 "[ -e '$p' ] && echo Y") -eq 'Y' }
function Need-Device {
  & $adb get-state *>$null
  if ($LASTEXITCODE -ne 0) { Write-Host "[X] No device. Plug in the Odin, enable USB debugging, tap Allow."; exit 1 }
}
function Detect-Sd { Dev1 'for d in /storage/*-*; do [ -d "$d" ] && echo "$d" && break; done' }
function Data-Access {
  if ([string]::IsNullOrEmpty((Dev1 "ls /sdcard/Android/data 2>/dev/null"))) { return 'none' }
  if ((Dev1 "echo ok > /sdcard/Android/data/.cas 2>/dev/null && cat /sdcard/Android/data/.cas 2>/dev/null && rm -f /sdcard/Android/data/.cas 2>/dev/null") -match 'ok') { return 'rw' }
  return 'ro'
}
function Get-Emu([string]$name) {
  foreach ($line in Get-Content "$root\lib\emulators.txt") {
    if (-not $line.Trim()) { continue }
    $f = $line.Split(';')
    if ($f[0] -ieq $name) {
      return [pscustomobject]@{
        Name = $f[0]; Pkgs = $f[1]; Where = $f[2]
        Cfgs = $(if ($f[3] -eq '-') { '' } else { $f[3] })
        KeyRe = $(if ($f[4] -eq '-') { '' } else { $f[4] })
      }
    }
  }
  return $null
}
function Resolve-Pkg([string]$pkgs) {
  foreach ($p in $pkgs.Split(' ')) {
    if (-not $p) { continue }
    & $adb shell pm path $p *>$null
    if ($LASTEXITCODE -eq 0) { return $p }
  }
  return ''
}
function Cfg-Path($e, $pkg, $sd) {
  foreach ($rel in $e.Cfgs.Split(' ')) {
    if (-not $rel) { continue }
    $p = if ($e.Where -eq 'SD') { "$sd/$rel" } elseif ($e.Where -eq 'DATADATA') { "/data/data/$pkg/$rel" } else { "/sdcard/Android/data/$pkg/$rel" }
    if (DevExists $p) { return $p }
  }
  return ''
}
function Stamp { Get-Date -Format 'yyyy-MM-dd_HHmmss' }
function Fmt-Size([long]$b) { if ($b -ge 1MB) { "{0:N1} MB" -f ($b / 1MB) } elseif ($b -ge 1KB) { "{0:N0} KB" -f ($b / 1KB) } else { "$b B" } }

# ============================ MODES ============================
function Mode-Inspect {
  $sd = Detect-Sd
  $report = "results\inspect_$(Stamp).txt"
  $out = New-Object System.Collections.ArrayList
  function L($s) { Write-Host $s; [void]$out.Add($s) }
  L "###### INSPECT ######"
  L ("model: {0}  device: {1}  android: {2}" -f (Dev1 'getprop ro.product.model'), (Dev1 'getprop ro.product.device'), (Dev1 'getprop ro.build.version.release'))
  L ("gpu:   {0}" -f (Dev1 "dumpsys SurfaceFlinger 2>/dev/null | grep -m1 -iE 'GLES|Adreno|Mali'"))
  L ("sd:    {0}" -f $sd)
  L ("android-data-access: {0}   [rw=file methods work | ro/none=need root or backup/restore]" -f (Data-Access))
  L "------------------------------------------------------------"
  foreach ($line in Get-Content "$root\lib\emulators.txt") {
    if (-not $line.Trim()) { continue }
    $e = Get-Emu ($line.Split(';')[0])
    $pkg = Resolve-Pkg $e.Pkgs
    if (-not $pkg) { L ("  {0,-12} [not installed]" -f $e.Name); continue }
    if ($e.Where -eq 'DATADATA') { L ("  {0,-12} pkg={1}`n      TYPE: DATA-DATA (root-only)  -> Method D (backup/restore) or temp-root" -f $e.Name, $pkg); continue }
    $cfg = Cfg-Path $e $pkg $sd
    if (-not $cfg) { L ("  {0,-12} pkg={1}`n      TYPE: NO CONFIG yet -> open app + add the game folder, then re-inspect" -f $e.Name, $pkg); continue }
    $body = (Dev "cat '$cfg' 2>/dev/null") | Out-String
    if ($body -match 'content://') { $typ = 'SAF content:// URI'; $rec = "Method A won't stick. Use B (clone grant, root) or C (re-pick once/unit)." }
    elseif ($body -match '/storage/([0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}|emulated)') { $typ = 'PLAIN /storage path'; $rec = 'Method A works: clone config + rewrite the SD serial. Best case.' }
    else { $typ = 'config present, game-folder line not obvious'; $rec = "run: run.bat getcfg $($e.Name)" }
    L ("  {0,-12} pkg={1}" -f $e.Name, $pkg)
    L ("      cfg:  {0}" -f $cfg)
    L ("      TYPE: {0}" -f $typ)
    L ("      ->    {0}" -f $rec)
    ($body -split "`n" | Select-String -Pattern 'content://|/storage/' | Select-Object -First 4) | ForEach-Object { L ("        | " + $_.ToString().Trim()) }
  }
  $out | Set-Content -Encoding UTF8 $report
  Write-Host ""; Write-Host "[saved] $report"
}

function Mode-Getcfg {
  $e = Get-Emu $Arg1; if (-not $e) { Write-Host "usage: getcfg <emu>"; return }
  $sd = Detect-Sd; $pkg = Resolve-Pkg $e.Pkgs
  if (-not $pkg) { Write-Host "$Arg1 not installed"; return }
  $found = $false
  foreach ($rel in $e.Cfgs.Split(' ')) {
    if (-not $rel) { continue }
    $p = if ($e.Where -eq 'SD') { "$sd/$rel" } elseif ($e.Where -eq 'DATADATA') { "/data/data/$pkg/$rel" } else { "/sdcard/Android/data/$pkg/$rel" }
    if (DevExists $p) { $found = $true; Write-Host "== $Arg1 :: $p =="; Dev "cat '$p' 2>/dev/null" }
  }
  if (-not $found) { Write-Host "no config file found for $Arg1 (open the app + set it up first)" }
}

function Mode-Setpath {
  $e = Get-Emu $Arg1; if (-not $e -or -not $Arg2 -or -not $Arg3) { Write-Host "usage: setpath <emu> <key> <value>"; return }
  $sd = Detect-Sd; $pkg = Resolve-Pkg $e.Pkgs
  if (-not $pkg) { Write-Host "$Arg1 not installed"; return }
  $cfg = Cfg-Path $e $pkg $sd
  if (-not $cfg) { Write-Host "no config file for $Arg1"; return }
  New-Item -ItemType Directory -Force -Path "results\edit" *>$null
  $orig = "results\edit\$($Arg1)_$(Stamp).orig"; $new = "results\edit\$($Arg1).new"
  & $adb pull $cfg $orig *>$null
  if (-not (Test-Path $orig)) { Write-Host "[X] pull failed (access?)"; return }
  $lines = Get-Content $orig
  $rx = "^\s*$([regex]::Escape($Arg2))\s*="
  if ($lines -match $rx) { $lines = $lines -replace "$rx.*", "$Arg2 = $Arg3" }
  else { $lines += "$Arg2 = $Arg3" }
  $lines | Set-Content -Encoding UTF8 $new
  & $adb push $new $cfg *>$null
  if ($LASTEXITCODE -ne 0) { Write-Host "[X] push failed (android-data may be read-only)"; return }
  Write-Host "[set] $Arg1 :: $Arg2 = $Arg3  ->  $cfg"
  Write-Host "      original saved: $orig . Relaunch the emulator and check the game list."
}

function Mode-Grant {
  $e = Get-Emu $Arg1; if (-not $e) { Write-Host "usage: grant <emu>"; return }
  $pkg = Resolve-Pkg $e.Pkgs; if (-not $pkg) { Write-Host "$Arg1 not installed"; return }
  Dev "appops set $pkg MANAGE_EXTERNAL_STORAGE allow" | Out-Null
  Write-Host "[grant] All-Files-Access -> $pkg"
}

function Mode-RootCheck {
  & $adb root *>$null; & $adb wait-for-device *>$null
  $uid = Dev1 'id -u'
  Write-Host "temp-root (adb root):  $(if($uid -eq '0'){'YES'}else{'no'})   (id -u = $uid)"
  Write-Host "android-data access:   $(Data-Access)"
  $dd = if ([string]::IsNullOrEmpty((Dev1 'ls /data/data 2>/dev/null'))) { 'no' } else { 'yes' }
  Write-Host "/data/data readable:   $dd"
  if ($uid -eq '0') { Write-Host "-> ROOT: Method B (clone SAF grants) + /data/data read/write available." }
  else { Write-Host "-> No root: use file methods (rw), or backup/restore for /data/data apps." }
  & $adb unroot *>$null
}

function Backup-Verdict([long]$sz) {
  if ($sz -gt 102400) { return 'OK (real data)' }
  elseif ($sz -gt 0)  { return 'TINY (firmware likely strips it)' }
  else                { return 'EMPTY (no prompt tapped, or backup disabled)' }
}
function Mode-Backup {
  $e = Get-Emu $Arg1; if (-not $e) { Write-Host "usage: backup <emu>"; return }
  $pkg = Resolve-Pkg $e.Pkgs; if (-not $pkg) { Write-Host "$Arg1 not installed"; return }
  New-Item -ItemType Directory -Force -Path "results\backup" *>$null
  $out = "results\backup\$($Arg1).ab"
  Write-Host "On the Odin a 'Full backup' screen should appear - leave password EMPTY, tap 'Back up my data'."
  & $adb backup -apk -f $out $pkg
  $sz = if (Test-Path $out) { (Get-Item $out).Length } else { 0 }
  Write-Host "[backup] $out  =  $(Fmt-Size $sz)  ->  $(Backup-Verdict $sz)"
}
function Mode-BackupAll {
  New-Item -ItemType Directory -Force -Path "results\backup" *>$null
  $report = "results\backup\report_$(Stamp).txt"
  $rows = New-Object System.Collections.ArrayList
  function LB($s) { Write-Host $s; [void]$rows.Add($s) }
  LB "== adb backup report (-apk = app + its data) =="
  LB "You must tap 'Back up my data' (password EMPTY) on the Odin for EACH app below."
  LB ("{0,-12} {1,-40} {2,12}  {3}" -f 'emulator', 'package', 'size', 'status')
  LB ("-" * 84)
  foreach ($line in Get-Content "$root\lib\emulators.txt") {
    if (-not $line.Trim()) { continue }
    $e = Get-Emu ($line.Split(';')[0])
    $pkg = Resolve-Pkg $e.Pkgs
    if (-not $pkg) { LB ("{0,-12} {1,-40} {2,12}  {3}" -f $e.Name, '(not installed)', '', 'SKIP'); continue }
    $out = "results\backup\$($e.Name).ab"
    Write-Host ""
    Write-Host ">>> $($e.Name): tap 'Back up my data' on the Odin now (password empty)..."
    & $adb backup -apk -f $out $pkg
    $sz = if (Test-Path $out) { (Get-Item $out).Length } else { 0 }
    $status = if ($sz -gt 102400) { 'OK' } elseif ($sz -gt 0) { 'TINY (stripped?)' } else { 'EMPTY' }
    LB ("{0,-12} {1,-40} {2,12}  {3}" -f $e.Name, $pkg, (Fmt-Size $sz), $status)
  }
  $rows | Set-Content -Encoding UTF8 $report
  Write-Host ""; Write-Host "[saved] $report"
  Write-Host "OK = real backup captured.  EMPTY/TINY = this firmware blocks adb backup for that app."
  Write-Host "Reminder: even an OK backup does NOT carry the SAF game-folder mapping (that lives in /data/system)."
}
function Mode-RestoreAll {
  $abs = Get-ChildItem "results\backup\*.ab" -ErrorAction SilentlyContinue
  if (-not $abs) { Write-Host "no .ab files in results\backup - run 'backup-all' first"; return }
  foreach ($ab in $abs) {
    Write-Host ">>> restoring $($ab.Name): tap 'Restore my data' on the Odin (password empty)..."
    & $adb restore $ab.FullName
  }
  Write-Host "[restore-all] done. Re-inspect / launch to confirm what carried."
}
function Mode-CloneTest {
  # Single-unit proof: simulate a Method-A clone onto a FRESH unit, for ONE emulator, with no
  # factory reset. capture files -> pm clear (wipes data + SAF folder grant) -> push files back ->
  # grant All-Files. Then you launch via ES-DE: if it boots, Method A + ES-DE = plug-and-play.
  $e = Get-Emu $Arg1
  if (-not $e) { Write-Host "usage: clonetest <emu>   (try 'flycast' first - small, SAF, no BIOS)"; return }
  if ($e.Where -ne 'ADATA') { Write-Host "clonetest needs an emulator whose config is in Android/data (where=ADATA). '$Arg1' is $($e.Where) - not suitable."; return }
  $pkg = Resolve-Pkg $e.Pkgs; if (-not $pkg) { Write-Host "$Arg1 not installed"; return }
  $src = "/sdcard/Android/data/$pkg/files"
  if (-not (DevExists $src)) { Write-Host "$Arg1 has no files/ on the device - is it set up?"; return }
  $save = "results\clonetest\$Arg1"
  New-Item -ItemType Directory -Force -Path $save *>$null
  Write-Host "=== CLONE TEST: $Arg1 ($pkg) ==="
  Write-Host "This SIMULATES cloning onto a fresh unit (no factory reset):"
  Write-Host "  1) capture $Arg1's files   2) pm clear (wipes its data + SAF folder grant)"
  Write-Host "  3) push the files back (= Method A clone)   4) grant All-Files-Access"
  Write-Host "Captured config is saved to $save\ and re-pushed, so $Arg1 is restored either way."
  $ok = Read-Host "Proceed? This briefly clears $Arg1 (config restored after). Type Y"
  if ($ok -ne 'Y') { Write-Host "cancelled."; return }

  # pick the cfg file we will byte-verify after the push (first recipe cfg present in the capture)
  function Get-VerifyRel {
    foreach ($r in $e.Cfgs.Split(' ')) {
      if ($r -and (Test-Path (Join-Path $save ('files\' + (($r -replace '^files/', '') -replace '/', '\'))))) { return $r }
    }
    return ''
  }
  function Prime-App {
    # after pm clear, a shell-made Android/data/<pkg> dir is useless on Android 13 (FUSE rejects
    # the file writes silently) - launching the app once makes Android recreate it app-owned
    & $adb shell monkey -p $pkg -c android.intent.category.LAUNCHER 1 *>$null
    Start-Sleep -Seconds 4
    Dev "am force-stop $pkg" | Out-Null
  }
  function Push-Back {
    & $adb shell "mkdir -p /sdcard/Android/data/$pkg" 2>$null
    & $adb push "$save\files" "/sdcard/Android/data/$pkg/" 2>&1 | Select-Object -Last 1 | ForEach-Object { Write-Host "      $_" }
  }
  function Get-DevSize([string]$p) { Dev1 "wc -c < '$p' 2>/dev/null" }

  Write-Host "[1/5] capturing $src ..."
  Dev "am force-stop $pkg" | Out-Null
  & $adb pull $src $save *>$null
  if (-not (Test-Path "$save\files")) { Write-Host "[X] capture failed - NOT clearing anything. Aborted."; return }

  Write-Host "[2/5] pm clear $pkg  (wipes app data + revokes its SAF folder grant = 'fresh unit')..."
  & $adb shell pm clear $pkg

  Write-Host "[3/5] priming: launching $Arg1 once so Android recreates its data dirs, then stopping it..."
  Prime-App

  Write-Host "[4/5] pushing captured files back (the Method-A clone)..."
  Push-Back
  $rel = Get-VerifyRel
  if ($rel) {
    $loc = Join-Path $save ('files\' + (($rel -replace '^files/', '') -replace '/', '\'))
    $chk = "/sdcard/Android/data/$pkg/$rel"
    $want = (Get-Item $loc).Length
    $devSz = Get-DevSize $chk
    $landed = ($devSz -match '^\d+$') -and ([long]$devSz -eq $want)
    if (-not $landed) {
      Write-Host "[!] push did NOT land ($chk on device = '$devSz' bytes, expected $want). Retrying once..."
      Prime-App; Push-Back
      $devSz = Get-DevSize $chk
      $landed = ($devSz -match '^\d+$') -and ([long]$devSz -eq $want)
    }
    if (-not $landed) {
      Write-Host "[X] CLONE DID NOT LAND - this test run is VOID, do not judge it."
      Write-Host "    The capture is safe in $save\ - paste this whole output back so we see why the push is rejected."
      return
    }
    Write-Host "[ok] clone verified on device ($chk = $devSz bytes). Key line(s):"
    $pat = if ($e.KeyRe) { $e.KeyRe } else { 'content://|/storage/' }
    (Dev "cat '$chk' 2>/dev/null") -split "`n" | Select-String -Pattern $pat | Select-Object -First 2 | ForEach-Object { Write-Host ("      | " + $_.ToString().Trim()) }
  } else {
    Write-Host "[!] no cfg file in the capture to verify against - check manually: run.bat getcfg $Arg1"
  }

  Write-Host "[5/5] granting All-Files-Access..."
  Dev "appops set $pkg MANAGE_EXTERNAL_STORAGE allow" | Out-Null

  Write-Host ""
  Write-Host ">>> IMPORTANT: do NOT open $Arg1 itself first - its own game list SHOULD look empty"
  Write-Host "    (the SAF folder grant is gone; that is exactly what a fresh cloned unit looks like)."
  Write-Host ">>> NOW open ES-DE on the Odin and launch a $Arg1 game FROM ES-DE:"
  Write-Host "    BOOTS & PLAYS  -> Method A clone is ENOUGH; ES-DE launch doesn't need the SAF grant"
  Write-Host "                      => plug-and-play works with no root. This is the win."
  Write-Host "    WON'T LOAD     -> this emulator needs its folder re-picked once per unit (Method C)."
  Write-Host "    (Config stays captured in $save\ either way; re-pick the folder in-app afterwards"
  Write-Host "     if you want $Arg1's own in-app browser back.)"
}

function Mode-Restore {
  $e = Get-Emu $Arg1; if (-not $e) { Write-Host "usage: restore <emu>"; return }
  $out = "results\backup\$($Arg1).ab"
  if (-not (Test-Path $out)) { Write-Host "no backup at $out - run: run.bat backup $Arg1 first"; return }
  $pkg = Resolve-Pkg $e.Pkgs; if ($pkg) { Dev "am force-stop $pkg" | Out-Null }
  Write-Host "On the Odin: leave password EMPTY, tap 'Restore my data'. Wait for the DEVICE to finish."
  & $adb restore $out
  Write-Host "[restore] done. Re-inspect / launch to confirm."
}

function Mode-Checklist {
  $rf = "$root\recipes\$Arg1.txt"
  if (-not $Arg1 -or -not (Test-Path $rf)) {
    $have = (Get-ChildItem "$root\recipes" -Filter *.txt -ErrorAction SilentlyContinue | ForEach-Object { $_.BaseName }) -join ' '
    Write-Host "usage: checklist <emu>   (have: $have)"; return
  }
  $e = Get-Emu $Arg1; $sd = Detect-Sd; $pkg = Resolve-Pkg $e.Pkgs
  if (-not $pkg) { Write-Host "$Arg1 not installed"; return }
  $report = "results\checklist_$($Arg1)_$(Stamp).txt"
  $out = New-Object System.Collections.ArrayList
  function L2($s) { Write-Host $s; [void]$out.Add($s) }
  L2 "###### SETUP CHECKLIST: $Arg1 ($pkg) ######"
  L2 ("android-data: {0}   sd: {1}" -f (Data-Access), $sd)
  L2 "------------------------------------------------------------"
  foreach ($line in Get-Content $rf) {
    if (-not $line.Trim()) { continue }
    $f = $line.Split(';'); $label = $f[0]; $kind = $f[1]; $target = $f[2]; $hint = $f[3]
    if ($target -eq '-') { $target = '' }
    $st = '?'
    switch ($kind) {
      'file' {
        $p = if ($target -like '/*') { $target } else { "/sdcard/Android/data/$pkg/$target" }
        $st = if (DevExists $p) { 'PASS' } else { 'MISSING' }
      }
      'filedir' {
        $p = if ($target -like '/*') { $target } else { "/sdcard/Android/data/$pkg/$target" }
        $n = Dev1 "ls '$p' 2>/dev/null | grep -c ."
        $st = if ([int]($n -as [int]) -gt 0) { "PASS ($n items)" } else { 'MISSING' }
      }
      'map' {
        $cfg = Cfg-Path $e $pkg $sd
        if ($cfg) {
          $v = (Dev "cat '$cfg' 2>/dev/null") -split "`n" | Select-String -Pattern $target | Select-String -Pattern 'content://|/storage/' | Select-Object -First 1
          if ($v -match 'content://') { $st = 'SET (SAF - re-pick or grant per unit)' }
          elseif ($v) { $st = 'SET (plain - clones cleanly)' }
          else { $st = 'NOT MAPPED' }
        } else { $st = 'no cfg' }
      }
      'show' {
        $cr, $pat = $target -split '::', 2
        $p = if ($cr -like '/*') { $cr } else { "/sdcard/Android/data/$pkg/$cr" }
        $v = ((Dev "cat '$p' 2>/dev/null") -split "`n" | Select-String -Pattern $pat | Select-Object -First 3) -join ' ; '
        $st = if ($v) { "now: $v" } else { 'key not found' }
      }
      'setting' {
        $parts = $target -split '::'; $cr = $parts[0]; $k = $parts[1]; $exp = $parts[2]
        $p = if ($cr -like '/*') { $cr } else { "/sdcard/Android/data/$pkg/$cr" }
        $cur = (Dev "cat '$p' 2>/dev/null") -split "`n" | Select-String -Pattern "^\s*$k\s*=" | Select-Object -First 1
        if ($cur -and $cur.ToString().Contains($exp)) { $st = 'PASS' }
        elseif ($cur) { $st = "WRONG: $($cur.ToString().Trim())" }
        else { $st = 'not set' }
      }
      'perm'   { $st = "run: run.bat grant $Arg1" }
      'manual' { $st = 'MANUAL' }
    }
    L2 ("  [ {0,-26} ] {1}" -f $st, $label)
    if ($hint) { L2 ("          -> {0}" -f $hint) }
  }
  $out | Set-Content -Encoding UTF8 $report
  Write-Host ""; Write-Host "[saved] $report"
}

# ============================ DISPATCH ============================
try { & $adb version *>$null } catch { Write-Host "[X] adb not found. Put platform-tools beside this folder or on PATH."; exit 1 }
Need-Device
New-Item -ItemType Directory -Force -Path "results" *>$null
switch ($Mode.ToLower()) {
  'inspect'    { Mode-Inspect }
  'getcfg'     { Mode-Getcfg }
  'setpath'    { Mode-Setpath }
  'grant'      { Mode-Grant }
  'root-check' { Mode-RootCheck }
  'rootcheck'  { Mode-RootCheck }
  'backup'     { Mode-Backup }
  'backup-all' { Mode-BackupAll }
  'restore'    { Mode-Restore }
  'restore-all'{ Mode-RestoreAll }
  'clonetest'  { Mode-CloneTest }
  'checklist'  { Mode-Checklist }
  default      { Write-Host "unknown mode: $Mode"; Write-Host "modes: inspect getcfg checklist setpath grant clonetest backup backup-all restore restore-all root-check" }
}
