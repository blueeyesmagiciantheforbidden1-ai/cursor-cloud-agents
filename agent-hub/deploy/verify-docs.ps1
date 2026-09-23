$ErrorActionPreference = 'Stop'
$Doc = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'README.md') -Raw
$Snippets = [regex]::Matches($Doc, '(?s)```powershell\r?\n(.*?)```')
$Source = ($Snippets | ForEach-Object { $_.Groups[1].Value }) -join [Environment]::NewLine
$ParseErrors = $null
$AstTokens = $null
[System.Management.Automation.Language.Parser]::ParseInput($Source, [ref]$AstTokens, [ref]$ParseErrors) | Out-Null
if ($ParseErrors.Count -gt 0) { $ParseErrors | Format-List; exit 1 }
Write-Output ('PowerShell blocks parse successfully: ' + $Snippets.Count)
