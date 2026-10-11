# FinLedger open-bills capture (read-only, OPTIONAL). Captures what TallyPrime returns for the Bill collection Export
# that FinLedger's open-bills read sends, so that parser can be checked against real bytes. Nobody has to run this
# before other FinLedger work continues.
# Three Export requests to 127.0.0.1 only, nothing else:
#   01_companies        which companies are loaded (exact-name match to -Company, GUID read)
#   02_bills            Bill collection pinned (SVCURRENTCOMPANY) to that resolved company only
#   03_companies_after  company list again: same name, same GUID, or the run is marked STOPPED
# It never pins a company that is not loaded (that crashed TallyPrime 7.1 EDU on 2026-10-10). Nothing in Tally is
# created, altered, imported or deleted, and there is no step for you to do in Tally.
# The saved replies hold real bill references and amounts of that company: share them only with the FinLedger team.
# Easiest: double-click Run-Bills-Capture.cmd in this folder.
param(
  [string]$Company = 'FinLedger Test Company',
  [ValidateRange(1, 65535)][int]$Port = 9000,
  [string]$OutRoot = (Join-Path $PSScriptRoot 'captures'),
  [switch]$NoExplorer
)
$ErrorActionPreference = 'Stop'
$capture = Join-Path $PSScriptRoot 'capture.ps1'
$guidPattern = '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'

function Say([string]$text, [string]$color = 'Gray') { Write-Host $text -ForegroundColor $color }

function Name-Key([string]$name) {
  # The Python resolver's comparison: Unicode compatibility form, case and spacing ignored.
  $words = $name.Normalize([System.Text.NormalizationForm]::FormKC).ToLowerInvariant() -split '\s+'
  return (@($words | Where-Object { $_ }) -join ' ')
}

function Load-Xml([string]$path) {
  # Parse a COPY for checking; DTDs and external entities are refused. The saved file is never changed.
  # TallyPrime writes the reserved-name prefix as the character reference &#4; which XML 1.0 forbids, so the copy is
  # read with character checking off; the bytes on disk stay exactly as Tally sent them.
  $settings = New-Object System.Xml.XmlReaderSettings
  $settings.DtdProcessing = [System.Xml.DtdProcessing]::Prohibit
  $settings.XmlResolver = $null
  $settings.CheckCharacters = $false
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

function Read-Companies([string]$path) {
  $doc = Load-Xml $path
  $state = Reply-State $doc
  if ($state -ne 'ok') { throw "company list $state" }
  $list = @()
  foreach ($node in $doc.SelectNodes('/ENVELOPE/BODY/DATA/COLLECTION/*')) {
    if ($node.get_LocalName() -ne 'COMPANY') { throw "company list holds a <$($node.get_LocalName())> row; expected only COMPANY" }
    $name = $node.SelectSingleNode('NAME'); $guid = $node.SelectSingleNode('GUID')
    if ($null -eq $name -or $null -eq $guid -or -not $name.InnerText) { throw 'a loaded company has no NAME or GUID in the company list' }
    $g = $guid.InnerText.Trim().ToLowerInvariant()
    if ($g -notmatch $guidPattern) { throw "a loaded company has GUID '$g', not the 8-4-4-4-12 hex form seen before" }
    $list += [pscustomobject]@{ Name = $name.InnerText; Guid = $g }
  }
  if (@($list | Select-Object -ExpandProperty Guid -Unique).Count -ne $list.Count) { throw 'two loaded companies report the same GUID' }
  $similar = @($list | Where-Object { (Name-Key $_.Name) -ceq (Name-Key $Company) })
  if ($similar.Count -gt 1) {
    throw ("$($similar.Count) loaded companies are named '$Company' ignoring case or spacing; the company cannot " +
      'be determined. Load only one of them and run again.')
  }
  $mine = @($list | Where-Object { $_.Name -ceq $Company })
  if ($mine.Count -eq 0) { throw "'$Company' is not loaded (exact name, case-sensitive). Load it and run again." }
  if ($mine.Count -gt 1) { throw "$($mine.Count) loaded companies are named '$Company'; the company cannot be determined." }
  return [pscustomobject]@{ Company = $mine[0]; Count = $list.Count }
}

function File-Line([string]$label) {
  foreach ($kind in 'request', 'response') {
    $f = Join-Path $runDir "${kind}_$label.xml"
    if (Test-Path -LiteralPath $f) {
      $len = (Get-Item -LiteralPath $f).Length
      $sha = (Get-FileHash -Algorithm SHA256 -LiteralPath $f).Hash.ToLowerInvariant()
      Note "file: ${kind}_$label.xml bytes=$len sha256=$sha"
    }
  }
}

$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
if (-not (Test-Path -LiteralPath $OutRoot)) { New-Item -ItemType Directory -Path $OutRoot | Out-Null }
$runDir = Join-Path $OutRoot "bills_$stamp"
$n = 1
while (Test-Path -LiteralPath $runDir) { $runDir = Join-Path $OutRoot "bills_${stamp}_$n"; $n++ }
New-Item -ItemType Directory -Path $runDir | Out-Null
$lines = New-Object System.Collections.ArrayList
$verdict = 'STOPPED'
$exitCode = 1

function Note([string]$text, [string]$color = 'Gray') { [void]$lines.Add($text); Say $text $color }

Say ''
Say '=== FinLedger open-bills capture (read-only) ===' 'Cyan'
Note "requested_company: $Company"
Note "folder: $runDir"
Note 'contains_real_data: yes (bill references, party ledgers and amounts of this company)'

try {
  $client = New-Object System.Net.Sockets.TcpClient
  $open = $false
  try { $open = $client.ConnectAsync('127.0.0.1', $Port).Wait(3000) -and $client.Connected } catch { $open = $false } finally { $client.Close() }
  if (-not $open) { throw "TallyPrime is not answering on 127.0.0.1:$Port. Open TallyPrime with '$Company' loaded and run this again." }

  $null = & $capture -Kind company -Label '01_companies' -Port $Port -OutDir $runDir
  File-Line '01_companies'
  $before = Read-Companies (Join-Path $runDir 'response_01_companies.xml')
  Note "company_guid: $($before.Company.Guid)"
  Note "companies_loaded: $($before.Count)"

  Say "-> 02_bills (SVCURRENTCOMPANY = '$($before.Company.Name)')" 'Cyan'
  $null = & $capture -Kind bill -Company $before.Company.Name -Label '02_bills' -Port $Port -OutDir $runDir
  File-Line '02_bills'

  $null = & $capture -Kind company -Label '03_companies_after' -Port $Port -OutDir $runDir
  File-Line '03_companies_after'
  $after = Read-Companies (Join-Path $runDir 'response_03_companies_after.xml')
  if ($after.Company.Guid -ne $before.Company.Guid) { throw "'$Company' changed GUID during the capture ($($before.Company.Guid) -> $($after.Company.Guid))" }

  # ---------- a quick look at the bill reply (the files are the evidence) ----------
  $issues = @()
  $raw = [System.IO.File]::ReadAllBytes((Join-Path $runDir 'response_02_bills.xml'))
  $bom = if ($raw.Length -ge 2 -and $raw[0] -eq 0xFF -and $raw[1] -eq 0xFE) { 'utf-16le-bom' } elseif ($raw.Length -ge 3 -and $raw[0] -eq 0xEF -and $raw[1] -eq 0xBB -and $raw[2] -eq 0xBF) { 'utf-8-bom' } else { 'none' }
  Note "bill_reply_bom: $bom"
  try { $bdoc = Load-Xml (Join-Path $runDir 'response_02_bills.xml'); $state = Reply-State $bdoc } catch { $state = "unparseable: $($_.Exception.Message)"; $bdoc = $null }
  Note "bill_reply_state: $state"
  if ($state -ne 'ok') { $issues += "bill reply $state" }
  $rows = 0; $bills = 0; $noName = 0; $noParent = 0; $badDate = 0; $badAmount = 0; $positive = 0; $negative = 0; $zero = 0
  $tags = @{}; $parties = @{}; $fields = @{}
  if ($bdoc) {
    $collections = @($bdoc.SelectNodes('/ENVELOPE/BODY/DATA/COLLECTION'))
    Note "collections: $($collections.Count)"
    if ($collections.Count -ne 1) { $issues += "expected one BODY/DATA/COLLECTION, found $($collections.Count)" }
    foreach ($row in $bdoc.SelectNodes('/ENVELOPE/BODY/DATA/COLLECTION/*')) {
      $rows++
      $tag = $row.get_LocalName(); $tags[$tag] = 1 + [int]$tags[$tag]
      if ($tag -ne 'BILL') { continue }
      $bills++
      foreach ($child in $row.ChildNodes) { if ($child.NodeType -eq 'Element') { $fields[$child.get_LocalName()] = 1 + [int]$fields[$child.get_LocalName()] } }
      $name = $row.GetAttribute('NAME'); if (-not $name) { $c = $row.SelectSingleNode('NAME'); if ($c) { $name = $c.InnerText } }
      if (-not $name) { $noName++ }
      $p = $row.SelectSingleNode('PARENT')
      if ($null -eq $p -or -not $p.InnerText) { $noParent++ } else { $parties[$p.InnerText] = 1 }
      $d = $row.SelectSingleNode('BILLDATE')
      if ($d -and $d.InnerText.Trim() -and $d.InnerText.Trim() -notmatch '^[0-9]{8}$') { $badDate++ }
      foreach ($f in 'OPENINGBALANCE', 'CLOSINGBALANCE') {
        $v = $row.SelectSingleNode($f)
        $t = if ($v) { $v.InnerText.Trim() } else { '' }
        if ($t -notmatch '^-?[0-9]{1,12}(\.[0-9]{1,2})?$') { $badAmount++ }
        elseif ($f -eq 'CLOSINGBALANCE') { $x = [decimal]$t; if ($x -gt 0) { $positive++ } elseif ($x -lt 0) { $negative++ } else { $zero++ } }
      }
    }
  }
  Note ("row_tags: {0}" -f (($tags.GetEnumerator() | Sort-Object Name | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join ' '))
  Note ("bill_fields: {0}" -f (($fields.GetEnumerator() | Sort-Object Name | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join ' '))
  Note "bill_rows: $bills distinct_parties: $($parties.Count)"
  Note "missing_name: $noName missing_parent: $noParent bad_date: $badDate bad_amount: $badAmount"
  Note "closing_positive: $positive closing_negative: $negative closing_zero: $zero"
  if ($rows -ne $bills) { $issues += 'reply holds rows other than BILL' }
  if ($bills -eq 0) { $issues += 'no BILL rows (fine if the company has no bill-wise entries, but nothing to compare)' }
  if ($noName -gt 0 -or $noParent -gt 0 -or $badDate -gt 0 -or $badAmount -gt 0) { $issues += 'field problems (see counts)' }
  if ($issues.Count -eq 0) {
    $verdict = 'CAPTURED_CLEAN'
    Note 'The bill reply matches the shape FinLedger expects (still to be confirmed by the Python parser).' 'Green'
    $exitCode = 0
  } else {
    $verdict = 'CAPTURED_WITH_DIFFERENCES'
    foreach ($i in $issues) { Note "difference: $i" 'Yellow' }
    Note 'Captured. The differences are exactly what FinLedger needs to see; nothing is wrong with your Tally.' 'Yellow'
    $exitCode = 3
  }
} catch {
  Note "STOPPED: $($_.Exception.Message)" 'Red'
  $verdict = 'STOPPED'
  $exitCode = 1
} finally {
  [void]$lines.Insert(0, "verdict: $verdict")
  [void]$lines.Insert(1, 'This is a quick check; the request_*.xml / response_*.xml files are the evidence.')
  [System.IO.File]::WriteAllLines((Join-Path $runDir 'bills_summary.txt'), [string[]]$lines, (New-Object System.Text.UTF8Encoding $false))
}

$zip = Join-Path $OutRoot ("FinLedger_Bills_{0}.zip" -f (Split-Path $runDir -Leaf))
Compress-Archive -Path (Join-Path $runDir '*') -DestinationPath $zip
Say ''
Say "VERDICT: $verdict" $(if ($exitCode -eq 0) { 'Green' } elseif ($exitCode -eq 3) { 'Yellow' } else { 'Red' })
Say "Folder : $runDir"
Say "Zip    : $zip"
Say 'Report back that the bills capture is done.'
if (-not $NoExplorer -and $env:OS -eq 'Windows_NT') { Start-Process explorer.exe -ArgumentList "/select,`"$zip`"" }
exit $exitCode
