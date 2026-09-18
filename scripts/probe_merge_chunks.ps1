$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
try {
    $probeRoot = [IO.Path]::GetFullPath($PSScriptRoot)
    if ((Get-Location).Path -ne $probeRoot) { throw 'Unexpected working directory' }
    $probeDir = Get-Item -LiteralPath $probeRoot -Force
    if ($probeDir.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked directory' }
    $manifestPath = Join-Path $probeRoot 'manifest.json'
    $manifestItem = Get-Item -LiteralPath $manifestPath -Force
    if ($manifestItem.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Linked manifest' }
    if ($manifestItem.Length -gt 4096) { throw 'Manifest too large' }
    $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
    if ($manifest.count -lt 1 -or $manifest.count -gt 5 -or
        $manifest.size -lt 1 -or $manifest.size -gt 20971520 -or
        $manifest.sha256 -notmatch '^[a-f0-9]{64}$') { throw 'Invalid manifest' }
    $expectedCount = [Math]::Ceiling($manifest.size / 4194304)
    if ($manifest.count -ne $expectedCount) { throw 'Invalid chunk count' }
    $outputPath = Join-Path $probeRoot 'merged.bin'
    # CreateNew refuses to overwrite any existing output, including a link.
    $output = [IO.File]::Open($outputPath, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
    try {
        for ($index = 0; $index -lt $manifest.count; $index++) {
            $chunkPath = Join-Path $probeRoot ('chunk-{0:D4}.bin' -f $index)
            $chunkItem = Get-Item -LiteralPath $chunkPath -Force
            if ($chunkItem.PSIsContainer -or ($chunkItem.Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw 'Invalid chunk' }
            $expectedSize = [Math]::Min(4194304, $manifest.size - $index * 4194304)
            if ($chunkItem.Length -ne $expectedSize) { throw 'Invalid chunk length' }
            $inputStream = [IO.File]::OpenRead($chunkPath)
            try { $inputStream.CopyTo($output) } finally { $inputStream.Dispose() }
        }
    } finally { $output.Dispose() }
    $hasher = [Security.Cryptography.SHA256]::Create()
    $hashStream = [IO.File]::OpenRead($outputPath)
    try {
        $hash = [BitConverter]::ToString($hasher.ComputeHash($hashStream)).Replace('-', '').ToLowerInvariant()
    } finally { $hashStream.Dispose(); $hasher.Dispose() }
    $size = (Get-Item -LiteralPath $outputPath).Length
    if ($size -ne $manifest.size -or $hash -ne $manifest.sha256) { throw 'Hash mismatch' }
    @{ verified = $true; size = $size; sha256 = $hash } | ConvertTo-Json -Compress
} catch {
    # Do not emit host paths, raw exceptions or file contents.
    [Console]::Error.WriteLine('Chunk merge verification failed')
    exit 1
}
