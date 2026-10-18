$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.IO.Compression
Add-Type -AssemblyName System.IO.Compression.FileSystem

function Get-VerifiedPackage($Url, $Path, $Expected) {
    if ((Test-Path -LiteralPath $Path) -and
        (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash -eq $Expected) { return }
    $partial = $Path + '.partial'
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $partial
    if ((Get-FileHash -LiteralPath $partial -Algorithm SHA256).Hash -ne $Expected) {
        throw "SHA256 mismatch: $partial"
    }
    Move-Item -LiteralPath $partial -Destination $Path -Force
}

function Expand-VerifiedPackage($ZipPath, $Destination, $Expected) {
    if ((Get-FileHash -LiteralPath $ZipPath -Algorithm SHA256).Hash -ne $Expected) {
        throw "SHA256 mismatch: $ZipPath"
    }
    $base = [IO.Path]::GetFullPath($Destination).TrimEnd('\') + '\'
    $zip = [IO.Compression.ZipFile]::OpenRead($ZipPath)
    $names = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    try {
        # Validate every entry before writing any file.
        foreach ($entry in $zip.Entries) {
            if ($entry.FullName -match '[:\\]' -or $entry.FullName.StartsWith('/') -or
                ($entry.FullName.Split('/') -contains '..') -or
                !$names.Add($entry.FullName)) { throw 'Unsafe or duplicate ZIP entry' }
            $target = [IO.Path]::GetFullPath([IO.Path]::Combine($base, $entry.FullName))
            if (!$target.StartsWith($base, [StringComparison]::OrdinalIgnoreCase)) {
                throw 'ZIP entry outside installation'
            }
        }
        foreach ($entry in $zip.Entries) {
            $target = [IO.Path]::GetFullPath([IO.Path]::Combine($base, $entry.FullName))
            if ($entry.FullName.EndsWith('/')) {
                [IO.Directory]::CreateDirectory($target) | Out-Null
            } else {
                [IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target)) | Out-Null
                [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $true)
            }
        }
    } finally { $zip.Dispose() }
}
