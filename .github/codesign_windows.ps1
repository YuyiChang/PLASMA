# Codesign one or more Windows executables with a self-signed certificate,
# if one is configured. No-op (exits 0) otherwise — builds stay unsigned
# exactly as before until the WINDOWS_CERTIFICATE_* secrets are set.
#
# A self-signed cert does NOT stop SmartScreen's "Windows protected your PC"
# warning — SmartScreen trusts a certificate via Microsoft's reputation
# system, which only recognizes CA-issued certificates. What this buys
# instead: tamper-evidence, a consistent signer identity across releases,
# and the exact signing plumbing a real purchased cert would slot into
# later with zero pipeline changes. See
# docs/reference/release-process.md for how to generate one and set the
# secrets.
#
# Required env:
#   WINDOWS_CERTIFICATE_PFX_BASE64  base64 of a code-signing .pfx
#                                   (certificate + private key)
#   WINDOWS_CERTIFICATE_PASSWORD    that .pfx's export password
#
# Usage: codesign_windows.ps1 <file1> [file2] ...
$ErrorActionPreference = "Stop"

if (-not $env:WINDOWS_CERTIFICATE_PFX_BASE64) {
    Write-Host "No signing certificate configured (WINDOWS_CERTIFICATE_PFX_BASE64 unset) — skipping codesign, shipping unsigned as before."
    exit 0
}

$work = New-Item -ItemType Directory -Path (Join-Path $env:RUNNER_TEMP "codesign_windows") -Force
$pfxPath = Join-Path $work.FullName "cert.pfx"
[IO.File]::WriteAllBytes($pfxPath, [Convert]::FromBase64String($env:WINDOWS_CERTIFICATE_PFX_BASE64))

$signtool = (Get-ChildItem -Path "C:\Program Files (x86)\Windows Kits\10\bin" -Recurse -Filter "signtool.exe" -ErrorAction SilentlyContinue |
    Where-Object { $_.FullName -match "\\x64\\" } | Select-Object -First 1).FullName
if (-not $signtool) { throw "signtool.exe not found under the Windows 10 SDK on this runner" }

foreach ($file in $args) {
    # SHA256 file digest + RFC3161 timestamp (DigiCert's public timestamp
    # server, free to use regardless of who issued the signing cert) so the
    # signature stays valid after this certificate itself expires.
    & $signtool sign /f $pfxPath /p $env:WINDOWS_CERTIFICATE_PASSWORD `
        /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 $file
    if ($LASTEXITCODE -ne 0) { throw "signtool sign failed on $file" }
    Write-Host "Signed: $file"
}

# no `signtool verify /pa` here — that checks the full chain up to a
# trusted root, which a self-signed cert never satisfies by design; the
# sign step above having succeeded (and the exit-code check on it) is the
# real confirmation.

Remove-Item -Recurse -Force $work.FullName
