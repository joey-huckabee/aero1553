# Build aero1553 on a disconnected Windows x64 machine (docs/BUILDING.md).
#
#     powershell -ExecutionPolicy Bypass -File source\scripts\offline-build.ps1 [STEP...]
#
# Run from anywhere, on a bundle made by scripts/offline-bundle.py. STEP is one or
# more of the following, in order (default: all of them):
#
#   check    every file matches the bundle's SHA256SUMS
#   exe      the release exe, C runtime linked in  -> BUNDLE\out\aero1553-x86_64-windows.exe
#   wheel    the Python wheel and sdist            -> BUNDLE\out\*.whl, *.tar.gz
#   verify   the exe imports no C runtime DLL, the conformance suite passes against it,
#            and it behaves as the release exe when the bundle carries one
#
# Needs, installed beforehand (docs/BUILDING.md says how, offline): Rust from the
# bundle's toolchains\ installer, Visual Studio Build Tools (C++), and Python 3.10+
# for wheel and verify. Nothing is downloaded: Cargo reads the bundle's vendored
# crates (BUNDLE\.cargo) with --offline, and its cache is BUNDLE\.cargo-home. CI runs
# this script (the offline-build-windows job), so the steps are proven.
# Written for Windows PowerShell 5.1, which every supported Windows has.

param(
    # Bare words after the script name (``offline-build.ps1 exe verify``); with -File,
    # only a remaining-arguments parameter collects more than one of them.
    [Parameter(ValueFromRemainingArguments = $true)] [string[]] $Steps
)
if (-not $Steps) { $Steps = @('check', 'exe', 'wheel', 'verify') }

$ErrorActionPreference = 'Stop'
$Bundle = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Source = Join-Path $Bundle 'source'
$Out = Join-Path $Bundle 'out'
$env:CARGO_HOME = Join-Path $Bundle '.cargo-home'
New-Item -ItemType Directory -Force -Path $Out | Out-Null

function Say([string] $Text) { Write-Host ''; Write-Host "== $Text" }
function Die([string] $Text) { Write-Error "offline-build: $Text"; exit 1 }

# Run a native command and stop on a non-zero exit, which PowerShell 5.1 does not do.
function Invoke-Checked {
    param([string] $Exe, [string[]] $Arguments)
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { Die "$Exe $($Arguments -join ' ') exited $LASTEXITCODE" }
}

function Require([string] $Command, [string] $Why) {
    if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) { Die "$Command not found: $Why" }
}

function Step-Check {
    Say 'check: every file against SHA256SUMS'
    $bad = 0
    foreach ($line in Get-Content (Join-Path $Bundle 'SHA256SUMS')) {
        $want, $name = $line -split '  ', 2
        $path = Join-Path $Bundle ($name -replace '/', '\')
        if (-not (Test-Path -LiteralPath $path)) { Write-Host "missing: $name"; $bad++; continue }
        $got = (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash.ToLower()
        if ($got -ne $want) { Write-Host "changed: $name"; $bad++ }
    }
    if ($bad -gt 0) { Die "$bad file(s) damaged or missing" }
    Write-Host 'all files match'
}

function Step-Exe {
    Say 'exe: the Windows x64 release exe'
    Require 'cargo' 'install Rust from the bundle (toolchains\rust-*-x86_64-pc-windows-msvc.msi)'
    Invoke-Checked 'cargo' @('--version')
    Push-Location (Join-Path $Source 'rust')
    try {
        # rust\.cargo\config.toml links the C runtime statically (L3-RS-020).
        Invoke-Checked 'cargo' @('build', '--release', '--locked', '--offline')
    } finally { Pop-Location }
    $exe = Join-Path $Out 'aero1553-x86_64-windows.exe'
    Copy-Item -Force (Join-Path $Source 'rust\target\release\aero1553.exe') $exe
    Invoke-Checked $exe @('--version')
}

function Step-Wheel {
    Say 'wheel: the Python wheel and sdist'
    $wheel = Get-ChildItem (Join-Path $Bundle 'wheels\maturin-*-win_amd64.whl') | Select-Object -First 1
    if (-not $wheel) { Die 'the bundle has no Windows maturin wheel' }
    # maturin's wheel is one executable; take it out rather than pip-installing it.
    # Expand-Archive accepts only a .zip name.
    $tools = Join-Path $Bundle '.tools'
    $zip = Join-Path $tools 'maturin.zip'
    New-Item -ItemType Directory -Force -Path $tools | Out-Null
    Copy-Item -Force $wheel.FullName $zip
    Expand-Archive -Force -Path $zip -DestinationPath (Join-Path $tools 'maturin-wheel')
    $maturin = Get-ChildItem -Recurse (Join-Path $tools 'maturin-wheel') -Filter 'maturin.exe' |
        Select-Object -First 1
    if (-not $maturin) { Die 'no maturin.exe in the maturin wheel' }
    Push-Location (Join-Path $Source 'python')
    try {
        Invoke-Checked $maturin.FullName @('build', '--release', '--locked', '--offline', '-o', $Out)
        Invoke-Checked $maturin.FullName @('sdist', '-o', $Out)
    } finally { Pop-Location }
    Get-ChildItem $Out -Include '*.whl', '*.tar.gz' -Recurse | ForEach-Object { Write-Host $_.Name }
}

function Step-Verify {
    Say 'verify'
    Require 'python' 'verify needs Python 3.10 or later'
    $exe = Join-Path $Out 'aero1553-x86_64-windows.exe'
    if (-not (Test-Path $exe)) { Die 'nothing to verify: run the exe step first' }
    Invoke-Checked 'python' @((Join-Path $Source 'scripts\pe-imports.py'), $exe)
    Write-Host 'conformance suite:'
    Invoke-Checked 'python' @((Join-Path $Source 'tests\conformance\run.py'), '--only', 'rust',
        '--rust-bin', $exe)
    $release = Join-Path $Bundle 'release\aero1553-x86_64-windows.exe'
    if (Test-Path $release) {
        Write-Host 'comparison with the release exe (same behaviour expected; see BUILDING.md):'
        Invoke-Checked 'python' @((Join-Path $Source 'scripts\compare-binaries.py'), '--behaviour-only',
            $release, $exe)
    }
}

foreach ($step in $Steps) {
    switch ($step) {
        'check' { Step-Check }
        'exe' { Step-Exe }
        'wheel' { Step-Wheel }
        'verify' { Step-Verify }
        default { Die "unknown step '$step' (check, exe, wheel, verify)" }
    }
}
Say "done: the results are in $Out"
