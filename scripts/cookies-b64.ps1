param(
    [string]$Path = "cookies.json",
    [switch]$Copy
)

$resolved = Resolve-Path -LiteralPath $Path -ErrorAction Stop
$bytes = [IO.File]::ReadAllBytes($resolved.ProviderPath)
$base64 = [Convert]::ToBase64String($bytes)

Write-Output $base64

if ($Copy) {
    Set-Clipboard -Value $base64
    Write-Output ""
    Write-Output "Copied to clipboard."
}
