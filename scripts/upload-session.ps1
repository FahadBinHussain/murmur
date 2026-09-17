# upload-session.ps1 - upload the local WhatsApp session store into Murmur's Neon DB
# (table whatsapp_sessions, read by the murmur bridge on startup / after a re-pair).
#
# usage:
#   pwsh scripts/upload-session.ps1                              # uses WACLI_STORE_PATH
#   pwsh scripts/upload-session.ps1 -StorePath "C:\path\to\store"
# env: repo-root .env (already-set env vars win) - DATABASE_URL (Neon murmur DSN,
# same key the Go bridge reads), WACLI_STORE_PATH (default store).
#
# GOTCHA: psql to Neon over port 5432 is silently dropped while ProtonVPN is up
# (the IDMWFP WFP driver kills non-tunnel flows - TCP connects but the SSLRequest
# never gets an answer, so this hangs/fails with no useful error). run with the VPN
# off, or port the insert to the wss serverless driver like scripts/bnp-db.mjs.
#
# after a successful upload, restart the space so the bridge reloads the session:
#   hf spaces restart fahadbinhussain/murmur

param(
    [string]$StorePath = ""
)

$ErrorActionPreference = "Stop"

# load repo-root .env (env vars already set win, same loader convention as murmur.ps1)
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$RepoRoot = if ($ScriptDir -match '\\scripts$') { Split-Path -Parent $ScriptDir } else { $ScriptDir }
$EnvFile = Join-Path $RepoRoot '.env'
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -match '^\s*#') { return }
        if ($line -match '^(\w+)\s*=\s*(.*)$') {
            if (-not [Environment]::GetEnvironmentVariable($Matches[1])) {
                [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2].Trim(), "Process")
            }
        }
    }
} else {
    Write-Host "WARNING: no .env found at $EnvFile" -ForegroundColor Yellow
}

if (-not $StorePath) {
    $StorePath = if ($env:WACLI_STORE_PATH) { $env:WACLI_STORE_PATH.Trim() } else { "" }
}
if (-not $StorePath) {
    Write-Host "ERROR: no -StorePath given and WACLI_STORE_PATH is empty (set it in .env)" -ForegroundColor Red
    exit 1
}

if (-not $env:DATABASE_URL) {
    Write-Host "ERROR: DATABASE_URL not set (env or repo-root .env) - the murmur Neon DSN" -ForegroundColor Red
    exit 1
}

$sessionPath = Join-Path $StorePath "session.db"
$wacliPath = Join-Path $StorePath "wacli.db"

if (-not (Test-Path $sessionPath)) {
    Write-Host "ERROR: session.db not found at $sessionPath" -ForegroundColor Red
    exit 1
}

$sessionBytes = [System.IO.File]::ReadAllBytes($sessionPath)
Write-Host "session.db: $($sessionBytes.Length) bytes" -ForegroundColor Cyan

$wacliBytes = @()
if (Test-Path $wacliPath) {
    $wacliBytes = [System.IO.File]::ReadAllBytes($wacliPath)
    Write-Host "wacli.db:   $($wacliBytes.Length) bytes" -ForegroundColor Cyan
} else {
    Write-Host "wacli.db:   not found (skipping)" -ForegroundColor Yellow
}

$sessionHex = ($sessionBytes | ForEach-Object { $_.ToString("x2") }) -join ""
$wacliHex = if ($wacliBytes.Length -gt 0) { ($wacliBytes | ForEach-Object { $_.ToString("x2") }) -join "" } else { "" }

$sql = @"
INSERT INTO whatsapp_sessions (id, session_data, wacli_data, updated_at)
VALUES ('default', decode('$sessionHex', 'hex'), $(if ($wacliHex) { "decode('$wacliHex', 'hex')" } else { "NULL" }), NOW())
ON CONFLICT (id) DO UPDATE SET
    session_data = decode('$sessionHex', 'hex'),
    wacli_data = $(if ($wacliHex) { "decode('$wacliHex', 'hex')" } else { "NULL" }),
    updated_at = NOW();
"@

Write-Host "`nUploading to Neon..." -ForegroundColor Green
$sql | psql $env:DATABASE_URL 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-Host "Done! Session uploaded to Neon." -ForegroundColor Green
    Write-Host "Restart the HF Space to pick up the new session:" -ForegroundColor Yellow
    Write-Host "  hf spaces restart fahadbinhussain/murmur" -ForegroundColor White
} else {
    Write-Host "Upload failed (exit code $LASTEXITCODE)" -ForegroundColor Red
    Write-Host "If ProtonVPN is up, psql to Neon over 5432 is silently dropped - retry with the VPN off." -ForegroundColor Yellow
}
