param(
    [ValidateRange(1, 65535)][int]$ClientPort = 18080,
    [ValidateRange(1, 65535)][int]$GatewayPort = 18081,
    [ValidateRange(1, 65535)][int]$AdapterPort = 18082,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $taskRoot
$taskCli = Join-Path $taskRoot '.venv\Scripts\guomi-gateway.exe'
$taskUv = Join-Path $taskRoot '.tools\uv\uv.exe'
if (-not (Test-Path -LiteralPath $taskCli) -or -not (Test-Path -LiteralPath (Join-Path $taskRoot '.tools/gmssl/manifest.json'))) {
    Write-Host 'Preparing Python, dependencies and GmSSL. The first run needs internet, CMake and a C compiler.'
    & (Join-Path $PSScriptRoot 'bootstrap.ps1') -SkipChecks -PrepareNative
    if (-not (Test-Path -LiteralPath $taskCli)) { throw 'Environment preparation failed.' }
}
if (Test-Path -LiteralPath $taskUv) {
    $taskPreviousCache = $env:UV_CACHE_DIR
    try {
        $env:UV_CACHE_DIR = Join-Path $taskRoot '.tools\cache'
        & $taskUv sync --locked
        if ($LASTEXITCODE -ne 0) { throw 'Dependency sync failed.' }
    } finally {
        $env:UV_CACHE_DIR = $taskPreviousCache
    }
}
$taskArguments = @('webui-all', '--client-port', $ClientPort,
    '--gateway-port', $GatewayPort, '--adapter-port', $AdapterPort)
if ($NoBrowser) { $taskArguments += '--no-browser' }
& $taskCli @taskArguments
exit $LASTEXITCODE
