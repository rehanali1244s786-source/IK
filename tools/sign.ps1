# Authenticode-signs a file with the certificate in the CERT_B64 (base64 .pfx) / CERT_PASSWORD environment variables.
# Signing removes most "Windows protected your PC" (SmartScreen) and antivirus false alarms for new releases.
param([Parameter(Mandatory = $true)][string]$File)
$ErrorActionPreference = "Stop"
$pfx = Join-Path $env:RUNNER_TEMP "cert.pfx"
[IO.File]::WriteAllBytes($pfx, [Convert]::FromBase64String($env:CERT_B64))
try {
  $signtool = Get-ChildItem "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" | Sort-Object FullName | Select-Object -Last 1
  if (-not $signtool) { throw "signtool.exe not found" }
  & $signtool.FullName sign /f $pfx /p $env:CERT_PASSWORD /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 $File
  if ($LASTEXITCODE -ne 0) { throw "signtool failed" }
  & $signtool.FullName verify /pa $File
} finally {
  Remove-Item $pfx -Force -ErrorAction SilentlyContinue
}
