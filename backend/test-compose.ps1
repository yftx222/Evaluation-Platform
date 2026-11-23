$ErrorActionPreference = 'Stop'
Push-Location (Join-Path $PSScriptRoot '..')
try {
    $pgPort = docker compose -f compose.yaml -f compose.test.yaml port postgres 5432
    if ($LASTEXITCODE -ne 0) {throw 'Start the Compose test services first'}
    $redisPort = docker compose -f compose.yaml -f compose.test.yaml port redis 6379
    if ($LASTEXITCODE -ne 0) {throw 'Redis port unavailable'}
    $password = $env:POSTGRES_PASSWORD
    if (!$password) {
        $line = Get-Content -LiteralPath .env | Where-Object { $_ -match '^POSTGRES_PASSWORD=' } | Select-Object -First 1
        $password = $line.Substring('POSTGRES_PASSWORD='.Length)
    }
    $prior = @{}
    foreach ($key in 'EVAL_DATABASE_URL','EVAL_REDIS_URL','EVAL_INTEGRATION') {
        $prior[$key] = [Environment]::GetEnvironmentVariable($key)
    }
    try {
        $env:EVAL_DATABASE_URL = 'postgresql+asyncpg://eval:' + $password + '@' + $pgPort + '/eval'
        $env:EVAL_REDIS_URL = 'redis://' + $redisPort
        $env:EVAL_INTEGRATION = '1'
        Set-Location backend
        & .venv/Scripts/python -m pytest -q
        if ($LASTEXITCODE -ne 0) {throw 'Integration suite failed'}
    } finally {
        foreach ($key in $prior.Keys) {[Environment]::SetEnvironmentVariable($key,$prior[$key])}
    }
} finally {Pop-Location}
