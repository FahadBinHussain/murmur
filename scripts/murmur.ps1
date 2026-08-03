# murmur.ps1 - single script: wacli sync + reverse polling for AI replies
# no tunnel needed. local machine polls HF for outgoing replies.
# RUN IN A VISIBLE WINDOW to see real-time logs.
#
# usage: pwsh scripts/murmur.ps1
# config: create .env in repo root from .env.example
#
# expects to be run from the murmur repo root (or scripts/ subdir).

param(
    [string]$StorePath = "",
    [int]$ProxyPort = 0,
    [int]$PollSeconds = 0
)

# ── load .env from repo root ────────────────────────────────────────────────
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Definition
$RepoRoot = if ($ScriptDir -match '\\scripts$') { Split-Path -Parent $ScriptDir } else { $ScriptDir }
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

# ── resolve config with param overrides ─────────────────────────────────────
$HfEmail = if ($env:HF_EMAIL) { $env:HF_EMAIL } else { "" }
$HfTokenRaw = if ($env:HF_TOKEN) { $env:HF_TOKEN.Trim() } else { "" }
$MurmurBase = if ($env:MURMUR_HF_SPACE_URL) { $env:MURMUR_HF_SPACE_URL.Trim().TrimEnd('/') } else { "https://fahadbinhussain-murmur.hf.space" }
$ResolvedProxyPort = if ($ProxyPort -gt 0) { $ProxyPort } elseif ($env:PROXY_PORT) { [int]$env:PROXY_PORT } else { 7870 }
$ResolvedPollSeconds = if ($PollSeconds -gt 0) { $PollSeconds } elseif ($env:POLL_SECONDS) { [int]$env:POLL_SECONDS } else { 5 }
$WebhookSecret = if ($env:WEBHOOK_SECRET) { $env:WEBHOOK_SECRET } else { "murmur-wa-2026" }
$WacliBin = if ($env:WACLI_BIN) { $env:WACLI_BIN } else { "C:\Users\Admin\go\bin\wacli.exe" }

# ── resolve HF token: raw token takes priority, else load from mainframe profile ──
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

# ── auto-detect phone number from whatsapp accounts ─────────────────────────
if (-not $StorePath) {
    if ($env:WACLI_STORE_PATH) {
        $StorePath = $env:WACLI_STORE_PATH
    } else {
        $waAccounts = Get-ChildItem "$env:APPDATA\mainframe\accounts\whatsapp" -Directory -ErrorAction SilentlyContinue | Sort-Object Name | Select-Object -First 1
        if ($waAccounts) {
            $StorePath = "$env:APPDATA\mainframe\accounts\whatsapp\$($waAccounts.Name)\store"
        }
    }
}

$WindowTitle = "murmur"
$Host.UI.RawUI.WindowTitle = $WindowTitle

# ── kill any previous murmur windows ─────────────────────────────────────────
Get-Process -Name "pwsh" -ErrorAction SilentlyContinue | ForEach-Object {
    if ($_.Id -ne $PID -and $_.MainWindowTitle -eq $WindowTitle) {
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Killed old murmur PID $($_.Id)" -ForegroundColor Yellow
    }
}
Start-Sleep -Seconds 2

# ── singleton guard ───────────────────────────────────────────────────────────
$SingletonFile = "$env:TEMP\murmur.lock"
if (Test-Path $SingletonFile) {
    $existingPid = Get-Content $SingletonFile -ErrorAction SilentlyContinue
    if ($existingPid) {
        $running = Get-Process -Id $existingPid -ErrorAction SilentlyContinue
        if ($running) {
            Write-Host "[$(Get-Date -Format 'HH:mm:ss')] Another murmur is already running (PID $existingPid). Exiting." -ForegroundColor Yellow
            Start-Sleep -Seconds 3
            exit 1
        }
    }
}
$PID | Set-Content $SingletonFile -Force

$SentMsgIdsPath = "$env:TEMP\murmur-sent-msg-ids.json"
$global:SentMsgIds = @{}
if (Test-Path $SentMsgIdsPath) {
    $data = Get-Content $SentMsgIdsPath -ErrorAction SilentlyContinue | ConvertFrom-Json -ErrorAction SilentlyContinue
    if ($data) { $data | Get-Member -MemberType NoteProperty | ForEach-Object { $global:SentMsgIds[$_.Name] = $true } }
}

# HF reverse-poll flap tracking. When HF proxy returns 5xx intermittently
# (happens on free-tier Spaces), we must NOT force-restart wacli — wacli
# is fine, only HF is hiccuping. The 60s force-restart stays in effect for
# real wacli desyncs, but is suppressed while HF is actively flapping.
$HfConsecutiveErrors = 0
$HfFlapThreshold = 2        # at or above this many consec errors, treat HF as flapping
$HfFlapPauseUntil = [DateTime]::MinValue   # if > now, skip force-restart entirely

function Now { Get-Date -Format 'HH:mm:ss' }

$ProxyScript = if ($env:MURMUR_PROXY_SCRIPT) { $env:MURMUR_PROXY_SCRIPT } else { Join-Path $RepoRoot "scripts\murmur-proxy.js" }
if (-not (Test-Path $ProxyScript)) {
    # fallback: check old automata path if murmur-proxy.js hasn't been moved yet
    $ProxyScript = Join-Path $PSScriptRoot "..\..\automata\whatsapp.com\murmur-proxy.js"
}
$MurmurWebhook = "$MurmurBase/wacli/webhook"
$ProxyUrl = "http://localhost:$ResolvedProxyPort/webhook"
$PollUrl = "$MurmurBase/api/outbox"
$AckUrl = "$MurmurBase/api/outbox/ack"

# ── cookie-refresh-on-failure config ────────────────────────────────────────
# Passive monitoring: query the daily-bnp outbox for FB send failures matching
# "send returned empty message ID". When failures cross a threshold inside a
# recent window, run murmur-cookie-refresher.mjs and reset the failed rows back
# to pending so murmur's BNP worker retries them automatically.
$BnpDatabaseUrl         = if ($env:BNP_DATABASE_URL) { $env:BNP_DATABASE_URL.Trim() } else { "" }
$CookieRefreshIntervalS = if ($env:BNP_COOKIE_REFRESH_INTERVAL_SECONDS) { [int]$env:BNP_COOKIE_REFRESH_INTERVAL_SECONDS } else { 600 }
$CookieRefreshWindowM   = if ($env:BNP_COOKIE_REFRESH_FAILURE_WINDOW_MINUTES) { [int]$env:BNP_COOKIE_REFRESH_FAILURE_WINDOW_MINUTES } else { 30 }
$CookieRefreshThreshold = if ($env:BNP_COOKIE_REFRESH_FAILURE_THRESHOLD) { [int]$env:BNP_COOKIE_REFRESH_FAILURE_THRESHOLD } else { 2 }
$PsqlBin                = if ($env:BNP_PSQL_BIN) { $env:BNP_PSQL_BIN } else { "C:\Users\Admin\scoop\apps\postgresql\current\bin\psql.exe" }
$CookieRefresherScript  = Join-Path $RepoRoot "scripts\murmur-cookie-refresher.mjs"
$CookieEngine           = "bnp-outbox"  # surfaced in logs
$LastCookieCheck        = [DateTime]::MinValue

# ── Neon usage warning config ───────────────────────────────────────────────
$NeonUsageScript        = if ($env:NEON_USAGE_TABLE_SCRIPT) { $env:NEON_USAGE_TABLE_SCRIPT } else { "C:\Users\Admin\Downloads\mainframe\neon-hours-table.ps1" }
$NeonCheckIntervalS     = if ($env:NEON_USAGE_CHECK_INTERVAL_SECONDS) { [int]$env:NEON_USAGE_CHECK_INTERVAL_SECONDS } else { 3600 }
$NeonWarningHours       = if ($env:NEON_USAGE_WARNING_HOURS) { [double]$env:NEON_USAGE_WARNING_HOURS } else { 90 }
$NeonWarningThreadId    = if ($env:NEON_USAGE_WARNING_THREAD_ID) { $env:NEON_USAGE_WARNING_THREAD_ID.Trim() } else { "2637078310061988" }
$NeonWarningStatePath   = if ($env:NEON_USAGE_WARNING_STATE_PATH) { $env:NEON_USAGE_WARNING_STATE_PATH } else { "$env:APPDATA\mainframe\state\murmur-neon-usage-warnings.json" }
$LastNeonUsageCheck     = [DateTime]::MinValue

# ── helpers ──────────────────────────────────────────────────────────────────

$LogFile = "$env:TEMP\murmur.log"
function Log($msg, $color = "White") {
    $line = "[$(Now)] $msg"
    Write-Host $line -ForegroundColor $color
    try { [System.IO.File]::AppendAllText($LogFile, "$line`n") } catch { }
}

function Kill-AllChildren {
    Get-Process -Name "node" -ErrorAction SilentlyContinue | ForEach-Object {
        try {
            $cmd = (Get-CimInstance Win32_Process -Filter "ProcessId=$($_.Id)" -ErrorAction SilentlyContinue).CommandLine
            if ($cmd -like "*$ProxyScript*") {
                Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
                Log "killed proxy node PID $($_.Id)" DarkGray
            }
        } catch {}
    }
    Get-Process -Name "wacli" -ErrorAction SilentlyContinue | ForEach-Object {
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        Log "killed wacli PID $($_.Id)" DarkGray
    }
    Start-Sleep -Seconds 2
    Remove-Item "$StorePath\LOCK" -Force -ErrorAction SilentlyContinue
    Remove-Item "$StorePath\.send.sock" -Force -ErrorAction SilentlyContinue
}

function Start-Proxy {
    if (-not (Test-Path $ProxyScript)) {
        Log "PROXY SCRIPT NOT FOUND at $ProxyScript" Red
        return $null
    }
    Log "=== STARTING PROXY on :$ResolvedProxyPort ===" Cyan
    $proc = Start-Process -FilePath "node" -ArgumentList $ProxyScript -WindowStyle Hidden -PassThru
    Start-Sleep -Seconds 3
    $port = Get-NetTCPConnection -LocalPort $ResolvedProxyPort -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($port) {
        Log "PROXY LISTENING on PID $($port.OwningProcess)" Green
    } else {
        Log "PROXY FAILED to bind to port $ResolvedProxyPort" Red
    }
    return $proc
}

function Start-Wacli {
    Log "=== STARTING WACLI SYNC ===" Green
    $args = @(
        "sync", "--follow",
        "--store", $StorePath,
        "--webhook", $ProxyUrl,
        "--webhook-secret", $WebhookSecret,
        "--webhook-allow-private"
    )
    $proc = Start-Process -FilePath $WacliBin -ArgumentList $args -WindowStyle Hidden -PassThru
    Start-Sleep -Seconds 5
    $running = Get-Process -Id $proc.Id -ErrorAction SilentlyContinue
    if ($running) {
        Log "WACLI RUNNING PID $($proc.Id)" Green
    } else {
        Log "WACLI FAILED TO START" Red
    }
    return $proc
}

function Send-ViaWacli($jid, $text) {
    Log "SENDING reply to $jid" Cyan
    $cleanText = $text -replace "`r`n", " " -replace "`n", " " -replace "`r", " "
    $preview = if ($cleanText.Length -gt 60) { $cleanText.Substring(0, 60) + "..." } else { $cleanText }
    Log "  text: $preview" DarkGray
    $tmpOut = "$env:TEMP\wacli-send-$([int](Get-Random -Maximum 9999999)).log"
    $tmpErr = "$env:TEMP\wacli-err-$([int](Get-Random -Maximum 9999999)).log"
    
    # Use call operator instead of Start-Process to properly handle spaces in --message
    $stdout = & $WacliBin send text --store $StorePath --to $jid --message $cleanText 2>"$tmpErr"
    if ($LASTEXITCODE -eq 0) {
        Log "  SENT OK: $stdout" Green
        return $true
    } else {
        $stderr = Get-Content $tmpErr -ErrorAction SilentlyContinue
        Log "  SEND FAILED (exit=$LASTEXITCODE): $stderr" Red
        return $false
    }
}

function Compute-HMAC($payload) {
    $hmac = New-Object System.Security.Cryptography.HMACSHA256
    $hmac.Key = [System.Text.Encoding]::UTF8.GetBytes($WebhookSecret)
    $hash = $hmac.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($payload))
    return "sha256=" + [BitConverter]::ToString($hash).Replace("-", "").ToLower()
}

function Forward-ToMurmur($payload) {
    $sig = Compute-HMAC $payload
    try {
        $resp = Invoke-RestMethod -Uri $MurmurWebhook -Method POST -Headers @{
            "Content-Type" = "application/json"
            "Authorization" = "Bearer $HfToken"
            "X-Wacli-Signature" = $sig
        } -Body $payload -TimeoutSec 30
        Log "  FORWARDED OK to murmur" Green
        return $true
    } catch {
        Log "  FORWARD FAILED: $($_.Exception.Message)" Red
        return $false
    }
}

function Poll-DB-AndForward {
    $query = "SELECT msg_id, ts, chat_jid, sender_jid, sender_name, text FROM messages WHERE text LIKE '/ai%' AND from_me = 0 AND ts > (SELECT COALESCE(MAX(last_poll_ts), 0) FROM pipeline_state) ORDER BY ts ASC;"
    $result = & sqlite3 "$StorePath\wacli.db" ".mode json" $query 2>$null
    
    if (-not $result) { return }
    
    $jsonText = ($result -join "`n").Trim()
    $rows = @()
    try {
        $parsed = $jsonText | ConvertFrom-Json -ErrorAction Stop
        if ($parsed -is [array]) { $rows = $parsed } elseif ($parsed -is [System.Management.Automation.PSCustomObject]) { $rows = @($parsed) }
    } catch { return }
    if (-not $rows) { return }
    
    Log "DB POLL: found $($rows.Count) new /ai message(s)" Cyan
    
    $maxTs = 0
    foreach ($row in $rows) {
        $msgId = $row.msg_id
        if (-not $msgId) { continue }
        
        $text = $row.text
        if (-not $text.Trim().StartsWith('/ai')) { continue }
        
        $chatJid = $row.chat_jid
        $senderJid = $row.sender_jid
        $senderName = $row.sender_name
        $ts = $row.ts
        $timeStr = [DateTimeOffset]::FromUnixTimeSeconds([long]$ts).ToLocalTime().ToString('HH:mm:ss')
        
        Log "  INCOMING MESSAGE:" Yellow
        Log "    time:    $timeStr" White
        Log "    chat:    $chatJid" White
        Log "    sender:  $senderJid ($senderName)" White
        Log "    text:    $text" White
        Log "    msg_id:  $msgId" White
        
        $chatUser = if ($chatJid -match '^(.*?)@') { $matches[1] } else { $chatJid }
        $chatServer = if ($chatJid -match '@(.*)$') { $matches[1] } else { 's.whatsapp.net' }
        
        $payload = @{
            Chat = @{ user = $chatUser; server = $chatServer }
            ID = $msgId
            SenderJID = $senderJid
            Timestamp = [DateTimeOffset]::FromUnixTimeSeconds([long]$ts).ToString("o")
            FromMe = $false
            Text = $text
            PushName = $senderName
        } | ConvertTo-Json -Depth 3 -Compress
        
        Log "  FORWARDING to murmur HF..." DarkGray
        Forward-ToMurmur $payload
        
        if ($row.ts -gt $maxTs) { $maxTs = $row.ts }
    }
    
    if ($maxTs -gt 0) {
        & sqlite3 "$StorePath\wacli.db" "CREATE TABLE IF NOT EXISTS pipeline_state (id INTEGER PRIMARY KEY, last_poll_ts INTEGER); INSERT OR REPLACE INTO pipeline_state (id, last_poll_ts) VALUES (1, $maxTs);" 2>$null
        Log "  updated last_poll_ts to $maxTs" DarkGray
    }
}

function Poll-HF-ForReplies {
    if (-not $HfToken) { 
        Log "HF token missing, skipping reverse poll" Yellow
        return 
    }

    $resp = $null
    $attempt = 0
    $maxAttempts = 3
    $delaySec = 1

    while ($attempt -lt $maxAttempts) {
        $attempt++
        try {
            $resp = Invoke-RestMethod -Uri $PollUrl -Method GET -Headers @{ Authorization = "Bearer $HfToken" } -TimeoutSec 15
            break
        } catch {
            $statusCode = $_.Exception.Response.StatusCode.Value__
            $msg = $_.Exception.Message
            if ($attempt -lt $maxAttempts) {
                Log "REVERSE POLL attempt $attempt/$maxAttempts failed ($statusCode): $msg. Retrying in ${delaySec}s..." Yellow
                Start-Sleep -Seconds $delaySec
                $delaySec = $delaySec * 2
            } else {
                Log "REVERSE POLL ERROR: $msg" Yellow
                $HfConsecutiveErrors++
                if ($HfConsecutiveErrors -ge $HfFlapThreshold) {
                    $HfFlapPauseUntil = (Get-Date).AddSeconds(180)
                    Log "HF flap detected ($HfConsecutiveErrors consec errors). Suppressing wacli force-restart until $($HfFlapPauseUntil.ToString('HH:mm:ss'))." Yellow
                }
                return
            }
        }
    }

    if ($HfConsecutiveErrors -gt 0) {
        Log "HF recovered after $HfConsecutiveErrors consec error(s)." Green
        $HfConsecutiveErrors = 0
        $HfFlapPauseUntil = [DateTime]::MinValue
    }

    if (-not $resp) { return }

    $msgCount = if ($resp.messages) { $resp.messages.Count } else { 0 }
    if ($msgCount -gt 0) {
        Log "REVERSE POLL: found $msgCount reply(s) from HF" Cyan
        $acked = @()
        foreach ($msg in $resp.messages) {
            $jid = $msg.jid
            $text = $msg.text
            $msgId = $msg.id
            Log "  REPLY: id=$msgId jid=$jid text_len=$($text.Length) text='$(if($text.Length -gt 80){$text.Substring(0,80)+'...'}else{$text})'" Yellow
            if ($global:SentMsgIds[$msgId]) {
                Log "    ALREADY SENT - skipping" DarkGray
                $acked += $msgId
                continue
            }
            if ($jid -and $text) {
                $sent = Send-ViaWacli $jid $text
                if ($sent -and $msgId) { 
                    $acked += $msgId
                    $global:SentMsgIds[$msgId] = $true
                    $global:SentMsgIds | ConvertTo-Json | Set-Content $SentMsgIdsPath -Force
                }
            } else {
                Log "    SKIPPED - missing jid or text" Yellow
            }
        }
        if ($acked.Count -gt 0) {
            $ackBody = @{ ids = $acked } | ConvertTo-Json
            Invoke-RestMethod -Uri $AckUrl -Method POST -Headers @{ Authorization = "Bearer $HfToken"; "Content-Type" = "application/json" } -Body $ackBody -TimeoutSec 10 | Out-Null
            Log "  ACKED $($acked.Count) message(s)" Green
        }
    } else {
        Log "reverse poll: empty" DarkGray
    }
}

function HealthCheck {
    $issues = @()
    $port = Get-NetTCPConnection -LocalPort $ResolvedProxyPort -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $port) { $issues += "proxy not on $ResolvedProxyPort" }
    $w = Get-Process -Name "wacli" -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $w) { $issues += "wacli not running" }
    return $issues
}

function Get-BnpSendFailureCount {
    # Returns count of outbox rows that failed with the FB-empty-message-id
    # signature within the recent window. 0 = healthy, >0 = cookie send path
    # is degraded. Returns -1 on connection/config error so callers can choose
    # to skip the reset instead of acting blindly.
    if (-not $BnpDatabaseUrl) { return -1 }
    if (-not (Test-Path $PsqlBin)) { Log "psql not found: $PsqlBin" Yellow; return -1 }

    $query = @"
SELECT count(*) FROM `"BnpMessengerNotification`"
WHERE status = 'failed'
  AND `"lastError"` = 'send returned empty message ID'
  AND `"updatedAt"` >= NOW() - (INTERVAL '$CookieRefreshWindowM minutes');
"@
    # parse the neon connection string into psql flags so we don't leak the
    # password in process args visible in task manager / logs.
    $m = [regex]::Match($BnpDatabaseUrl, '^postgresql://([^:]+):([^@]+)@([^:]+?)(?::(\d+))?/(\w+)')
    if (-not $m.Success) { Log "BNP_DATABASE_URL not parseable" Yellow; return -1 }
    $pgUser = $m.Groups[1].Value
    $pgPass = $m.Groups[2].Value
    $pgHost = $m.Groups[3].Value
    $pgPort = if ($m.Groups[4].Value) { $m.Groups[4].Value } else { '5432' }
    $pgDb   = $m.Groups[5].Value

    $env:PGPASSWORD = $pgPass
    try {
        $out = & $PsqlBin -h $pgHost -p $pgPort -U $pgUser -d $pgDb -t -A -c $query --no-password 2>&1
        if ($LASTEXITCODE -ne 0) { Log "psql count failed: $out" Yellow; return -1 }
        $n = 0
        $raw = ($out -join '').Trim()
        if ([int]::TryParse($raw, [ref]$n)) { return $n }
        Log "psql count unparseable: $out" Yellow
        return -1
    } finally {
        Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
    }
}

function Reset-FailedBnpOutbox {
    if (-not $BnpDatabaseUrl) { return }
    if (-not (Test-Path $PsqlBin)) { return }

    $sql = @"
UPDATE `"BnpMessengerNotification`"
SET status = 'pending',
    `"lockedAt"` = NULL,
    `"lastError"` = NULL,
    attempts = 0,
    `"updatedAt"` = NOW()
WHERE status = 'failed'
  AND `"lastError"` = 'send returned empty message ID'
  AND `"updatedAt"` >= NOW() - (INTERVAL '$CookieRefreshWindowM minutes')
  AND phase IN ('detected', 'published');
"@
    $m = [regex]::Match($BnpDatabaseUrl, '^postgresql://([^:]+):([^@]+)@([^:]+?)(?::(\d+))?/(\w+)')
    if (-not $m.Success) { return }
    $pgPort = if ($m.Groups[4].Value) { $m.Groups[4].Value } else { '5432' }
    $env:PGPASSWORD = $m.Groups[2].Value
    try {
        $out = & $PsqlBin -h $m.Groups[3].Value -p $pgPort -U $m.Groups[1].Value -d $m.Groups[5].Value -c $sql --no-password 2>&1
        if ($LASTEXITCODE -eq 0) {
            Log "reset failed outbox rows: $((($out -join "`n") -split "`n" | Select-String 'UPDATE (\d+)').Matches.Groups[1].Value)" Green
        } else {
            Log "reset failed outbox error: $out" Yellow
        }
    } finally {
        Remove-Item Env:\PGPASSWORD -ErrorAction SilentlyContinue
    }
}

function Invoke-CookieRefresh {
    Log "=== COOKIE REFRESH TRIGGERED ($CookieEngine) ===" Cyan
    if (-not (Test-Path $CookieRefresherScript)) {
        Log "cookie refresher missing: $CookieRefresherScript" Red
        return
    }
    $proc = Start-Process -FilePath "node" -ArgumentList $CookieRefresherScript -PassThru -NoNewWindow
    $proc.WaitForExit()
    if ($proc.ExitCode -eq 0) {
        Log "cookie refresher ok (exit 0)" Green
    } else {
        Log "cookie refresher failed (exit $($proc.ExitCode))" Yellow
        return
    }
    # give murmur ~15s to settle the async MQTT reconnect after ReloadCookies
    Start-Sleep -Seconds 15
    Reset-FailedBnpOutbox
    Log "=== COOKIE REFRESH CYCLE DONE ===" Cyan
}

function Check-CookieHealth {
    Log "--- COOKIE HEALTH CHECK ---" Cyan
    # Cheap gate: if murmur's HTTP API is down entirely there's no point
    # refreshing FB cookies (the murmur isn't up to receive them).
    try {
        $null = Invoke-RestMethod -Uri "$MurmurBase/api/health" -Headers @{ Authorization = "Bearer $HfToken" } -TimeoutSec 10
        Log "  murmur /api/health: ok" Green
    } catch {
        Log "  murmur /api/health: UNREACHABLE ($($_.Exception.Message))" Red
        Log "  cookie-check skipped — murmur down" Yellow
        return
    }

    $failed = Get-BnpSendFailureCount
    if ($failed -lt 0) {
        Log "  BNP outbox query: error (skipping refresh)" Yellow
        return
    }
    if ($failed -ge $CookieRefreshThreshold) {
        Log "  BNP outbox: $failed failed sends in last ${CookieRefreshWindowM}m (threshold $CookieRefreshThreshold)" Red
        Log "  >>> TRIGGERING COOKIE REFRESH <<<" Yellow
        Invoke-CookieRefresh
    } else {
        Log "  BNP outbox: $failed recent failures (healthy)" Green
        Log "--- COOKIE HEALTH: OK ---" DarkCyan
    }
}

function Send-NeonUsageWarning($project) {
    if (-not $HfToken) {
        Log "  Neon warning not sent: HF token missing" Yellow
        return $false
    }

    $used = [double]$project.CU_Hours_Used
    $left = [double]$project.CU_Hours_Left
    # Normalize Quota_Reset to a stable yyyy-MM-dd UTC date so the dedup key
    # and human message stay consistent across API format / locale shifts and
    # don't change when a value like "2026-09-01T00:00:00Z" is rendered locally.
    $resetDate = 'unknown'
    if ($project.Quota_Reset -and $project.Quota_Reset -ne '-') {
        try {
            $dt = [DateTimeOffset]::Parse($project.Quota_Reset, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeUniversal -bor [Globalization.DateTimeStyles]::RoundtripKind)
            $resetDate = $dt.UtcDateTime.ToString('yyyy-MM-dd')
        } catch { $resetDate = [string]$project.Quota_Reset }
    }
    $body = @{
        source = 'neon-usage'
        threadId = $NeonWarningThreadId
        title = 'Neon usage warning'
        message = "$($project.Project) ($($project.Account)) has used $used of 100 CU-hours. $left CU-hours remain. Quota reset: $resetDate UTC."
        dedupeKey = "neon-$NeonWarningHours`:$($project.ProjectId):$resetDate"
    } | ConvertTo-Json -Compress

    try {
        $null = Invoke-RestMethod -Uri "$MurmurBase/api/automation/notifications" -Method POST -Headers @{
            Authorization = "Bearer $HfToken"
            'X-HF-Authorization' = "Bearer $HfToken"
            'Content-Type' = 'application/json'
        } -Body $body -TimeoutSec 30
        return $true
    } catch {
        Log "  Neon warning send failed: $($_.Exception.Message)" Yellow
        return $false
    }
}

function Check-NeonUsage {
    Log "--- NEON USAGE CHECK (warning at ${NeonWarningHours} CU-h) ---" Cyan
    if (-not (Test-Path $NeonUsageScript)) {
        Log "  Neon usage script missing: $NeonUsageScript" Yellow
        return
    }

    try {
        $json = & $NeonUsageScript -Json 2>&1
        if ($LASTEXITCODE -ne 0) { throw "usage script exited $LASTEXITCODE`: $($json -join ' ')" }
        $projects = @($json | ConvertFrom-Json -ErrorAction Stop)
    } catch {
        Log "  Neon usage query failed: $($_.Exception.Message)" Yellow
        return
    }

    $state = @{}
    if (Test-Path $NeonWarningStatePath) {
        try {
            $saved = Get-Content $NeonWarningStatePath -Raw | ConvertFrom-Json -ErrorAction Stop
            $saved.PSObject.Properties | ForEach-Object { $state[$_.Name] = [string]$_.Value }
        } catch {
            Log "  Neon warning state unreadable; rebuilding it" Yellow
        }
    }

    $overThreshold = @($projects | Where-Object {
        $_.ProjectId -and $_.ProjectId -ne '-' -and
        $_.Status -notmatch 'ERR' -and
        [double]$_.CU_Hours_Used -ge $NeonWarningHours
    })
    foreach ($project in $overThreshold) {
        $period = 'unknown'
        if ($project.Quota_Reset -and $project.Quota_Reset -ne '-') {
            try {
                $dt = [DateTimeOffset]::Parse($project.Quota_Reset, [Globalization.CultureInfo]::InvariantCulture, [Globalization.DateTimeStyles]::AssumeUniversal -bor [Globalization.DateTimeStyles]::RoundtripKind)
                $period = $dt.UtcDateTime.ToString('yyyy-MM-dd')
            } catch { $period = [string]$project.Quota_Reset }
        }
        if ($state[[string]$project.ProjectId] -eq $period) {
            Log "  already warned: $($project.Project) ($($project.CU_Hours_Used) CU-h)" DarkGray
            continue
        }

        Log "  threshold reached: $($project.Project) ($($project.CU_Hours_Used) CU-h used)" Yellow
        if (Send-NeonUsageWarning $project) {
            $state[[string]$project.ProjectId] = $period
            $stateDir = Split-Path -Parent $NeonWarningStatePath
            if (-not (Test-Path $stateDir)) { New-Item -ItemType Directory -Path $stateDir -Force | Out-Null }
            $state | ConvertTo-Json | Set-Content $NeonWarningStatePath -Force
            Log "  Neon usage warning sent" Green
        }
    }

    if ($overThreshold.Count -eq 0) {
        $highest = $projects | Where-Object { $_.ProjectId -and $_.ProjectId -ne '-' } | Sort-Object CU_Hours_Used -Descending | Select-Object -First 1
        if ($highest) {
            Log "  no project at threshold; highest is $($highest.Project) at $($highest.CU_Hours_Used) CU-h" Green
        } else {
            Log "  no Neon projects returned" Yellow
        }
    }
}

# ── main loop ────────────────────────────────────────────────────────────────

while ($true) {
    Log "========================================" Cyan
    Log "=== MURMUR STARTING ===" Cyan
    Log "========================================" Cyan
    Log "Store:  $StorePath" White
    Log "Proxy:  :$ResolvedProxyPort" White
    Log "HF:     $MurmurBase" White
    Log "Poll:   every ${ResolvedPollSeconds}s" White
    Log "========================================" Cyan
    
    Kill-AllChildren
    
    Remove-Item "$env:TEMP\murmur-proxy-processed.json" -Force -ErrorAction SilentlyContinue
    Remove-Item "$env:TEMP\murmur-poller-state.json" -Force -ErrorAction SilentlyContinue
    
    & sqlite3 "$StorePath\wacli.db" "CREATE TABLE IF NOT EXISTS pipeline_state (id INTEGER PRIMARY KEY, last_poll_ts INTEGER); INSERT OR IGNORE INTO pipeline_state (id, last_poll_ts) VALUES (1, 0);" 2>$null
    
    $proxyProc = Start-Proxy
    $wacliProc = Start-Wacli
    $start = Get-Date
    $pollCounter = 0
    $maxRunSeconds = 60
    
    while ($true) {
        Start-Sleep -Seconds $ResolvedPollSeconds
        $elapsedSec = [math]::Round(((Get-Date) - $start).TotalSeconds, 0)
        $elapsedMin = [math]::Round(((Get-Date) - $start).TotalMinutes, 1)
        $pollCounter++

        Poll-HF-ForReplies

        # passive cookie-health check on a coarse cadence (default 10 min)
        $now = Get-Date
        if (([int64]($now - $LastCookieCheck).TotalSeconds -ge $CookieRefreshIntervalS)) {
            $LastCookieCheck = $now
            Check-CookieHealth
        }

        if (([int64]($now - $LastNeonUsageCheck).TotalSeconds -ge $NeonCheckIntervalS)) {
            $LastNeonUsageCheck = $now
            Check-NeonUsage
        }

        if ($elapsedMin % 1 -lt 0.1) {
            $issues = HealthCheck
            if ($issues.Count -eq 0) {
                Log "HEALTH CHECK: healthy (${elapsedMin}m)" DarkGray
            } else {
                Log "HEALTH CHECK: issues: $($issues -join '; ')" Yellow
            }
        }

        if ($wacliProc.HasExited) {
            Log "WACLI EXITED (code $($wacliProc.ExitCode)), restarting..." Yellow
            break
        }

        if ($elapsedSec -ge $maxRunSeconds) {
            # Skip the force-restart while HF is flapping — wacli is fine,
            # only HF is hiccuping. Reschedule Force-restart to fire 60s after
            # HF flap pause ends, so we still re-sync wacli once HF recovers.
            if ((Get-Date) -lt $HfFlapPauseUntil) {
                Log "FORCE RESTART skipped (HF flap until $($HfFlapPauseUntil.ToString('HH:mm:ss'))). wacli stays up." DarkGray
            } else {
                Log "WACLI FORCE RESTART after ${maxRunSeconds}s" Yellow
                break
            }
        }

        if ($proxyProc -and $proxyProc.HasExited) {
            Log "PROXY DIED, restarting..." Yellow
            break
        }
    }
    
    Log "murmur restart in 5s..." Yellow
    Start-Sleep -Seconds 5
}
