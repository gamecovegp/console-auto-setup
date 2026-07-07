<#
  install-drivers.ps1 — one-time, fleet-wide Windows driver setup for CAS on-device flashing.

  Publishes drivers to the Windows DRIVER STORE (pnputil /add-driver /install) so EVERY unit with a
  matching USB id auto-binds on plug — no per-device Zadig, ever. Two drivers:

    * fastboot / bootloader  (Retroid / AYN / Odin) — WinUSB on the Android fastboot interface so
      `fastboot devices` works. Standard id VID_18D1&PID_D00D (+ the fastboot interface class FF/42/03).
    * Qualcomm EDL 9008      (MANGMI, EDL/Firehose)  — a COM-port driver on VID_05C6&PID_9008, which
      CAS's Windows EDL backend needs (it locates the port by that USB id).

  For each driver it uses the most-trusted source available, in order:
    1) a real VENDOR driver dropped into drivers\vendor\<kind>\  (signed by the vendor — best)
    2) (fastboot only) Google's signed USB driver, downloaded if this bench is online
    3) the bundled FALLBACK inf, self-signed on THIS machine (works offline)

  WHY the fallback is safe to self-sign: the INF only maps a device to an IN-BOX Microsoft driver
  (winusb.sys / usbser.sys) — no third-party kernel binary is introduced — so signature enforcement on
  the .sys is already satisfied; only our catalog needs to be trusted, which we do by importing a
  self-signed cert into TrustedPublisher + Root. No Secure Boot / test-signing changes required.

  Run elevated (setup-windows.bat elevates for you). Idempotent. Installs PC-side drivers only; it does
  NOT flash, reboot, or modify any device.
#>
[CmdletBinding()]
param(
  [switch]$SkipFastboot,
  [switch]$SkipEdl
)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

function Assert-Admin {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  $p  = New-Object Security.Principal.WindowsPrincipal($id)
  if (-not $p.IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)) {
    throw "Must run as Administrator. Launch setup-windows.bat (it elevates for you)."
  }
}

function Add-DriverToStore([string]$inf, [string]$label) {
  Write-Host "[*] installing $label"
  Write-Host "    $inf"
  # /install also binds it to any matching CONNECTED device now; the store copy binds future units.
  $out = & pnputil.exe /add-driver "$inf" /install 2>&1
  $out | ForEach-Object { Write-Host "    $_" }
  $rc = $LASTEXITCODE
  # 0 ok; 259 = ERROR_NO_MORE_ITEMS (added to store, nothing connected to bind now); 3010 = reboot wanted.
  if ($rc -ne 0 -and $rc -ne 259 -and $rc -ne 3010) {
    throw "$label install failed (pnputil exit $rc)."
  }
  Write-Host "[ok] $label published to the Windows driver store."
}

function Get-SigningCert {
  # One reusable self-signed code-signing cert, trusted machine-wide, so pnputil accepts the
  # self-signed fallback catalogs silently. Created once; reused on every re-run.
  $subject = 'CN=CAS Driver Signing (self-signed, controlled fleet)'
  $cert = Get-ChildItem Cert:\LocalMachine\My -ErrorAction SilentlyContinue |
            Where-Object { $_.Subject -eq $subject } | Select-Object -First 1
  if (-not $cert) {
    Write-Host "[*] creating a self-signed driver-signing certificate (one-time)..."
    $cert = New-SelfSignedCertificate -Type Custom -Subject $subject -KeyUsage DigitalSignature `
              -CertStoreLocation Cert:\LocalMachine\My -NotAfter (Get-Date).AddYears(10) `
              -TextExtension @('2.5.29.37={text}1.3.6.1.5.5.7.3.3')  # EKU: Code Signing
    $cer = Join-Path $env:TEMP 'cas-driver-signing.cer'
    Export-Certificate -Cert $cert -FilePath $cer | Out-Null
    # Root -> chain validates; TrustedPublisher -> pnputil installs without an interactive trust prompt.
    Import-Certificate -FilePath $cer -CertStoreLocation Cert:\LocalMachine\Root | Out-Null
    Import-Certificate -FilePath $cer -CertStoreLocation Cert:\LocalMachine\TrustedPublisher | Out-Null
    Remove-Item $cer -ErrorAction SilentlyContinue
  }
  return $cert
}

function Sign-FallbackInf([string]$infPath) {
  # PnpLockdown=1 requires a valid signed catalog. Build + sign it with pure PowerShell
  # (New-FileCatalog) so no WDK / inf2cat is needed on the bench.
  $dir = Split-Path -Parent $infPath
  $cat = Join-Path $dir ([IO.Path]::GetFileNameWithoutExtension($infPath) + '.cat')
  if (Test-Path $cat) { Remove-Item $cat -Force }
  New-FileCatalog -Path $dir -CatalogFilePath $cat -CatalogVersion 2 | Out-Null
  $sig = Set-AuthenticodeSignature -FilePath $cat -Certificate (Get-SigningCert) -HashAlgorithm SHA256
  if ($sig.Status -ne 'Valid') { throw "could not sign $cat (status: $($sig.Status))." }
}

function Try-Vendor([string]$kind) {
  $vdir = Join-Path $here "vendor\$kind"
  if (-not (Test-Path $vdir)) { return $null }
  $inf = Get-ChildItem $vdir -Recurse -Filter *.inf -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($inf) { Write-Host "[i] using bundled vendor $kind driver: $($inf.Name)"; return $inf.FullName }
  return $null
}

function Try-DownloadGoogleUsb {
  # Google's own SIGNED USB driver — covers the VID_18D1 fastboot ids across brands. Stable URL.
  $url  = 'https://dl.google.com/android/repository/usb_driver_r13-windows.zip'
  $dest = Join-Path $here 'vendor\fastboot'
  try {
    Write-Host "[*] no vendor fastboot driver bundled; trying Google's signed driver online..."
    $zip = Join-Path $env:TEMP 'google_usb_driver.zip'
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing -TimeoutSec 60
    New-Item -ItemType Directory -Force -Path $dest | Out-Null
    Expand-Archive -Path $zip -DestinationPath $dest -Force
    Remove-Item $zip -ErrorAction SilentlyContinue
    $inf = Get-ChildItem $dest -Recurse -Filter android_winusb.inf -ErrorAction SilentlyContinue |
             Select-Object -First 1
    if ($inf) { Write-Host "[ok] downloaded Google USB driver."; return $inf.FullName }
  } catch {
    Write-Host "[!] could not download Google's driver ($($_.Exception.Message)); using the offline fallback."
  }
  return $null
}

function Install-Fastboot {
  $inf = Try-Vendor 'fastboot'
  if (-not $inf) { $inf = Try-DownloadGoogleUsb }
  if (-not $inf) {
    $inf = Join-Path $here 'fallback\fastboot\cas-fastboot.inf'
    Sign-FallbackInf $inf
    Write-Host "[i] using the bundled self-signed WinUSB fallback for fastboot."
  }
  Add-DriverToStore $inf 'fastboot (Android Bootloader / WinUSB)'
}

function Install-Edl {
  $inf = Try-Vendor 'edl'
  if (-not $inf) {
    $inf = Join-Path $here 'fallback\edl\cas-edl-9008.inf'
    Sign-FallbackInf $inf
    Write-Host "[i] using the bundled self-signed usbser fallback for EDL 9008."
    Write-Host "    (For the vendor QDLoader driver, drop its .inf into drivers\vendor\edl and re-run.)"
  }
  Add-DriverToStore $inf 'Qualcomm EDL 9008 (COM port)'
}

Assert-Admin
Write-Host "=== CAS Windows driver setup (one-time per PC; publishes to the driver store) ==="
if (-not $SkipFastboot) { Install-Fastboot } else { Write-Host "[skip] fastboot driver" }
if (-not $SkipEdl)      { Install-Edl }      else { Write-Host "[skip] EDL driver" }
Write-Host ""
Write-Host "[done] Drivers are in the Windows driver store. Every matching unit now auto-binds on plug —"
Write-Host "       no Zadig, no per-device setup. Unplug/replug the unit, then re-run (0) Root in CAS."
