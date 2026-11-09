# Bootstrap shim: find a Python 3.11+; ALL real logic is in up.py.
$ErrorActionPreference = "Stop"
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue }
if (-not $py) {
  Write-Error "Python 3.11+ required — install from https://www.python.org/downloads/"
  exit 1
}
& $py.Source (Join-Path $PSScriptRoot 'up.py') @Args
