param([string]$PackageDirectory)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$installLock = $null
try {
    . (Join-Path $PSScriptRoot 'install-lib.ps1')
    $root = Split-Path -Parent $PSScriptRoot
    $lock = Get-Content (Join-Path $root 'release-lock.json') -Raw | ConvertFrom-Json
    $app = Join-Path $root $lock.app
    $cache = Join-Path $root '.install-cache'
    New-Item -ItemType Directory -Force -Path $cache | Out-Null
    $installLock = [IO.File]::Open((Join-Path $cache 'install.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    if ($PackageDirectory) {
        $packages = (Resolve-Path -LiteralPath $PackageDirectory).Path
    } else {
        $packages = $cache
        Write-Host 'Downloading runtime...'
        Get-VerifiedPackage $lock.runtime_url "$packages\runtime.zip" $lock.runtime_sha256
        Write-Host 'Downloading data...'
        Get-VerifiedPackage $lock.data_url "$packages\data.zip" $lock.data_sha256
    }
    Write-Host 'Verifying and extracting packages...'
    # Check both archives before changing the installation.
    foreach ($name in @('runtime', 'data')) {
        if ((Get-FileHash "$packages\$name.zip" -Algorithm SHA256).Hash -ne $lock."${name}_sha256") {
            throw "SHA256 mismatch: $name.zip"
        }
    }
    Expand-VerifiedPackage "$packages\runtime.zip" $app $lock.runtime_sha256
    Expand-VerifiedPackage "$packages\data.zip" $app $lock.data_sha256
    & "$app\portable\python-backend\python.exe" -X utf8 -B "$PSScriptRoot\verify_install.py" $app
    if ($LASTEXITCODE -ne 0) { throw 'Installation verification failed' }
    Write-Host 'CAI DAT XONG. Chay CHAY_SAFEROUTE.bat.'
} catch {
    Write-Error $_
    exit 1
} finally {
    if ($installLock) { $installLock.Dispose() }
}
