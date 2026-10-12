# FinLedger Tally identity capture: runs every read-only capture in one go and saves everything in one new folder.
# It only sends Export (read) requests to the TallyPrime on this PC (127.0.0.1). It never creates, alters, imports
# or deletes anything in Tally. The one Tally change (editing the voucher amount) is done by you, by hand, when asked.
# Easiest: double-click Run-FinLedger-Capture.cmd in this folder.
param(
  [string]$Company = 'FinLedger Test Company',
  [ValidatePattern('^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')]
  [string]$DocumentId = '9d4f3d5d-499e-41db-b510-e98348e5b503',
  [ValidatePattern('^[0-9]{8}$')][string]$Date = '20260401',
  [ValidatePattern('^[0-9]{8}$')][string]$OtherDate = '20260402',
  [ValidateRange(1, 65535)][int]$Port = 9000,
  [ValidateSet('ask', 'done', 'skip')][string]$EditStep = 'ask',
  [string]$OutRoot = (Join-Path $PSScriptRoot 'captures'),
  [switch]$NoExplorer
)
$ErrorActionPreference = 'Stop'
$capture = Join-Path $PSScriptRoot 'capture.ps1'
$marker = "finledger:document_id=$DocumentId"
$culture = [System.Globalization.CultureInfo]::InvariantCulture
$day = [datetime]::ParseExact($Date, 'yyyyMMdd', $culture)
$null = [datetime]::ParseExact($OtherDate, 'yyyyMMdd', $culture)
$fyStartYear = if ($day.Month -ge 4) { $day.Year } else { $day.Year - 1 }
$fyFrom = '{0}0401' -f $fyStartYear
$fyTo = '{0}0331' -f ($fyStartYear + 1)

function Say([string]$text, [string]$color = 'Gray') { Write-Host $text -ForegroundColor $color }

function Name-Key([string]$name) {
  # The Python resolver's comparison: Unicode compatibility form, case and spacing ignored.
  $words = $name.Normalize([System.Text.NormalizationForm]::FormKC).ToLowerInvariant() -split '\s+'
  return (@($words | Where-Object { $_ }) -join ' ')
}

function Read-Raw([string]$path) {
  # Decode a COPY of the reply for the quick look. The saved file is never changed.
  $bytes = [System.IO.File]::ReadAllBytes($path)
  $name = 'UTF-8 (no BOM)'; $enc = New-Object System.Text.UTF8Encoding $false; $skip = 0
  if ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFF -and $bytes[1] -eq 0xFE) { $name = 'UTF-16LE with BOM'; $enc = [System.Text.Encoding]::Unicode; $skip = 2 }
  elseif ($bytes.Length -ge 2 -and $bytes[0] -eq 0xFE -and $bytes[1] -eq 0xFF) { $name = 'UTF-16BE with BOM'; $enc = [System.Text.Encoding]::BigEndianUnicode; $skip = 2 }
  elseif ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) { $name = 'UTF-8 with BOM'; $skip = 3 }
  elseif ($bytes.Length -ge 2 -and $bytes[1] -eq 0) { $name = 'UTF-16LE (no BOM, guessed)'; $enc = [System.Text.Encoding]::Unicode }
  [pscustomobject]@{ Encoding = $name; Text = $enc.GetString($bytes, $skip, $bytes.Length - $skip) }
}

function Get-Values([string]$text, [string]$tag) {
  @([regex]::Matches($text, "<$tag(?:\s[^>]*)?>([^<]*)</$tag>") | ForEach-Object { $_.Groups[1].Value })
}

function Quick-Look($result) {
  # Informational only: counts and values as text. The raw files are the evidence, not this summary.
  $raw = Read-Raw $result.Response
  $t = $raw.Text
  $look = [ordered]@{
    label = $result.Label; window = "$($result.FromDate)-$($result.ToDate)"; bytes = $result.Bytes
    sha256 = $result.Sha256; http_status = $result.HttpStatus; content_type = $result.ContentType
    encoding = $raw.Encoding
    voucher_tags = ([regex]::Matches($t, '<VOUCHER\b')).Count
    object_tags = ([regex]::Matches($t, '<OBJECT\b')).Count
    company_tags = ([regex]::Matches($t, '<COMPANY\b')).Count
    lineerror = ([regex]::Matches($t, '<LINEERROR\b')).Count
    exact_marker_hits = ([regex]::Matches($t, [regex]::Escape($marker))).Count
    uuid_hits = ([regex]::Matches($t, [regex]::Escape($DocumentId), 'IgnoreCase')).Count
  }
  foreach ($tag in 'GUID', 'MASTERID', 'ALTERID', 'DATE', 'VOUCHERTYPENAME', 'VOUCHERNUMBER', 'PARTYLEDGERNAME', 'REFERENCE', 'AMOUNT', 'NAME') {
    $vals = Get-Values $t $tag
    if ($vals.Count -gt 0) { $look["values_$tag"] = ($vals | Select-Object -First 10) -join ' | ' }
  }
  [pscustomobject]$look
}

# ---------- 1. one new output folder ----------
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
if (-not (Test-Path -LiteralPath $OutRoot)) { New-Item -ItemType Directory -Path $OutRoot | Out-Null }
$runDir = Join-Path $OutRoot "run_$stamp"
$n = 1
while (Test-Path -LiteralPath $runDir) { $runDir = Join-Path $OutRoot "run_${stamp}_$n"; $n++ }
New-Item -ItemType Directory -Path $runDir | Out-Null
$summaryPath = Join-Path $runDir 'summary.txt'
$looks = New-Object System.Collections.ArrayList
$notes = New-Object System.Collections.ArrayList
$failed = $false

Say ''
Say '=== FinLedger Tally capture (read-only) ===' 'Cyan'
Say "Company   : $Company"
Say "Voucher   : $marker on $Date"
Say "Saving to : $runDir"
Say ''

function Write-Summary {
  $lines = New-Object System.Collections.ArrayList
  [void]$lines.Add('FinLedger Tally capture summary (quick look; the response_*.xml files are the evidence)')
  [void]$lines.Add("run_folder: $runDir")
  [void]$lines.Add("started: $stamp   powershell: $($PSVersionTable.PSVersion)   os: $([Environment]::OSVersion.VersionString)")
  [void]$lines.Add("company: $Company   document_id: $DocumentId   date: $Date   other_date: $OtherDate   fy_window: $fyFrom-$fyTo   port: $Port")
  foreach ($note in $notes) { [void]$lines.Add("note: $note") }
  foreach ($look in $looks) {
    [void]$lines.Add('')
    foreach ($p in $look.PSObject.Properties) { [void]$lines.Add(('{0,-24} {1}' -f $p.Name, $p.Value)) }
  }
  [System.IO.File]::WriteAllLines($summaryPath, [string[]]$lines, (New-Object System.Text.UTF8Encoding $false))
}

function Step([string]$label, [hashtable]$a) {
  Say "-> $label" 'Cyan'
  $r = & $capture -Label $label -Port $Port -OutDir $runDir @a
  $look = Quick-Look $r
  [void]$looks.Add($look)
  if ($look.lineerror -gt 0) { Say "   Tally answered with an error (LINEERROR). Saved anyway." 'Yellow' }
  Say ("   {0} bytes, {1}, voucher tags {2}, object tags {3}, marker found {4}x" -f $look.bytes, $look.encoding, $look.voucher_tags, $look.object_tags, $look.exact_marker_hits)
  $look
}

try {
  # ---------- 2. is Tally listening? ----------
  Say "-> Checking that TallyPrime answers on 127.0.0.1:$Port" 'Cyan'
  $client = New-Object System.Net.Sockets.TcpClient
  $open = $false
  try { $open = $client.ConnectAsync('127.0.0.1', $Port).Wait(3000) -and $client.Connected } catch { $open = $false } finally { $client.Close() }
  if (-not $open) {
    [void]$notes.Add("port $Port closed")
    throw "TallyPrime is not answering on 127.0.0.1:$Port. Open TallyPrime, load '$Company', check F1 > Settings > Connectivity (TallyPrime acts as Server/Both, port $Port), then run this again."
  }
  Say '   OK' 'Green'

  # ---------- 3. which companies are loaded? ----------
  $c = Step '01_companies' @{ Kind = 'company' }
  $escapedName = [System.Security.SecurityElement]::Escape($Company)
  $companyText = (Read-Raw (Join-Path $runDir 'response_01_companies.xml')).Text
  # Every pin below names '$Company'; refuse when two loaded companies differ only by case or spacing.
  $attrNames = [regex]::Matches($companyText, '<COMPANY\b[^>]*\bNAME="([^"]*)"') | ForEach-Object { $_.Groups[1].Value }
  $variants = New-Object 'System.Collections.Generic.HashSet[string]' ([System.StringComparer]::Ordinal)
  foreach ($v in @(Get-Values $companyText 'NAME') + @($attrNames)) {
    $v = [System.Net.WebUtility]::HtmlDecode($v)
    if ((Name-Key $v) -ceq (Name-Key $Company)) { [void]$variants.Add($v) }
  }
  if ($variants.Count -gt 1) {
    [void]$notes.Add('company name is ambiguous ignoring case or spacing')
    throw ("$($variants.Count) loaded companies are named '$Company' ignoring case or spacing; the company cannot " +
      'be determined. Load only one of them and run again.')
  }
  if ($companyText.Contains(">$escapedName<") -or $companyText.Contains("`"$escapedName`"")) {
    Say "   '$Company' is loaded." 'Green'
  } else {
    [void]$notes.Add('company name not seen in company list reply')
    Say "   Could not see '$Company' in Tally's company list. Continuing; the replies will show why." 'Yellow'
  }

  # ---------- 4. before the edit ----------
  $common = @{ Kind = 'voucher'; Company = $Company }
  $d1 = Step '02_day_before_edit' ($common + @{ Date = $Date; ToDate = $Date })
  $f1 = Step '03_fy_before_edit' ($common + @{ Date = $fyFrom; ToDate = $fyTo })
  $o1 = Step '04_other_day' ($common + @{ Date = $OtherDate; ToDate = $OtherDate })
  if ($d1.exact_marker_hits -ge 1 -or $f1.exact_marker_hits -ge 1) { Say "   Found the FinLedger marker in Tally's reply." 'Green' }
  else { Say '   The marker was NOT found in the replies. Keep going; the saved files will tell us why.' 'Yellow' }

  # ---------- 5. the one manual step ----------
  $action = $EditStep
  if ($action -eq 'ask') {
    Say ''
    Say '=== YOUR TURN: one small change in TallyPrime ===' 'Yellow'
    Say "1. In TallyPrime open the Purchase voucher with supplier invoice TEST-INV-001 (Gateway of Tally > Day Book, F2 to set the date)."
    Say '2. Change ONLY the amount (for example the purchase line 1,000 -> 2,000). Do not touch the narration, date, number or party.'
    Say '3. Save it (Ctrl+A). Then come back to this window.'
    Say ''
    $answer = Read-Host 'Type DONE and press Enter when saved (or SKIP to finish without the after-edit capture)'
    $action = if ($answer.Trim() -match '^(?i)done$') { 'done' } else { 'skip' }
    if ($action -eq 'done') {
      $amount = Read-Host 'Optional: type the new amount you saved (or just press Enter)'
      if ($amount) { [void]$notes.Add("new amount typed by user: $amount") }
    }
  }

  # ---------- 6. after the edit ----------
  if ($action -eq 'done') {
    [void]$notes.Add('user confirmed the amount edit')
    $d2 = Step '05_day_after_edit' ($common + @{ Date = $Date; ToDate = $Date })
    $f2 = Step '06_fy_after_edit' ($common + @{ Date = $fyFrom; ToDate = $fyTo })
    foreach ($k in 'values_GUID', 'values_MASTERID', 'values_ALTERID', 'values_AMOUNT') {
      $before = $d1.PSObject.Properties[$k]; $after = $d2.PSObject.Properties[$k]
      $b = if ($before) { $before.Value } else { '(none)' }
      $a = if ($after) { $after.Value } else { '(none)' }
      $same = if ($b -eq $a) { 'same' } else { 'CHANGED' }
      [void]$notes.Add("$k before=[$b] after=[$a] -> $same")
      Say ("   {0,-16} {1}" -f $k.Substring(7), $same)
    }
  } else {
    [void]$notes.Add('after-edit capture skipped')
  }
} catch {
  $failed = $true
  [void]$notes.Add("STOPPED: $($_.Exception.Message)")
  Say ''
  Say "STOPPED: $($_.Exception.Message)" 'Red'
} finally {
  Write-Summary
}

# ---------- 7. one zip to hand back ----------
$zip = Join-Path $OutRoot ("FinLedger_Tally_Capture_{0}.zip" -f (Split-Path $runDir -Leaf))
Compress-Archive -Path (Join-Path $runDir '*') -DestinationPath $zip
Say ''
if ($failed) { Say 'Finished with a problem (see STOPPED above). Everything captured so far is saved.' 'Yellow' }
else { Say 'All captures saved.' 'Green' }
Say "Folder : $runDir"
Say "Zip    : $zip"
Say 'Report back that the capture is done (or attach the zip).'
if (-not $NoExplorer -and $env:OS -eq 'Windows_NT') { Start-Process explorer.exe -ArgumentList "/select,`"$zip`"" }
if ($failed) { exit 1 }
