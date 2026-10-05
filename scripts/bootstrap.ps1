param([switch]$SkipChecks)

$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskUvVersion = (Get-Content -LiteralPath (Join-Path $taskRoot '.uv-version') -Raw).Trim()
$taskPythonVersion = (Get-Content -LiteralPath (Join-Path $taskRoot '.python-version') -Raw).Trim()
$taskTools = Join-Path $taskRoot '.tools'
$taskUv = Join-Path $taskTools 'uv/uv.exe'

if ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64') {
    throw 'This bootstrap supports Windows x86_64 only.'
}

New-Item -ItemType Directory -Path (Join-Path $taskTools 'uv') -Force | Out-Null
if (-not (Test-Path -LiteralPath $taskUv)) {
    $taskArchive = Join-Path $taskTools 'uv.zip'
    $taskUri = "https://github.com/astral-sh/uv/releases/download/$taskUvVersion/uv-x86_64-pc-windows-msvc.zip"
    Invoke-WebRequest -Uri $taskUri -OutFile $taskArchive
    $taskHash = (Get-FileHash -LiteralPath $taskArchive -Algorithm SHA256).Hash.ToLowerInvariant()
    $taskExpectedHash = '5049375aa2a5162f132b2c1cb992e25d42d47d934cab8c174dbe6f60973dcc12'
    if ($taskHash -ne $taskExpectedHash) { throw 'uv archive checksum mismatch.' }
    Expand-Archive -LiteralPath $taskArchive -DestinationPath (Join-Path $taskTools 'uv') -Force
}

$taskVersionOutput = & $taskUv --version
if ($LASTEXITCODE -ne 0 -or $taskVersionOutput -notmatch "^uv $([regex]::Escape($taskUvVersion))( |$)") {
    throw 'Local uv version does not match .uv-version; remove .tools/uv and rerun.'
}

$taskPreviousDirectory = Get-Location
$taskPreviousCache = $env:UV_CACHE_DIR
$taskPreviousPython = $env:UV_PYTHON_INSTALL_DIR
$taskPreviousPath = $env:PATH
$taskPreviousLibrary = $env:GMSSL_LIBRARY
$taskPreviousDigest = $env:GMSSL_SHA256
try {
    Set-Location -LiteralPath $taskRoot
    $env:UV_CACHE_DIR = Join-Path $taskTools 'cache'
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $taskTools 'python'
    & $taskUv python install --no-bin --no-registry $taskPythonVersion
    if ($LASTEXITCODE -ne 0) { throw 'Python installation failed.' }
    & $taskUv sync --locked --dev
    if ($LASTEXITCODE -ne 0) { throw 'Dependency sync failed.' }
    if (-not $SkipChecks) {
        $taskManifestPath = Join-Path $taskTools 'gmssl/manifest.json'
        if (-not (Test-Path -LiteralPath $taskManifestPath)) {
            & $taskUv run --locked python -m gateway.crypto.build_native
            if ($LASTEXITCODE -ne 0) { throw 'GmSSL build failed; install CMake and a C compiler or prepare the native manifest first.' }
        }
        $taskManifest = Get-Content -LiteralPath $taskManifestPath -Raw | ConvertFrom-Json
        $env:GMSSL_LIBRARY = $taskManifest.library
        $env:GMSSL_SHA256 = $taskManifest.sha256
        $env:PATH = (Split-Path -Parent $taskManifest.library) + ';' + $env:PATH
        & $taskUv run --locked ruff check src test scripts
        if ($LASTEXITCODE -ne 0) { throw 'Ruff check failed.' }
        & $taskUv run --locked ruff format --check src test scripts
        if ($LASTEXITCODE -ne 0) { throw 'Ruff format check failed.' }
        & $taskUv run --locked mypy src scripts
        if ($LASTEXITCODE -ne 0) { throw 'Type check failed.' }
        & $taskUv run --locked pytest test/ -m 'not e2e'
        if ($LASTEXITCODE -ne 0) { throw 'Tests failed.' }
    }
    Write-Output 'Environment ready. Use .tools/uv/uv.exe run --locked <command> in this checkout.'
} finally {
    $env:UV_CACHE_DIR = $taskPreviousCache
    $env:UV_PYTHON_INSTALL_DIR = $taskPreviousPython
    $env:PATH = $taskPreviousPath
    $env:GMSSL_LIBRARY = $taskPreviousLibrary
    $env:GMSSL_SHA256 = $taskPreviousDigest
    Set-Location -LiteralPath $taskPreviousDirectory
}
