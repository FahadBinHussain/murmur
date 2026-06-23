#!/usr/bin/env pwsh
<#
.SYNOPSIS
    Run murmur-bridge with default config
.PARAMETER CookiesPath
    Path to cookies JSON (default: cookies_map.json in script dir)
.PARAMETER Platform
    Messenger platform (default: messenger)
#>
param(
    [string]$CookiesPath = "$PSScriptRoot\..\cookies_map.json",
    [string]$Platform = "messenger",
    [string]$LiteLLMBase = "https://alchoholpad-litellm.hf.space/v1"
)

$env:MURMUR_COOKIES = $CookiesPath
$env:MURMUR_PLATFORM = $Platform
$env:LITELLM_BASE = $LiteLLMBase
$env:NO_COLOR = "1"

$bridge = "$PSScriptRoot\..\bin\murmur-bridge.exe"
if (!(Test-Path $bridge)) {
    Write-Host "bridge not found, building..."
    & "$PSScriptRoot\build.sh"
}

& $bridge --cookies $CookiesPath --platform $Platform