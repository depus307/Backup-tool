$ErrorActionPreference = 'Stop'
$requestFile = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__REQUEST_BASE64__'))
$request = Get-Content -LiteralPath $requestFile -Raw | ConvertFrom-Json
$root = (Resolve-Path -LiteralPath $request.root).Path
$stage = (Resolve-Path -LiteralPath $request.stage).Path
if ((Split-Path -Parent $stage) -ne $root -or (Split-Path -Leaf $stage) -notmatch '^\.backuptool-update-[a-z0-9_]+$') {
    throw 'Invalid update staging folder.'
}
$payload = Join-Path $stage 'payload'
$backup = Join-Path $stage 'previous'
$exe = Join-Path $root 'BackupTool.exe'
$resultFile = Join-Path $root '.backuptool-update-result.json'
$lock = $null
$installed = @()
$saved = @()
$result = @{ success = $false; version = $request.version; stage = (Split-Path -Leaf $stage); error = '' }

try {
    $parent = Get-Process -Id $request.pid -ErrorAction SilentlyContinue
    if ($parent -and -not $parent.WaitForExit(45000)) {
        throw 'BackupTool did not close in time.'
    }
    $lock = [IO.File]::Open((Join-Path $root '.backuptool-update.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    $sha256 = [Security.Cryptography.SHA256]::Create()
    $stream = [IO.File]::OpenRead($exe)
    try {
        $hash = [BitConverter]::ToString($sha256.ComputeHash($stream)).Replace('-', '')
    } finally {
        $stream.Dispose()
        $sha256.Dispose()
    }
    if ($hash -ne $request.original) {
        throw 'BackupTool changed while the update was downloading.'
    }
    [IO.Directory]::CreateDirectory($backup) | Out-Null
    foreach ($name in @('_internal', 'README.md', 'CHANGELOG.md', 'BackupTool.exe')) {
        $source = Join-Path $payload $name
        $target = Join-Path $root $name
        if (-not (Test-Path -LiteralPath $source)) { continue }
        if (Test-Path -LiteralPath $target) {
            Move-Item -LiteralPath $target -Destination $backup
            $saved += $name
        }
        Move-Item -LiteralPath $source -Destination $root
        $installed += $name
    }
    $result.success = $true
} catch {
    $result.error = $_.Exception.Message
    try {
        foreach ($name in $installed) {
            Move-Item -LiteralPath (Join-Path $root $name) -Destination $payload
        }
        foreach ($name in $saved) {
            Move-Item -LiteralPath (Join-Path $backup $name) -Destination $root
        }
    } catch {
        $result.error += ' Rollback: ' + $_.Exception.Message
    }
} finally {
    try {
        $json = $result | ConvertTo-Json -Compress
        [IO.File]::WriteAllText($resultFile, $json, [Text.Encoding]::UTF8)
    } finally {
        if ($lock) { $lock.Dispose() }
    }
}

$env:PYINSTALLER_RESET_ENVIRONMENT = '1'
Start-Process -FilePath $exe -WorkingDirectory $root
