# PromoGen AI - one-command setup + run for Windows PowerShell.
# Usage (from the project folder):   .\start.ps1
# It always uses the project's OWN virtual environment, so "No module named uvicorn/httpx" cannot happen.
$ErrorActionPreference = 'Stop'
Set-Location (Join-Path $PSScriptRoot 'backend')

$venvDir = '.venv313'
$venvPython = Join-Path (Get-Location) "$venvDir\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    if (-not (Get-Command py -ErrorAction SilentlyContinue)) {
        throw 'Python 3.13 is required. Install Python 3.13 and run start.ps1 again.'
    }
    Write-Host 'Creating Python 3.13 virtual environment (.venv313)...' -ForegroundColor Cyan
    py -3.13 -m venv $venvDir
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the Python 3.13 virtual environment.' }
}
$venvVersion = & $venvPython -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($LASTEXITCODE -ne 0 -or $venvVersion -ne '3.13') {
    throw "The project virtual environment must use Python 3.13; found '$venvVersion'."
}
Write-Host "Installing dependencies into $venvPython..." -ForegroundColor Cyan
& $venvPython -m pip install --upgrade pip | Out-Null
& $venvPython -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw 'pip install failed - see the message above.' }

if (-not (Test-Path '.env')) { Copy-Item '.env.example' '.env'; Write-Host 'Created backend\.env from .env.example' -ForegroundColor Cyan }
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue) -or -not (Get-Command ffprobe -ErrorAction SilentlyContinue)) {
    $wingetPackages = Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages'
    $ffmpegPackage = Get-ChildItem -Path $wingetPackages -Directory -Filter 'Gyan.FFmpeg*' -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($ffmpegPackage) {
        $ffmpegBin = Get-ChildItem -Path $ffmpegPackage.FullName -Directory -Filter 'ffmpeg-*-full_build' |
            ForEach-Object { Join-Path $_.FullName 'bin' } |
            Where-Object { (Test-Path (Join-Path $_ 'ffmpeg.exe')) -and (Test-Path (Join-Path $_ 'ffprobe.exe')) } |
            Select-Object -First 1
        if ($ffmpegBin) { $env:Path = "$ffmpegBin;$env:Path" }
    }
}
if (-not (Get-Command ffmpeg -ErrorAction SilentlyContinue)) {
    Write-Warning 'FFmpeg was not found on PATH. Script generation works, but video rendering needs it:  winget install Gyan.FFmpeg   (then restart PowerShell)'
}
Write-Host 'Tip: Telugu/Hindi/Tamil video text needs a font with those glyphs (Windows ships Nirmala UI). If the Studio reports a missing font, copy a Noto Sans .ttf into backend\fonts.' -ForegroundColor DarkGray
Write-Host 'Starting PromoGen AI at http://127.0.0.1:8000  (API docs: http://127.0.0.1:8000/docs)' -ForegroundColor Green
& $venvPython -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
