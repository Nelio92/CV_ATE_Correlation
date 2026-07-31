param(
    [string]$Version,
    [string]$ReleaseDir = "release_pyinstaller",
    [string]$PackageDir = "release_packages",
    [switch]$SkipBuild
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$workspaceRoot = Split-Path -Parent $scriptRoot
$projectConfiguration = Get-Content (Join-Path $scriptRoot "pyproject.toml") -Raw
$versionMatch = [regex]::Match($projectConfiguration, '(?m)^version\s*=\s*"([^"]+)"\s*$')
if (-not $versionMatch.Success) {
    throw "Could not resolve the CorreLaTE version from pyproject.toml"
}
$projectVersion = "v$($versionMatch.Groups[1].Value)"
if (-not $Version) {
    if ($env:GITHUB_REF_TYPE -eq "tag" -and $env:GITHUB_REF_NAME -match '^correlate-(v\d+\.\d+\.\d+)$') {
        $Version = $Matches[1]
    }
    else {
        $Version = $projectVersion
    }
}
if ($Version -notmatch '^v\d+\.\d+\.\d+$') {
    throw "Release version must use v<major>.<minor>.<patch>, for example v0.1.0"
}
if ($Version -ne $projectVersion) {
    throw "Release version '$Version' does not match pyproject.toml version '$projectVersion'"
}

$releaseRoot = Join-Path $scriptRoot $ReleaseDir
$packageRoot = Join-Path $scriptRoot $PackageDir
$versionPackageDir = Join-Path $packageRoot $Version
$zipName = "CorreLaTE-$Version-windows-x64.zip"
$zipPath = Join-Path $versionPackageDir $zipName
$zipHashPath = Join-Path $versionPackageDir "$zipName.sha256.txt"
$metadataPath = Join-Path $versionPackageDir "release-metadata.json"

if (-not $SkipBuild) {
    & (Join-Path $scriptRoot "build_correlate_exe.ps1") -Clean -ReleaseDir $ReleaseDir
    if ($LASTEXITCODE -ne 0) {
        throw "CorreLaTE build failed with exit code $LASTEXITCODE"
    }
}
if (-not (Test-Path (Join-Path $releaseRoot "CorreLaTE.exe"))) {
    throw "Staged CorreLaTE release was not found: $releaseRoot"
}

New-Item -ItemType Directory -Force -Path $versionPackageDir | Out-Null
if (Test-Path $zipPath) {
    Remove-Item $zipPath -Force
}
Compress-Archive -Path (Join-Path $releaseRoot "*") -DestinationPath $zipPath -Force

$zipHash = (Get-FileHash -Path $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
"SHA256  $zipName  $zipHash" | Set-Content -Path $zipHashPath -Encoding ASCII

$originUrl = ""
$commitSha = ""
$dirty = $false
try {
    $repositoryRoot = (git -C $scriptRoot rev-parse --show-toplevel 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($repositoryRoot)) {
        throw "CorreLaTE is not inside a Git working tree"
    }

    $commitSha = (git -C $repositoryRoot rev-parse HEAD 2>$null | Out-String).Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "The source commit could not be resolved"
    }
    $dirty = -not [string]::IsNullOrWhiteSpace((git -C $repositoryRoot status --short | Out-String))

    $remoteOutput = git -C $repositoryRoot remote get-url origin 2>$null | Out-String
    if ($LASTEXITCODE -eq 0) {
        $originUrl = $remoteOutput.Trim()
    }
}
catch {
    Write-Warning "Git provenance could not be collected: $($_.Exception.Message)"
}

$metadata = [ordered]@{
    tool = "CorreLaTE"
    version = $Version
    platform = "windows-x64"
    builtAtUtc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ssZ")
    sourceRepository = $originUrl
    sourceCommit = $commitSha
    sourceWorkingTreeDirty = $dirty
    zipFile = $zipName
    sha256 = $zipHash
}
$metadata | ConvertTo-Json -Depth 4 | Set-Content -Path $metadataPath -Encoding UTF8

Write-Host "Release ZIP:      $zipPath"
Write-Host "ZIP SHA256:       $zipHash"
Write-Host "Release metadata: $metadataPath"
