# Downloads the PINNED Deno version (tools/deno-version.txt) and verifies its SHA-256 against the checksum file that
# Deno publishes next to every release asset. Fails closed. Bump the version file on purpose, never "latest".
param([string]$Out = ".")
$ErrorActionPreference = "Stop"
$ver = (Get-Content (Join-Path $PSScriptRoot "deno-version.txt") -Raw).Trim()
$asset = "deno-x86_64-pc-windows-msvc.zip"
$base = "https://github.com/denoland/deno/releases/download/$ver"
$zip = Join-Path $Out "deno.zip"
Invoke-WebRequest "$base/$asset" -OutFile $zip
Invoke-WebRequest "$base/$asset.sha256sum" -OutFile "$zip.sha256sum"
$expected = ((Get-Content "$zip.sha256sum" -Raw).Trim() -split "\s+")[0].ToLower()
$actual = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
if ($expected -ne $actual) { throw "Deno $ver failed the SHA-256 check (expected $expected, got $actual)" }
Expand-Archive $zip -DestinationPath $Out -Force
Remove-Item $zip, "$zip.sha256sum"
Write-Host "Deno $ver verified and extracted."
