param(
    [Parameter(Mandatory = $true)][ValidateSet("staging", "production")][string]$Environment,
    [string]$Project = ".\src\InventoryApi\InventoryApi.csproj"
)

$ErrorActionPreference = "Stop"
$targets = @{
    staging    = "\\web-staging01\sites\inventory-api"
    production = "\\web-prod01\sites\inventory-api"
}

dotnet test .\tests\InventoryApi.Tests
dotnet publish $Project -c Release -o .\publish

if ($Environment -eq "production") {
    $answer = Read-Host "Deploy to PRODUCTION? (yes/no)"
    if ($answer -ne "yes") { Write-Host "Cancelled"; exit 1 }
}

robocopy .\publish $targets[$Environment] /MIR /XF appsettings.Production.json
Write-Host "Deployed to $Environment"
