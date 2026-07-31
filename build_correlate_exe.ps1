param(
    [switch]$Clean,
    [switch]$SkipInstall,
    [switch]$SkipTests,
    [string]$ReleaseDir = "release_pyinstaller"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$workspaceRoot = Split-Path -Parent $scriptRoot
$venvPythonCandidates = @(
    (Join-Path $scriptRoot ".venv/Scripts/python.exe"),
    (Join-Path $workspaceRoot ".venv/Scripts/python.exe")
)
$venvPython = $venvPythonCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
$specPath = Join-Path $scriptRoot "correlate_pyinstaller.spec"
$distRoot = Join-Path $scriptRoot "dist_pyinstaller"
$buildRoot = Join-Path $scriptRoot "build_pyinstaller"
$releaseRoot = Join-Path $scriptRoot $ReleaseDir
$exeSource = Join-Path $distRoot "CorreLaTE.exe"
$releaseExe = Join-Path $releaseRoot "CorreLaTE.exe"
$releaseReadme = Join-Path $scriptRoot "README_STANDALONE.txt"
$chipManifestTemplate = Join-Path $scriptRoot "src/cv_ate_correlation/assets/CorreLaTE_Chips_Manifest.xlsx"
$projectConfiguration = Get-Content (Join-Path $scriptRoot "pyproject.toml") -Raw
$versionMatch = [regex]::Match($projectConfiguration, '(?m)^version\s*=\s*"([^"]+)"\s*$')
if (-not $versionMatch.Success) {
    throw "Could not resolve the CorreLaTE version from pyproject.toml"
}
$applicationVersion = $versionMatch.Groups[1].Value

if (-not $venvPython) {
    throw "CorreLaTE virtual-environment Python was not found. Checked: $($venvPythonCandidates -join ', ')"
}
if (-not [Environment]::Is64BitOperatingSystem) {
    throw "CorreLaTE deployment requires 64-bit Windows"
}
if (-not (Test-Path $chipManifestTemplate)) {
    throw "Section 2 chip-manifest template was not found: $chipManifestTemplate"
}

if ($Clean) {
    foreach ($path in @($buildRoot, $distRoot, $releaseRoot)) {
        if (Test-Path $path) {
            Remove-Item -Recurse -Force $path
        }
    }
}

if (-not $SkipInstall) {
    & $venvPython -m pip install --upgrade pip
    & $venvPython -m pip install -e "$scriptRoot[test]"
    & $venvPython -m pip install -r (Join-Path $scriptRoot "requirements-pyinstaller-build.txt")
    if ($LASTEXITCODE -ne 0) {
        throw "Dependency installation failed with exit code $LASTEXITCODE"
    }
}

if (-not $SkipTests) {
    Push-Location $scriptRoot
    try {
        & $venvPython -m pytest -q
        if ($LASTEXITCODE -ne 0) {
            throw "Test suite failed with exit code $LASTEXITCODE"
        }
    }
    finally {
        Pop-Location
    }
}

Push-Location $scriptRoot
try {
    & $venvPython -m PyInstaller `
        --noconfirm `
        --clean `
        --distpath $distRoot `
        --workpath $buildRoot `
        $specPath
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }
}
finally {
    Pop-Location
}

if (-not (Test-Path $exeSource)) {
    throw "PyInstaller did not create the expected executable: $exeSource"
}
if ((Get-Item $exeSource).Length -le 0) {
    throw "The generated CorreLaTE executable is empty"
}

& $exeSource --smoke-test
if ($LASTEXITCODE -ne 0) {
    $logPath = Join-Path $(if ($env:LOCALAPPDATA) { $env:LOCALAPPDATA } else { $env:APPDATA }) "CorreLaTE/logs/startup-error.log"
    throw "Frozen executable smoke test failed with exit code $LASTEXITCODE. Diagnostic log: $logPath"
}

New-Item -ItemType Directory -Force -Path $releaseRoot | Out-Null
Copy-Item $exeSource $releaseExe -Force
$readmeContent = (Get-Content $releaseReadme -Raw).Replace("{{VERSION}}", $applicationVersion)
$readmeContent | Set-Content (Join-Path $releaseRoot "README.txt") -Encoding UTF8
Copy-Item $chipManifestTemplate (Join-Path $releaseRoot "CorreLaTE_Chips_Manifest.xlsx") -Force

$exeHash = (Get-FileHash -Path $releaseExe -Algorithm SHA256).Hash.ToLowerInvariant()
"SHA256  CorreLaTE.exe  $exeHash" | Set-Content `
    -Path (Join-Path $releaseRoot "CorreLaTE.exe.sha256.txt") `
    -Encoding ASCII

Write-Host "CorreLaTE standalone release created at: $releaseRoot"
Write-Host "Executable: $releaseExe"
Write-Host "SHA256:    $exeHash"
