[CmdletBinding(SupportsShouldProcess)]
param([int]$OlderThanDays = 30)

$downloads = Join-Path $env:USERPROFILE "Downloads"
$cutoff = (Get-Date).AddDays(-$OlderThanDays)
$old = Get-ChildItem $downloads -File | Where-Object { $_.LastWriteTime -lt $cutoff }
foreach ($file in $old) {
    if ($PSCmdlet.ShouldProcess($file.FullName, "Delete")) {
        Remove-Item $file.FullName
    }
}
Write-Output "$($old.Count) files older than $OlderThanDays days"
