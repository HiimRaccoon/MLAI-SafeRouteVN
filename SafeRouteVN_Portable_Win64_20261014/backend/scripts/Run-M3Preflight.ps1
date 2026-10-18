param(
    [Parameter(Mandatory = $true)]
    [string]$InstallationConfig,
    [Parameter(Mandatory = $true)]
    [string]$M1Python
)

$ErrorActionPreference = 'Stop'
$taskConfigPath = (Resolve-Path -LiteralPath $InstallationConfig).Path
$taskConfig = Get-Content -LiteralPath $taskConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($taskConfig.schema_version -ne 'saferoute-m3-server-installation/1') {
    throw 'Unsupported M3 installation configuration'
}
$taskProjectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$taskLocalRoot = Split-Path -Parent $taskConfigPath
$taskScript = Join-Path $PSScriptRoot 'preflight_m3.py'

# No installation, package updates, policy changes or source writes here.
& $taskConfig.runtime_python -B $taskScript --team-root $taskProjectRoot `
    --snapshot-root $taskConfig.snapshot_root --local-root $taskLocalRoot `
    --native --m1-python $M1Python
exit $LASTEXITCODE
