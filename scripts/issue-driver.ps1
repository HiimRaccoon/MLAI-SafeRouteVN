$ErrorActionPreference = 'Stop'
try {
    $root = Split-Path -Parent $PSScriptRoot
    $app = Join-Path $root 'SafeRouteVN_Portable_Win64_20261014'
    $matches = @(
        Get-ChildItem "$app\portable-state" -Filter installation.json -Recurse |
        Where-Object {
            $config = Get-Content $_.FullName -Raw | ConvertFrom-Json
            $config.snapshot_root -eq $app
        }
    )
    if ($matches.Count -ne 1) { throw 'Chay SafeRoute va mo scenario trong Admin truoc.' }
    $driver = Read-Host 'Driver ID, vi du TX01'
    if ([string]::IsNullOrWhiteSpace($driver)) { throw 'Driver ID khong duoc de trong.' }
    $config = Get-Content "$app\deployment\funnel.json" -Raw | ConvertFrom-Json
    & "$app\portable\python-backend\python.exe" -X utf8 -B `
        "$app\deployment\funnel_runtime.py" issue `
        --local-root $matches[0].DirectoryName --origin $config.public_origin --driver-id $driver
    if ($LASTEXITCODE -ne 0) { throw 'Tao tai khoan that bai.' }
    Write-Host 'Tai khoan duoc luu trong SafeRouteVN_Driver_Accounts, ngoai Git.'
} catch { Write-Error $_; exit 1 }
