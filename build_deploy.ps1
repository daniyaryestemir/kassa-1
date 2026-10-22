$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$DeployDir = Join-Path $ProjectRoot "deploy"
$ArchivePath = Join-Path $DeployDir "sait-deploy.zip"
$TempDir = Join-Path $DeployDir "package"

if (Test-Path $TempDir) {
    Remove-Item -LiteralPath $TempDir -Recurse -Force
}

New-Item -ItemType Directory -Path $TempDir | Out-Null

$excludeDirs = @("__pycache__", ".git", ".venv", "venv", "env", "deploy")
$excludeFiles = @("flask_server.log", "flask_server.err.log", "sait.rar")
$backupPrefix = [string]([char]0x0440) + [string]([char]0x0435) + [string]([char]0x0437)

Get-ChildItem -LiteralPath $ProjectRoot -Force -Recurse | ForEach-Object {
    $relativePath = $_.FullName.Substring($ProjectRoot.Length).TrimStart("\")
    $parts = $relativePath -split "\\"

    foreach ($part in $parts) {
        if (($excludeDirs -contains $part) -or ($part -like "$backupPrefix*")) {
            return
        }
    }

    if ($_.PSIsContainer) {
        return
    }

    if ($excludeFiles -contains $_.Name) {
        return
    }

    $targetPath = Join-Path $TempDir $relativePath
    $targetDir = Split-Path -Parent $targetPath
    if (-not (Test-Path $targetDir)) {
        New-Item -ItemType Directory -Path $targetDir | Out-Null
    }

    Copy-Item -LiteralPath $_.FullName -Destination $targetPath -Force
}

if (Test-Path $ArchivePath) {
    Remove-Item -LiteralPath $ArchivePath -Force
}

Compress-Archive -Path (Join-Path $TempDir "*") -DestinationPath $ArchivePath -Force
Remove-Item -LiteralPath $TempDir -Recurse -Force

Write-Host "Done: $ArchivePath"
