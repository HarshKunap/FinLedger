# Read-only capture for the FinLedger identity check. Sends ONE Export request to the TallyPrime on this PC
# (127.0.0.1 only) and saves the request and the reply byte-for-byte. Requests contain no Import, no VOUCHER element
# and no ACTION, so they cannot change Tally data. It never overwrites an earlier capture.
# Most people should double-click Run-FinLedger-Capture.cmd instead, which calls this for every step.
# Single use (PowerShell, in this folder):
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\capture.ps1 -Company "Exact Test Company Name" -Date 20260401 -Label before_edit
param(
  [ValidateSet('voucher', 'company', 'ledger', 'bill')][string]$Kind = 'voucher',
  [string]$Company,
  [ValidatePattern('^[0-9]{8}$')][string]$Date,
  [ValidatePattern('^[0-9]{8}$')][string]$ToDate,
  [Parameter(Mandatory=$true)][ValidatePattern('^[A-Za-z0-9_-]{1,60}$')][string]$Label,
  [ValidateRange(1, 65535)][int]$Port = 9000,
  [string]$OutDir = $PSScriptRoot
)
$ErrorActionPreference = 'Stop'

function Assert-Date([string]$name, [string]$value) {
  try { return [datetime]::ParseExact($value, 'yyyyMMdd', [System.Globalization.CultureInfo]::InvariantCulture) }
  catch { throw "refusing: -$name $value is not a real calendar date in YYYYMMDD form (1-Apr-2026 is 20260401)" }
}

if ($Kind -eq 'voucher') {
  if ([string]::IsNullOrEmpty($Company)) { throw 'refusing: -Company is required for a voucher capture' }
  if ([string]::IsNullOrEmpty($Date)) { throw 'refusing: -Date is required for a voucher capture' }
  if ([string]::IsNullOrEmpty($ToDate)) { $ToDate = $Date }
  $from = Assert-Date 'Date' $Date
  $to = Assert-Date 'ToDate' $ToDate
  if ($to -lt $from) { throw "refusing: -ToDate $ToDate is before -Date $Date" }
  $templateName = 'voucher_export_request.xml'
} elseif ($Kind -eq 'ledger') {
  if ([string]::IsNullOrEmpty($Company)) { throw 'refusing: -Company is required for a ledger capture' }
  $templateName = 'ledger_export_request.xml'
} elseif ($Kind -eq 'bill') {
  if ([string]::IsNullOrEmpty($Company)) { throw 'refusing: -Company is required for a bill capture' }
  $templateName = 'bill_export_request.xml'
} else {
  $templateName = 'company_list_request.xml'
}

if (-not (Test-Path -LiteralPath $OutDir -PathType Container)) { throw "refusing: output folder $OutDir does not exist" }
$req = Join-Path $OutDir "request_$Label.xml"
$resp = Join-Path $OutDir "response_$Label.xml"
if ((Test-Path -LiteralPath $req) -or (Test-Path -LiteralPath $resp)) {
  throw "refusing: request_$Label.xml or response_$Label.xml already exists; pick a new -Label so no capture is overwritten"
}

# The write-word guard reads the TEMPLATE (all of its text, before any name goes in) and then the tags of the filled
# request; the company name is escaped text, never a tag, so a name like 'Shree Imports' is data, not structure.
$pureExport = 'Import|ACTION\s*=|<VOUCHER\b|<TALLYMESSAGE\b'
$template = Get-Content -Raw -Encoding UTF8 -LiteralPath (Join-Path $PSScriptRoot $templateName)
if ($template -match $pureExport) { throw 'refusing: request template is not a pure export' }
if (([regex]::Matches($template, '<TALLYREQUEST>Export</TALLYREQUEST>')).Count -ne 1) {
  throw 'refusing: request template is not a single Export'
}
$fills = @{}
if ($Kind -eq 'voucher' -or $Kind -eq 'ledger' -or $Kind -eq 'bill') {
  $fills['__COMPANY__'] = [System.Security.SecurityElement]::Escape($Company)
}
if ($Kind -eq 'voucher') { $fills['__FROMDATE__'] = $Date; $fills['__TODATE__'] = $ToDate }
$unfilled = $template
foreach ($k in $fills.Keys) { $unfilled = $unfilled.Replace($k, '') }
if ($unfilled -match '__[A-Z]+__') { throw 'refusing: unfilled placeholder in request' }
# One pass over the template: a value is never searched again, so a name holding '__TODATE__' stays that name.
$body = [regex]::Replace($template, '__[A-Z]+__', { param($m) $fills[$m.Value] })
# Filling may change text only: the filled request must hold exactly the template's tags, in the same order.
$tagsOf = { param($x) @([regex]::Matches($x, '<[^>]*>') | ForEach-Object { $_.Value }) -join "`n" }
$tags = & $tagsOf $body
if ($tags -cne (& $tagsOf $template) -or $tags -match $pureExport -or ($body -replace '<[^>]*>', '') -match '[<>]') {
  throw 'refusing: filling the template changed the request structure'
}
if (([regex]::Matches($body, '<TALLYREQUEST>Export</TALLYREQUEST>')).Count -ne 1) {
  throw 'refusing: request is not a single Export'
}

[System.IO.File]::WriteAllText($req, $body, (New-Object System.Text.UTF8Encoding $false))
try {
  $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port" -Method Post -ContentType 'text/xml; charset=utf-8' `
    -InFile $req -OutFile $resp -PassThru -UseBasicParsing -TimeoutSec 60
} catch {
  Remove-Item -LiteralPath $req, $resp -Force -ErrorAction SilentlyContinue
  throw "Tally did not answer on 127.0.0.1:$Port, nothing was saved. Is TallyPrime open with the company loaded? $($_.Exception.Message)"
}

$size = (Get-Item -LiteralPath $resp).Length
$hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $resp).Hash
Write-Host "Saved request_$Label.xml and response_$Label.xml (raw bytes, unmodified)."
Write-Host "response_$Label.xml: $size bytes, SHA256 $hash"
if ($size -eq 0) { Write-Warning 'Tally sent an empty reply. Keep the file anyway.' }
[pscustomobject]@{
  Label = $Label; Kind = $Kind; Company = $Company; FromDate = $Date; ToDate = $ToDate
  Request = $req; Response = $resp; Bytes = $size; Sha256 = $hash
  HttpStatus = [int]$r.StatusCode; ContentType = [string]($r.Headers['Content-Type'] -join ', ')
}
