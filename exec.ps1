param(
  [Parameter(Mandatory = $true)]
  [string]$Cmd,
  [string]$Cwd,
  [int]$TimeoutSec = 120
)

$cmdBytes = [System.Text.Encoding]::UTF8.GetBytes($Cmd)
$payload = @{
  cmd_b64     = [Convert]::ToBase64String($cmdBytes)
  timeout_sec = $TimeoutSec
}
if ($Cwd) { $payload.cwd = $Cwd }

$json = $payload | ConvertTo-Json -Compress

& (Join-Path $PSScriptRoot 'call.ps1') -Method POST -Path '/exec' -Body $json
