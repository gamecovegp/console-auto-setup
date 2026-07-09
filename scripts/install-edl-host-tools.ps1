<#
  install-edl-host-tools.ps1 - drop the Windows EDL host tools into every MANGMI firmware payload.

  MANGMI units (AIR X / AIR X I2C / Pocket Max) are EDL-only: their bootloader cannot fastboot-flash
  init_boot, so CAS Root/Lock flashes them over EDL/Firehose using two HOST-side Qualcomm tools -
  QSaharaServer + fh_loader. The MANGMI vendor firmware ships only the LINUX builds of those tools, so on
  a Windows bench CAS refuses to flash with:

     "EDL firmware 'air-x' unusable: EDL flashing on Windows needs the Windows host tools
      QSaharaServer.exe + fh_loader.exe ... this build ships only the Linux binaries"

  The Windows .exe builds come from Qualcomm QPST/QFIL. Install QPST once (QPST.2.7.496.1.exe), then run
  this script: it finds QSaharaServer.exe + fh_loader.exe in the QPST bin folder and copies them beside
  the Linux tools in EVERY EDL payload under the CAS library's _firmware\ tree. Fleet-wide, idempotent,
  future-proof (any new EDL build or version dir is picked up automatically). Copies files only - it does
  NOT flash, reboot, or modify any device.

  USAGE
    .\install-edl-host-tools.ps1                         # auto-detect QPST bin + the library drive
    .\install-edl-host-tools.ps1 -Library "E:\CAS Profiles"
    .\install-edl-host-tools.ps1 -QpstBin "C:\Program Files (x86)\Qualcomm\QPST\bin" -Library "E:\CAS Profiles"
    .\install-edl-host-tools.ps1 -VerifyOnly            # report what WOULD be copied, change nothing
#>
[CmdletBinding()]
param(
  [string]$QpstBin = "",
  [string]$Library = "",
  [switch]$VerifyOnly
)
$ErrorActionPreference = 'Stop'
$tools = @("QSaharaServer.exe", "fh_loader.exe")

function Assert-Pe([string]$path) {
  # A Windows tool must start with the 'MZ' PE signature. Catches a Linux ELF renamed to .exe (0x7F ELF).
  $fs = [System.IO.File]::OpenRead($path)
  try {
    $b0 = $fs.ReadByte(); $b1 = $fs.ReadByte()
  } finally {
    $fs.Close()
  }
  if ($b0 -ne 0x4D -or $b1 -ne 0x5A) {
    throw "'$path' is not a Windows executable (no 'MZ' header) - is it the Linux build? Use the QPST .exe."
  }
}

# --- 1. locate QSaharaServer.exe + fh_loader.exe --------------------------------------------------------
$binCandidates = @()
if ($QpstBin -ne "") { $binCandidates += $QpstBin }
$binCandidates += @(
  "C:\Program Files (x86)\Qualcomm\QPST\bin",
  "C:\Program Files\Qualcomm\QPST\bin",
  "C:\Program Files (x86)\Qualcomm\QPST",
  "C:\Program Files\Qualcomm\QPST"
)
$srcMap = @{}
foreach ($bin in $binCandidates) {
  if (-not (Test-Path -LiteralPath $bin)) { continue }
  $ok = $true
  $found = @{}
  foreach ($t in $tools) {
    $hit = Get-ChildItem -LiteralPath $bin -Recurse -File -Filter $t -ErrorAction SilentlyContinue |
           Select-Object -First 1
    if ($hit) { $found[$t] = $hit.FullName } else { $ok = $false }
  }
  if ($ok) { $srcMap = $found; break }
}
if ($srcMap.Count -ne $tools.Count) {
  Write-Host "[!] Could not find both QSaharaServer.exe and fh_loader.exe." -ForegroundColor Yellow
  Write-Host "    Install QPST (QPST.2.7.496.1.exe), then pass the bin folder explicitly, e.g.:"
  Write-Host '      .\install-edl-host-tools.ps1 -QpstBin "C:\Program Files (x86)\Qualcomm\QPST\bin"'
  throw "EDL host tools not found."
}
foreach ($t in $tools) {
  Assert-Pe $srcMap[$t]
  Write-Host "[*] using $t"
  Write-Host "    $($srcMap[$t])"
}

# --- 2. locate the library's _firmware tree ------------------------------------------------------------
function Resolve-FirmwareRoot([string]$lib) {
  if ($lib -ne "") {
    if ((Split-Path -Leaf $lib) -ieq "_firmware" -and (Test-Path -LiteralPath $lib)) { return $lib }
    $p = Join-Path $lib "_firmware"
    if (Test-Path -LiteralPath $p) { return $p }
    throw "No _firmware folder under '$lib'. Point -Library at your CAS library root (the 'CAS Profiles' folder)."
  }
  # auto-detect: scan every filesystem drive for <root>\_firmware or <root>\CAS Profiles\_firmware
  foreach ($d in (Get-PSDrive -PSProvider FileSystem)) {
    foreach ($rel in @("_firmware", "CAS Profiles\_firmware")) {
      $p = Join-Path $d.Root $rel
      if (Test-Path -LiteralPath $p) { return $p }
    }
  }
  throw "Could not auto-detect the CAS library. Pass it: -Library `"E:\CAS Profiles`" (the folder with _firmware\ inside)."
}
$fwRoot = Resolve-FirmwareRoot $Library
Write-Host "[*] library firmware root:"
Write-Host "    $fwRoot"

# --- 3. every EDL payload = a folder containing the Linux 'QSaharaServer' (extension-less) --------------
$targets = Get-ChildItem -LiteralPath $fwRoot -Recurse -File -ErrorAction SilentlyContinue |
           Where-Object { $_.Name -eq "QSaharaServer" } |
           ForEach-Object { $_.DirectoryName } |
           Select-Object -Unique
if (-not $targets -or @($targets).Count -eq 0) {
  Write-Host "[!] No EDL payloads found under $fwRoot (looked for the Linux 'QSaharaServer' marker)." -ForegroundColor Yellow
  Write-Host "    Nothing to do. Is the correct library drive plugged in?"
  exit 0
}

# --- 4. copy the two .exe beside the Linux tools in each payload ----------------------------------------
$updated = 0
foreach ($dir in @($targets)) {
  $need = $false
  foreach ($t in $tools) {
    $dst = Join-Path $dir $t
    if (-not (Test-Path -LiteralPath $dst)) { $need = $true }
    elseif ((Get-Item -LiteralPath $dst).Length -ne (Get-Item -LiteralPath $srcMap[$t]).Length) { $need = $true }
  }
  if (-not $need) {
    Write-Host "[ok] already has both .exe: $dir"
    continue
  }
  Write-Host "[*] $(if ($VerifyOnly) {'WOULD update'} else {'updating'}): $dir"
  if (-not $VerifyOnly) {
    foreach ($t in $tools) {
      Copy-Item -LiteralPath $srcMap[$t] -Destination (Join-Path $dir $t) -Force
    }
  }
  $updated++
}

Write-Host ""
if ($VerifyOnly) {
  Write-Host "[ok] verify-only: $updated payload(s) would be updated, $((@($targets).Count) - $updated) already complete."
} else {
  Write-Host "[ok] done: $updated payload(s) updated, $((@($targets).Count)) EDL payload(s) total now carry the Windows EDL tools."
  Write-Host "     Re-run Root on the MANGMI unit - it should flash over EDL now."
}
