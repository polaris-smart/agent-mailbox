# Install agent-mailbox watch daemon as a logon scheduled task (Windows).
# All arguments are passed through to `agent-mailbox watch`, e.g.:
#   scripts\install-watch-windows.ps1 -WebhookUrl http://localhost:8644/webhooks/agent-mailbox -WebhookSecret SECRET -Notify HS
# A wrapper .cmd is generated so schtasks never has to quote the argument list.
param(
    [string]$WebhookUrl,
    [string]$WebhookSecret,
    [string[]]$Notify,
    [string]$TaskName = "AgentMailboxWatch"
)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
$PythonW = Join-Path $Repo ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $PythonW)) {
    throw "error: $PythonW not found - create the venv first (uv venv; uv pip install -e .)"
}

$MailRoot = Join-Path $HOME ".agent-mail"
New-Item -ItemType Directory -Force -Path $MailRoot | Out-Null

# build the watch argument list
$watchArgs = @("-m", "agent_mailbox.watch", "--root", $MailRoot, "--interval", "2.0")
if ($Notify) { $watchArgs += (@("--notify") + $Notify) }
if ($WebhookUrl) {
    $watchArgs += @("--webhook-url", $WebhookUrl)
    if ($WebhookSecret) { $watchArgs += @("--webhook-secret", $WebhookSecret) }
}

# wrapper cmd: one line, arguments baked in, no quoting hell for schtasks
$wrapper = Join-Path $MailRoot "watch.cmd"
$quoted = ($watchArgs | ForEach-Object { if ($_ -match '\s') { "`"$_`"" } else { $_ } }) -join " "
Set-Content -Path $wrapper -Value "@`"$PythonW`" $quoted" -Encoding ASCII

schtasks /Create /F /TN $TaskName /SC ONLOGON /TR "`"$wrapper`""
schtasks /Run /TN $TaskName
Write-Host "[ok] installed scheduled task '$TaskName' (runs at logon)"
Write-Host "     verify: schtasks /Query /TN $TaskName /V"
Write-Host "     log:    Get-Content $MailRoot\watch.log -Wait"
