$env:MESSENGER_COOKIES = "C:\tmp\mautrix-test\messenger-cookies.json"
$env:NO_COLOR = "1"

$psi = New-Object System.Diagnostics.ProcessStartInfo
$psi.FileName = "C:\tmp\mautrix-test\messenger-bridge.exe"
$psi.Arguments = "--cookies C:\tmp\mautrix-test\messenger-cookies.json"
$psi.RedirectStandardInput = $true
$psi.RedirectStandardOutput = $true
$psi.RedirectStandardError = $true
$psi.UseShellExecute = $false
$psi.CreateNoWindow = $true

$p = [System.Diagnostics.Process]::Start($psi)

$stdoutLines = [System.Collections.Concurrent.ConcurrentQueue[string]]::new()
$stderrLines = [System.Collections.Concurrent.ConcurrentQueue[string]]::new()

$stdoutJob = Start-ThreadJob -ScriptBlock {
    param($reader, $queue)
    while ($true) {
        $line = $reader.ReadLine()
        if ($null -eq $line) { break }
        $queue.Enqueue($line)
    }
} -ArgumentList $p.StandardOutput, $stdoutLines

$stderrJob = Start-ThreadJob -ScriptBlock {
    param($reader, $queue)
    while ($true) {
        $line = $reader.ReadLine()
        if ($null -eq $line) { break }
        $queue.Enqueue($line)
    }
} -ArgumentList $p.StandardError, $stderrLines

Start-Sleep -Seconds 30

# Test send_message to own thread
$p.StandardInput.WriteLine('{"type":"send_message","id":"send1","data":{"thread_id":100094912747838,"text":"test from go bridge"}}')
Start-Sleep -Seconds 10

$p.StandardInput.WriteLine('{"type":"stop"}')
Start-Sleep -Seconds 8
if (!$p.HasExited) { $p.Kill() }

$stdoutJob | Wait-Job -Timeout 5 | Out-Null
$stderrJob | Wait-Job -Timeout 5 | Out-Null

Write-Host "=== STDOUT ==="
$stdoutLines.ToArray()

Write-Host "=== STDERR (last 10) ==="
$stderrLines.ToArray() | Select-Object -Last 10
