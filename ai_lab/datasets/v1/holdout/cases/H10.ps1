param([string]$Path)
Import-Csv -LiteralPath $Path |
    Group-Object Status |
    Select-Object Name, Count |
    ConvertTo-Json
