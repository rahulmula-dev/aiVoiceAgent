# run_phone.ps1
# This script loads credentials from .env and starts the Twilio Dev Phone

# 1. Load .env variables
$envPath = Join-Path $PSScriptRoot ".env"
if (Test-Path $envPath) {
    Get-Content $envPath | Where-Object { $_ -match "=" -and $_ -notmatch "^#" } | ForEach-Object {
        $name, $value = $_.Split("=", 2)
        [System.Environment]::SetEnvironmentVariable($name.Trim(), $value.Trim(), [System.EnvironmentVariableTarget]::Process)
    }
    Write-Host ">>> Credentials loaded from .env"
}
else {
    Write-Error "!!! .env file not found."
    exit 1
}

# 2. Check for SID and Token
if (-not $env:TWILIO_ACCOUNT_SID -or -not $env:TWILIO_AUTH_TOKEN) {
    Write-Error "!!! Missing TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN in .env"
    exit 1
}

# 3. Run Twilio Dev-Phone
Write-Host ">>> Starting Twilio Dev Phone (Headless Mode)..."
twilio dev-phone --headless
