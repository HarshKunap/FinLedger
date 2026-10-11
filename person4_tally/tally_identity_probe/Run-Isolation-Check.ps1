# FinLedger company-isolation check (read-only). Needs TWO OR MORE companies loaded in TallyPrime.
# It asks Tally which companies are loaded, then sends one voucher Export pinned (SVCURRENTCOMPANY) to each loaded
# company. For every reply it checks that each voucher GUID starts with the GUID of the company that request was
# pinned to. It does NOT pin a request to a company that is not loaded: on 2026-10-10 that request crashed TallyPrime
# 7.1 EDU (Software Exception c0000005, connection closed), so how Tally answers a missing company is unresolved.
# Only Export requests to 127.0.0.1 are sent; nothing in Tally is created,
# altered, imported or deleted, and there is no step for you to do in Tally.
# Easiest: double-click Run-Isolation-Check.cmd in this folder.
param(
  [string]$Company = 'FinLedger Test Company',
  [ValidatePattern('^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')]
  [string]$DocumentId = '9d4f3d5d-499e-41db-b510-e98348e5b503',
  [ValidatePattern('^[0-9]{8}$')][string]$FromDate = '20260401',
  [ValidatePattern('^[0-9]{8}$')][string]$ToDate = '20270331',
  [ValidateRange(1, 65535)][int]$Port = 9000,
  [ValidateRange(2, 10)][int]$MaxCompanies = 5,
  [string]$OutRoot = (Join-Path $PSScriptRoot 'captures'),
  [switch]$NoExplorer
)
$ErrorActionPreference = 'Stop'
$capture = Join-Path $PSScriptRoot 'capture.ps1'
$marker = "finledger:document_id=$DocumentId"
$guidPattern = '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'

function Say([string]$text, [string]$color = 'Gray') { Write-Host $text -ForegroundColor $color }

function Name-Key([string]$name) {
  # The Python resolver's comparison: Unicode compatibility form, case and spacing ignored.
  $words = $name.Normalize([System.Text.NormalizationForm]::FormKC).ToLowerInvariant() -split '\s+'
  return (@($words | Where-Object { $_ }) -join ' ')
}

function Load-Xml([string]$path) {
  # Parse a COPY for checking; DTDs and external entities are refused. The saved file is never changed.
  $settings = New-Object System.Xml.XmlReaderSettings
  $settings.DtdProcessing = [System.Xml.DtdProcessing]::Prohibit
  $settings.XmlResolver = $null
  $reader = [System.Xml.XmlReader]::Create($path, $settings)
  try { $doc = New-Object System.Xml.XmlDocument; $doc.Load($reader); return $doc } finally { $reader.Close() }
}

function Reply-State($doc) {
  $status = $doc.SelectSingleNode('/ENVELOPE/HEADER/STATUS')
  $errors = @($doc.SelectNodes('//LINEERROR') | ForEach-Object { $_.InnerText })
  if ($errors.Count -gt 0) { return "rejected: $($errors -join '; ')" }
  if ($null -eq $status -or $status.InnerText.Trim() -ne '1') { return "rejected: STATUS is '$(if ($status) { $status.InnerText } else { 'missing' })'" }
  return 'ok'
}

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
if (-not (Test-Path -LiteralPath $OutRoot)) { New-Item -ItemType Directory -Path $OutRoot | Out-Null }
$runDir = Join-Path $OutRoot "isolation_$stamp"
$n = 1
while (Test-Path -LiteralPath $runDir) { $runDir = Join-Path $OutRoot "isolation_${stamp}_$n"; $n++ }
New-Item -ItemType Directory -Path $runDir | Out-Null
$lines = New-Object System.Collections.ArrayList
$verdict = 'STOPPED'
$exitCode = 1

function Note([string]$text, [string]$color = 'Gray') { [void]$lines.Add($text); Say $text $color }

Say ''
Say '=== FinLedger company-isolation check (read-only) ===' 'Cyan'
Note "requested_company: $Company"
Note "marker: $marker"
Note "window_sent: $FromDate-$ToDate (Tally is known to ignore it; not used for any check)"
Note "folder: $runDir"

try {
  # ---------- 1. Tally listening? ----------
  $client = New-Object System.Net.Sockets.TcpClient
  $open = $false
  try { $open = $client.ConnectAsync('127.0.0.1', $Port).Wait(3000) -and $client.Connected } catch { $open = $false } finally { $client.Close() }
  if (-not $open) { throw "TallyPrime is not answering on 127.0.0.1:$Port. Open TallyPrime with two companies loaded and run this again." }

  # ---------- 2. which companies are loaded, and is the requested one exactly one of them? ----------
  $null = & $capture -Kind company -Label '01_companies' -Port $Port -OutDir $runDir
  $doc = Load-Xml (Join-Path $runDir 'response_01_companies.xml')
  $state = Reply-State $doc
  if ($state -ne 'ok') { throw "company list $state" }
  $companies = @()
  foreach ($node in $doc.SelectNodes('/ENVELOPE/BODY/DATA/COLLECTION/*')) {
    if ($node.get_LocalName() -ne 'COMPANY') { throw "company list holds a <$($node.get_LocalName())> row; expected only COMPANY" }
    $name = $node.SelectSingleNode('NAME'); $guid = $node.SelectSingleNode('GUID')
    if ($null -eq $name -or $null -eq $guid -or -not $name.InnerText) { throw 'a loaded company has no NAME or GUID in the company list' }
    $g = $guid.InnerText.Trim().ToLowerInvariant()
    if ($g -notmatch $guidPattern) { throw "company '$($name.InnerText)' has GUID '$g', not the 8-4-4-4-12 hex form seen before" }
    $companies += [pscustomobject]@{ Name = $name.InnerText; Guid = $g }
  }
  foreach ($c in $companies) { Note "loaded_company: '$($c.Name)' guid=$($c.Guid)" }
  if (@($companies | Select-Object -ExpandProperty Guid -Unique).Count -ne $companies.Count) { throw 'two loaded companies report the same GUID' }
  $similar = @($companies | Where-Object { (Name-Key $_.Name) -ceq (Name-Key $Company) })
  if ($similar.Count -gt 1) {
    throw ("$($similar.Count) loaded companies are named '$Company' ignoring case or spacing; the company cannot " +
      'be determined. Load only one of them and run again.')
  }
  $mine = @($companies | Where-Object { $_.Name -ceq $Company })
  if ($mine.Count -eq 0) { throw "'$Company' is not loaded (exact name, case-sensitive). Load it and run again." }
  if ($mine.Count -gt 1) { throw "$($mine.Count) loaded companies are named '$Company'; the company cannot be determined." }
  # every loaded company is pinned below, so no two of them may share a name ignoring case or spacing
  if (@($companies | ForEach-Object { Name-Key $_.Name } | Select-Object -Unique).Count -ne $companies.Count) {
    throw ('two loaded companies have the same name ignoring case or spacing; a pin by name could reach either. ' +
      'Load only one of them and run again.')
  }
  if ($companies.Count -lt 2) { throw "Only one company is loaded. TallyPrime must have a second existing company loaded at the same time as '$Company'; response_01_companies.xml shows what is loaded now." }
  if ($companies.Count -gt $MaxCompanies) { throw "$($companies.Count) companies are loaded; load at most $MaxCompanies for this check." }

  # ---------- 3. one pinned voucher export per loaded company (never a missing company: it crashed Tally) ----------
  $results = @()
  $i = 2
  $targets = @($companies)
  foreach ($t in $targets) {
    $label = '{0:d2}_vouchers_pinned_company{1}' -f $i, ($i - 1)
    $i++
    Say "-> $label  (SVCURRENTCOMPANY = '$($t.Name)')" 'Cyan'
    $r = [ordered]@{ label = $label; pinned_to = $t.Name; pinned_guid = $t.Guid; state = ''; vouchers = 0; own = 0; foreign = 0; malformed = 0; marker_hits = 0; foreign_guids = '' }
    try {
      $null = & $capture -Kind voucher -Company $t.Name -Date $FromDate -ToDate $ToDate -Label $label -Port $Port -OutDir $runDir
      try {
        $vdoc = Load-Xml (Join-Path $runDir "response_$label.xml")
        $r.state = Reply-State $vdoc
      } catch { $r.state = "unparseable: $($_.Exception.Message)" }
    } catch { $r.state = "capture failed: $($_.Exception.Message)" }
    if ($r.state -eq 'ok') {
      $foreign = @()
      foreach ($row in $vdoc.SelectNodes('/ENVELOPE/BODY/DATA/COLLECTION/*')) {
        if ($row.get_LocalName() -ne 'VOUCHER') { $r.malformed++; continue }
        $r.vouchers++
        $gnode = $row.SelectSingleNode('GUID')
        $vg = if ($gnode) { $gnode.InnerText.Trim().ToLowerInvariant() } else { '' }
        $owner = if ($vg -match '^([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})-[0-9a-f]{8}$') { $Matches[1] } else { $null }
        if ($null -eq $owner) { $r.malformed++ }
        elseif ($owner -eq $t.Guid) { $r.own++ }
        else { $r.foreign++; $foreign += $vg }
        $narr = $row.SelectSingleNode('NARRATION')
        if ($narr -and $narr.InnerText.Contains($marker)) { $r.marker_hits++ }
      }
      $r.foreign_guids = ($foreign | Select-Object -First 5) -join ' '
    }
    $line = "   state=$($r.state) vouchers=$($r.vouchers) own=$($r.own) foreign=$($r.foreign) malformed=$($r.malformed) marker_hits=$($r.marker_hits)"
    $color = if ($r.foreign -gt 0 -or $r.malformed -gt 0) { 'Red' } else { 'Gray' }
    Say $line $color
    [void]$lines.Add("$label pinned_to='$($t.Name)' $($line.Trim()) $(if ($r.foreign_guids) { "foreign_guids=$($r.foreign_guids)" })")
    $results += [pscustomobject]$r
  }

  # ---------- 4. verdict ----------
  $real = @($results)
  $ownTest = @($real | Where-Object { $_.pinned_to -ceq $Company })[0]
  $broken = @($real | Where-Object { $_.state -ne 'ok' -or $_.foreign -gt 0 -or $_.malformed -gt 0 })
  $othersWithVouchers = @($real | Where-Object { $_.pinned_to -cne $Company -and $_.own -gt 0 })
  if ($broken.Count -gt 0) {
    $verdict = 'ISOLATION_NOT_PROVEN'
    foreach ($b in $broken) { Note "$($b.label): state=$($b.state) foreign=$($b.foreign) malformed=$($b.malformed)" 'Red' }
    $exitCode = 2
  } elseif ($ownTest.marker_hits -ne 1) {
    $verdict = 'INCONCLUSIVE'
    Note "The marker was found $($ownTest.marker_hits) times in '$Company' (expected exactly 1)." 'Yellow'
    $exitCode = 3
  } elseif ($othersWithVouchers.Count -eq 0) {
    $verdict = 'PARTIAL_PASS'
    Note "Every pinned reply held only its own company's vouchers, but no other company had any voucher, so 'pinning keeps another company's vouchers out' is only tested in one direction." 'Yellow'
    $exitCode = 3
  } else {
    $verdict = 'PASS'
    Note 'Every pinned reply held only its own company''s vouchers, and the other company''s vouchers never appeared.' 'Green'
    $exitCode = 0
  }
} catch {
  Note "STOPPED: $($_.Exception.Message)" 'Red'
  $verdict = 'STOPPED'
  $exitCode = 1
} finally {
  [void]$lines.Insert(0, "verdict: $verdict")
  [void]$lines.Insert(1, 'This is a quick check; the request_*.xml / response_*.xml files are the evidence.')
  [System.IO.File]::WriteAllLines((Join-Path $runDir 'isolation_summary.txt'), [string[]]$lines, (New-Object System.Text.UTF8Encoding $false))
}

$zip = Join-Path $OutRoot ("FinLedger_Isolation_{0}.zip" -f (Split-Path $runDir -Leaf))
Compress-Archive -Path (Join-Path $runDir '*') -DestinationPath $zip
Say ''
Say "VERDICT: $verdict" $(if ($exitCode -eq 0) { 'Green' } elseif ($exitCode -eq 3) { 'Yellow' } else { 'Red' })
Say "Folder : $runDir"
Say "Zip    : $zip"
Say 'Report back that the isolation check is done.'
if (-not $NoExplorer -and $env:OS -eq 'Windows_NT') { Start-Process explorer.exe -ArgumentList "/select,`"$zip`"" }
exit $exitCode
