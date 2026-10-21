# Building Argus for distribution on Windows: a separate folder, both build "flavor" variables, the distribution stage.
#   powershell -ExecutionPolicy Bypass -File packaging\build_win.ps1            # output: ..\_winbuild
#   powershell -ExecutionPolicy Bypass -File packaging\build_win.ps1 -Out D:\x  # to your own folder
#
# Why a script and not three commands in the README. Without ARGUS_BUILD_NO_SUBSCRIPTION,
# claude.exe goes into the build (about 217 MB, the subscription must not be distributed),
# and without ARGUS_BUILD_API_ONLY, the local recognition engines go in: a forgotten
# variable produced exactly such an archive, and there was no way to notice it. Here both
# variables are always set (like `build_macos.sh` on Mac), and the publish step
# (`deploy/win/publish_win.py`) then checks the folder anyway.
#
# It builds NOT into `dist\`: the developer's own program may be running there, and
# PyInstaller cleans the destination folder.
param(
    [string]$Out = ""
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
if (-not $Out) { $Out = Join-Path (Split-Path -Parent $Root) '_winbuild' }
if ((Resolve-Path -LiteralPath $Root).Path -eq [System.IO.Path]::GetFullPath($Out)) { throw "Out must not be the project folder" }

$Python = Join-Path $Root '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $Python)) { throw "$Python is missing: set up the project environment first" }

# The version is computed once and handed to stage.py (as in the Mac build): the timestamp
# added when the working folder has edits must not differ between separate calls.
Remove-Item Env:\ARGUS_FULL_VERSION -ErrorAction SilentlyContinue
# The project root is passed as an argument instead of being taken from the current
# folder: the script can be started from anywhere, and the version of a foreign folder (or
# an import error) would be silently taken for this build's version.
$FullVersion = & $Python -c "import sys, pathlib; root = pathlib.Path(sys.argv[1]); sys.path.insert(0, str(root / 'packaging')); import stage; print(stage.build_version(root))" $Root
if ($LASTEXITCODE -or -not $FullVersion) { throw "could not compute the build version" }
$env:ARGUS_FULL_VERSION = "$FullVersion".Trim()
$env:ARGUS_BUILD_API_ONLY = '1'
$env:ARGUS_BUILD_NO_SUBSCRIPTION = '1'
Write-Host "version: $($env:ARGUS_FULL_VERSION); building into $Out"

Push-Location $Root
try {
    & $Python -m PyInstaller --noconfirm --distpath (Join-Path $Out 'dist') --workpath (Join-Path $Out 'work') packaging\argus.spec
    if ($LASTEXITCODE) { throw "PyInstaller exited with code $LASTEXITCODE" }
    $App = Join-Path $Out 'dist\Argus'
    & $Python packaging\stage.py --public --app $App
    if ($LASTEXITCODE) { throw "stage.py exited with code $LASTEXITCODE" }
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "done: $App"
Write-Host "next: python deploy\win\publish_win.py --app `"$App`" --check   (check and archive without uploading)"
