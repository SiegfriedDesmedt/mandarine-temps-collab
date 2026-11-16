param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("start", "refresh", "deploy")]
    [string]$Action
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$scriptPath = Join-Path $PSScriptRoot "local_environment.py"
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$codexPython = Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$python = $null
$candidates = @()

if ($env:REPORTING_PYTHON) {
    $candidates += $env:REPORTING_PYTHON
}
$candidates += $venvPython
$candidates += $codexPython
$localPythonRoot = Join-Path $env:LOCALAPPDATA "Programs\Python"
if (Test-Path -LiteralPath $localPythonRoot) {
    $candidates += Get-ChildItem -LiteralPath $localPythonRoot -Filter "python.exe" -Recurse -File -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty FullName
}
$pythonCommand = Get-Command python -ErrorAction SilentlyContinue
if ($pythonCommand) {
    $candidates += $pythonCommand.Source
}

foreach ($candidate in $candidates | Select-Object -Unique) {
    if (-not (Test-Path -LiteralPath $candidate)) {
        continue
    }
    & $candidate -c "import openpyxl" *> $null
    if ($LASTEXITCODE -eq 0) {
        $python = $candidate
        break
    }
}

if (-not $python) {
    $pyCommand = Get-Command py -ErrorAction SilentlyContinue
    if ($pyCommand) {
        & $pyCommand.Source -3 -c "import openpyxl" *> $null
        if ($LASTEXITCODE -eq 0) {
            & $pyCommand.Source -3 $scriptPath $Action
            exit $LASTEXITCODE
        }
    }
    throw "Python avec openpyxl est introuvable. Installez les dependances ou definissez REPORTING_PYTHON."
}

& $python $scriptPath $Action
exit $LASTEXITCODE
