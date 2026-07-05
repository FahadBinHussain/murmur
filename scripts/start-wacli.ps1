# start-wacli.ps1 - wacli sync daemon with murmur webhook forwarding + localtunnel
# runs as a scheduled task. loads config from .env in repo root.
#
# usage: pwsh scripts/start-wacli.ps1
# expects to be run from repo root or scripts/ subdir.

param(
    [string]$StorePath = ""
)

$ErrorActionPreference = "Continue"

# ── resolve repo root ─────────────────────────────────────────────────────────
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$RepoRoot = if ($ScriptDir -match '\\scripts$') { Split-Path -Parent $ScriptDir } else { $ScriptDir }

# ── load .env ─────────────────────────────────────────────────────────────────
$EnvFile = Join-Path $RepoRoot '.env'
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | ForEach-Object {
        $line = $_.Trim()
        if ($line -match '^\s*#') { return }
        if ($line -match '^(\w+)\s*=\s*(.*)$') {
            $key = $Matches[1]
            $val = $Matches[2].Trim()
            if (-not [Environment]::GetEnvironmentVariable($key)) {
                [Environment]::SetEnvironmentVariable($key, $val)
            }
        }
    }
} else {
    Write-Host "WARNING: no .env found at $EnvFile" -ForegroundColor Yellow
}

# ── resolve config ────────────────────────────────────────────────────────────
$HfEmail = if ($env:HF_EMAIL) { $env:HF_EMAIL } else { "" }
$HfTokenRaw = if ($env:HF_TOKEN) { $env:HF_TOKEN.Trim() } else { "" }
$MurmurSpace = if ($env:MURMUR_HF_SPACE) { $env:MURMUR_HF_SPACE } else { "fahadbinhussain/murmur" }
$MurmurBase = if ($env:MURMUR_HF_SPACE_URL) { $env:MURMUR_HF_SPACE_URL.Trim().TrimEnd('/') } else { "https://fahadbinhussain-murmur.hf.space" }
$WebhookSecret = if ($env:WEBHOOK_SECRET) { $env:WEBHOOK_SECRET } else { "murmur-wa-2026" }
$ProxyPort = if ($env:PROXY_PORT) { [int]$env:PROXY_PORT } else { 7870 }

$WacliBin = if ($env:WACLI_BIN) { $env:WACLI_BIN } else { "C:\Users\Admin\go\bin\wacli.exe" }
if (-not $StorePath) {
    if ($env:WACLI_STORE_PATH) {
        $StorePath = $env:WACLI_STORE_PATH
    } else {
        $waAccounts = Get-ChildItem "$env:APPDATA\mainframe\accounts\whatsapp" -Directory -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($waAccounts) {
            $StorePath = "$env:APPDATA\mainframe\accounts\whatsapp\$($waAccounts.Name)\store"
        }
    }
}

# ── resolve HF token ──────────────────────────────────────────────────────────
$HfToken = ""
if ($HfTokenRaw) {
    $HfToken = $HfTokenRaw
} elseif ($HfEmail) {
    $profileTokenPath = "$env:APPDATA\mainframe\accounts\hf\$HfEmail\token"
    if (Test-Path $profileTokenPath) {
        $HfToken = (Get-Content $profileTokenPath -Raw).Trim()
    } else {
        $profileTokenPathTxt = "$env:APPDATA\mainframe\accounts\hf\$HfEmail\token.txt"
        if (Test-Path $profileTokenPathTxt) {
            $HfToken = (Get-Content $profileTokenPathTxt -Raw).Trim()
        }
    }
}

function Now { Get-Date -Format 'HH:mm:ss' }

$ProxyScript = Join-Path $RepoRoot "scripts\murmur-proxy.js"
$ProxyUrl = "http://localhost:$ProxyPort/webhook"

function Update-TunnelSecret($tunnelUrl) {
    $sendUrl = "$tunnelUrl/send"
    $body = @{ key = 'WACLI_SEND_WEBHOOK_URL'; value = $sendUrl } | ConvertTo-Json
    try {
        Invoke-RestMethod -Uri "https://huggingface.co/api/spaces/$MurmurSpace/secrets" -Method POST -Headers @{ Authorization = "Bearer $HfToken"; "Content-Type" = "application/json" } -Body $body -TimeoutSec 15 | Out-Null
        Write-Host "[$(Now)] HF secret updated: $sendUrl" -ForegroundColor Cyan
    } catch {
        Write-Host "[$(Now)] HF secret update failed: $($_.Exception.Message)" -ForegroundColor Red
    }
}

function Start-Localtunnel {
    $ltOut = "$env:TEMP\lt.out"
    $ltErr = "$env:TEMP\lt.err"
    $proc = Start-Process -FilePath "pwsh" -ArgumentList @("-NoProfile", "-Command", "npx lt --port $ProxyPort") -RedirectStandardOutput $ltOut -RedirectStandardError $ltErr -WindowStyle Hidden -PassThru
    Start-Sleep -Seconds 15
    $out = Get-Content $ltOut -ErrorAction SilentlyContinue
    $urlLine = $out | Select-String -Pattern "https://[a-z0-9-]+\.loca\.lt" | Select-Object -First 1
    if ($urlLine) {
        $url = $urlLine.Matches[0].Value
        $url | Set-Content "$env:TEMP\tunnel-url.txt" -Force
        Write-Host "[$(Now)] localtunnel: $url" -ForegroundColor Cyan
        return @{ Proc = $proc; Url = $url }
    }
    Write-Host "[$(Now)] localtunnel failed to get URL" -ForegroundColor Red
    return @{ Proc = $proc; Url = $null }
}

function Kill-ExistingProcesses {
    Get-Process -Name "wacli" -ErrorAction SilentlyContinue | ForEach-Object {
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
    }
    Get-Process -Name "node" -ErrorAction SilentlyContinue | ForEach-Object {
        try {
            $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)" -ErrorAction SilentlyContinue).CommandLine
            if ($cmd -like "*$ProxyScript*") {
                Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
            }
        } catch {}
    }
    Get-Process -Name "pwsh" -ErrorAction SilentlyContinue | ForEach-Object {
        try {
            $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)" -ErrorAction SilentlyContinue).CommandLine
            if ($cmd -like "*npx lt --port*") {
                Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
            }
        } catch {}
    }
    Start-Sleep -Seconds 2
}

while ($true) {
    Kill-ExistingProcesses

    if (Test-Path $ProxyScript) {
        Write-Host "[$(Now)] starting murmur proxy..." -ForegroundColor Cyan
        $proxyProc = Start-Process -FilePath "node" -ArgumentList $ProxyScript -WindowStyle Hidden -PassThru
        Start-Sleep -Seconds 3
    }

    Write-Host "[$(Now)] starting localtunnel..." -ForegroundColor Cyan
    $lt = Start-Localtunnel
    if ($lt.Url) {
        Update-TunnelSecret $lt.Url
        Start-Sleep -Seconds 30
    }

    Write-Host "[$(Now)] starting wacli sync..." -ForegroundColor Green
    try {
        $wacliArgs = @(
            "sync", "--follow",
            "--store", $StorePath,
            "--webhook", $ProxyUrl,
            "--webhook-secret", $WebhookSecret,
            "--webhook-allow-private"
        )
        $proc = Start-Process -FilePath $WacliBin -ArgumentList $wacliArgs -WindowStyle Hidden -PassThru
        $start = Get-Date
        
        while (-not $proc.HasExited) {
            $elapsed = [math]::Round(((Get-Date) - $start).TotalMinutes, 1)
            if ($elapsed % 5 -eq 0) {
                Write-Host "[$(Now)] wacli running for ${elapsed}m" -ForegroundColor DarkGray
            }
            Start-Sleep -Seconds 30
        }
        
        Write-Host "[$(Now)] wacli exited with code $($proc.ExitCode), restarting in 5s..." -ForegroundColor Yellow
    } catch {
        Write-Host "[$(Now)] failed to start wacli: $_" -ForegroundColor Red
    }
    
    if ($proxyProc -and -not $proxyProc.HasExited) {
        Stop-Process -Id $proxyProc.Id -Force -ErrorAction SilentlyContinue
    }
    if ($lt.Proc -and -not $lt.Proc.HasExited) {
        Stop-Process -Id $lt.Proc.Id -Force -ErrorAction SilentlyContinue
    }
    
    Start-Sleep -Seconds 5
}
