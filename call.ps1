param(
  [ValidateSet('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS')]
  [string]$Method = 'GET',
  [string]$Path = '/',
  [string]$Body,
  [switch]$IncludeHeaders
)

$ErrorActionPreference = 'Stop'

$envFile = Join-Path $PSScriptRoot 'secrets.env'
if (!(Test-Path $envFile)) {
  throw "Missing secrets file: $envFile"
}

Get-Content $envFile | ForEach-Object {
  $line = $_.Trim()
  if ($line -eq '' -or $line.StartsWith('#')) { return }
  if ($line -notmatch '^([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { return }

  $name = $matches[1]
  $value = $matches[2].Trim()
  if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {
    $value = $value.Substring(1, $value.Length - 2)
  }

  Set-Item -Path "env:$name" -Value $value
}

if (-not $env:API_KEY) { throw 'API_KEY missing in secrets.env' }
if (-not $env:DEVICE_ID) { throw 'DEVICE_ID missing in secrets.env' }

$pathPart = $Path
if ([string]::IsNullOrWhiteSpace($pathPart)) { $pathPart = '/' }
if (-not $pathPart.StartsWith('/')) { $pathPart = '/' + $pathPart }

$url = "https://gdriscv.com/api/remote/$($env:DEVICE_ID)$pathPart"

$args = @('-sS', '-X', $Method, '-H', "X-API-KEY: $($env:API_KEY)", $url)
if ($IncludeHeaders) { $args = @('-sS', '-i') + $args }
if ($PSBoundParameters.ContainsKey('Body')) {
  $tmp = [System.IO.Path]::GetTempFileName()
  try {
    [System.IO.File]::WriteAllText($tmp, $Body, [System.Text.UTF8Encoding]::new($false))
    $args += @('-H', 'Content-Type: application/json', '--data-binary', "@$tmp")
    & curl.exe @args
  } finally {
    Remove-Item -Force $tmp -ErrorAction SilentlyContinue
  }
} else {
  & curl.exe @args
}
