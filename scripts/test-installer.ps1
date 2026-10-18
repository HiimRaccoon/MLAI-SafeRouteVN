$ErrorActionPreference = 'Stop'
$library = Join-Path $PSScriptRoot 'install-lib.ps1'
if (!(Test-Path $library)) { throw 'FAIL: installer package verification is not implemented' }
. $library
$scratch = Join-Path $env:TEMP ('saferoute-installer-test-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory $scratch | Out-Null
Add-Type -AssemblyName System.IO.Compression.FileSystem
function Make-Zip($Path, $Names) {
    $zip = [IO.Compression.ZipFile]::Open($Path, [IO.Compression.ZipArchiveMode]::Create)
    try {
        foreach ($name in $Names) {
            $entry = $zip.CreateEntry($name)
            $writer = [IO.StreamWriter]::new($entry.Open())
            try { $writer.Write('payload') } finally { $writer.Dispose() }
        }
    } finally { $zip.Dispose() }
}
function Must-Reject($Action, $Label) {
    $rejected = $false
    try { & $Action } catch { $rejected = $true }
    if (!$rejected) { throw "FAIL: $Label accepted" }
}
$good = Join-Path $scratch 'good.zip'
Make-Zip $good @('nested/file.txt')
$hash = (Get-FileHash $good -Algorithm SHA256).Hash
Expand-VerifiedPackage $good (Join-Path $scratch 'valid') $hash
if ((Get-Content (Join-Path $scratch 'valid/nested/file.txt') -Raw) -ne 'payload') {
    throw 'FAIL: valid archive content differs'
}
Must-Reject { Expand-VerifiedPackage $good (Join-Path $scratch 'bad-hash') ('0' * 64) } 'bad checksum'
if (Test-Path (Join-Path $scratch 'bad-hash')) { throw 'FAIL: checksum failure wrote destination' }
foreach ($entry in @('../escape.txt', '/absolute.txt', 'C:/absolute.txt', 'nested\escape.txt')) {
    $id = [guid]::NewGuid().ToString('N')
    $bad = Join-Path $scratch "$id.zip"
    Make-Zip $bad @('first.txt', $entry)
    $dest = Join-Path $scratch $id
    Must-Reject { Expand-VerifiedPackage $bad $dest (Get-FileHash $bad).Hash } 'unsafe archive'
    if (Test-Path $dest) { throw 'FAIL: unsafe archive partially extracted' }
}
$duplicate = Join-Path $scratch 'duplicate.zip'
Make-Zip $duplicate @('same.txt', 'SAME.txt')
Must-Reject { Expand-VerifiedPackage $duplicate (Join-Path $scratch 'duplicates') (Get-FileHash $duplicate).Hash } 'case-insensitive duplicate'
Write-Output 'INSTALLER_TESTS_PASS: valid ZIP, hash rejection, four traversal cases, duplicate rejection'
