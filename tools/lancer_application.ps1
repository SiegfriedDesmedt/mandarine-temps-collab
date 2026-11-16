[CmdletBinding()]
param(
    [switch]$NoBrowser,
    [switch]$NoDialogs
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot

function Get-EnvSetting {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$DefaultValue
    )

    $environmentValue = [Environment]::GetEnvironmentVariable($Name)
    if (-not [string]::IsNullOrWhiteSpace($environmentValue)) {
        return $environmentValue.Trim()
    }

    foreach ($envFile in @(
        (Join-Path $repoRoot "reporting\.env"),
        (Join-Path $repoRoot ".env")
    )) {
        if (-not (Test-Path -LiteralPath $envFile)) {
            continue
        }
        $pattern = "^\s*" + [Regex]::Escape($Name) + "\s*=\s*(.*?)\s*$"
        foreach ($line in Get-Content -LiteralPath $envFile) {
            if ($line -match $pattern) {
                $value = $Matches[1].Trim().Trim('"').Trim("'")
                if (-not [string]::IsNullOrWhiteSpace($value)) {
                    return $value
                }
            }
        }
    }
    return $DefaultValue
}

function Show-LaunchError {
    param([string]$Message)

    if ($NoDialogs) {
        Write-Error $Message
        return
    }
    try {
        $shell = New-Object -ComObject WScript.Shell
        $null = $shell.Popup($Message, 0, "Mandarine Reporting - Erreur", 16)
    }
    catch {
        Write-Error $Message
    }
}

try {
    & (Join-Path $PSScriptRoot "run_local.ps1") start
    if ($LASTEXITCODE -ne 0) {
        throw "Le demarrage de Reporting a echoue."
    }

    if (-not $NoBrowser) {
        $port = Get-EnvSetting -Name "REPORTING_PORT" -DefaultValue "4174"
        Start-Process "http://127.0.0.1:$port/"
    }
}
catch {
    Show-LaunchError -Message $_.Exception.Message
    exit 1
}

exit 0
