param(
    [string]$Source = "$env:USERPROFILE\Documents",
    [string]$Destination = "D:\Backup",
    [int]$KeepDays = 14
)

$stamp = Get-Date -Format "yyyy-MM-dd"
$target = Join-Path $Destination $stamp
New-Item -ItemType Directory -Path $target -Force | Out-Null

Get-ChildItem -Path $Source -Recurse -File |
    Where-Object { $_.Extension -notin ".tmp", ".lnk" } |
    ForEach-Object {
        $relative = $_.FullName.Substring($Source.Length)
        $dest = Join-Path $target $relative
        New-Item -ItemType Directory -Path (Split-Path $dest) -Force | Out-Null
        Copy-Item $_.FullName $dest
    }

# remove old backup folders
Get-ChildItem $Destination -Directory |
    Where-Object { $_.CreationTime -lt (Get-Date).AddDays(-$KeepDays) } |
    Remove-Item -Recurse -Force

Write-Host "Backup finished: $target"
