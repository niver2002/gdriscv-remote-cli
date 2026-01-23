param(
  [Parameter(Mandatory = $true)]
  [string]$RemotePath,

  [string]$Text,
  [string]$LocalFile
)

$hasText = $PSBoundParameters.ContainsKey('Text')
$hasLocalFile = $PSBoundParameters.ContainsKey('LocalFile')

if ($hasText -and $hasLocalFile) {
  throw 'Pass either -Text or -LocalFile, not both.'
}
if (-not $hasText -and -not $hasLocalFile) {
  throw 'Pass -Text or -LocalFile.'
}

if ($hasLocalFile) {
  if (!(Test-Path $LocalFile)) { throw "File not found: $LocalFile" }
  $bytes = [System.IO.File]::ReadAllBytes((Resolve-Path $LocalFile))
} else {
  $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text)
}

$payload = @{
  path        = $RemotePath
  content_b64 = [Convert]::ToBase64String($bytes)
}
$json = $payload | ConvertTo-Json -Compress

& (Join-Path $PSScriptRoot 'call.ps1') -Method POST -Path '/write' -Body $json
