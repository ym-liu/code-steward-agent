param([string]$Root, [string]$Csv, [long]$MinBytes = 1024)
Get-ChildItem -LiteralPath $Root -File |
    Where-Object { $_.Length -ge $MinBytes } |
    Select-Object Name, Length |
    Export-Csv -LiteralPath $Csv -NoTypeInformation
