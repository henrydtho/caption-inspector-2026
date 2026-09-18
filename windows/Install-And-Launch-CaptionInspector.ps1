[CmdletBinding()]
param(
    [switch]$SkipHomebrew,
    [switch]$OfflineOnly,
    [switch]$Web,
    [string]$InstallerCacheDir = "",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$AppArgs = @()
)

$ErrorActionPreference = "Stop"

try {
    Add-Type -AssemblyName System.Windows.Forms -ErrorAction SilentlyContinue | Out-Null
}
catch {
}

function Write-Step {
    param([string]$Message)
    Write-Host "`n==> $Message" -ForegroundColor Cyan
}

function Show-Info {
    param([string]$Message)

    if ([System.Type]::GetType("System.Windows.Forms.MessageBox, System.Windows.Forms")) {
        [System.Windows.Forms.MessageBox]::Show($Message, "Caption Inspector", "OK", "Information") | Out-Null
    }
    else {
        Write-Host $Message -ForegroundColor Green
    }
}

function Show-ErrorMessage {
    param([string]$Message)

    if ([System.Type]::GetType("System.Windows.Forms.MessageBox, System.Windows.Forms")) {
        [System.Windows.Forms.MessageBox]::Show($Message, "Caption Inspector Setup Failed", "OK", "Error") | Out-Null
    }
    else {
        Write-Host $Message -ForegroundColor Red
    }
}

function Test-WingetAvailable {
    return [bool](Get-Command winget -ErrorAction SilentlyContinue)
}

function Get-InstallerCacheDir {
    if ($InstallerCacheDir -and $InstallerCacheDir.Trim() -ne "") {
        return $InstallerCacheDir
    }

    return (Join-Path $PSScriptRoot "installers")
}

function Resolve-InstallerPath {
    param([string]$FileName)

    $cacheDir = Get-InstallerCacheDir
    $candidate = Join-Path $cacheDir $FileName
    if (Test-Path $candidate) {
        return $candidate
    }

    return $null
}

function Test-PythonReady {
    $pyCommand = Get-Command py -ErrorAction SilentlyContinue
    if (-not $pyCommand) {
        return $false
    }

    & py -3 -c "import tkinter" | Out-Null
    return ($LASTEXITCODE -eq 0)
}

function Ensure-Admin {
    $currentUser = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($currentUser)
    if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        return
    }

    Write-Host "Requesting administrator access..." -ForegroundColor Yellow
    $argList = @(
        "-NoProfile"
        "-ExecutionPolicy", "Bypass"
        "-File", ('"{0}"' -f $PSCommandPath)
    )

    if ($SkipHomebrew) {
        $argList += "-SkipHomebrew"
    }

    if ($OfflineOnly) {
        $argList += "-OfflineOnly"
    }

    if ($Web) {
        $argList += "-Web"
    }

    if ($InstallerCacheDir -and $InstallerCacheDir.Trim() -ne "") {
        $argList += ("-InstallerCacheDir `"{0}`"" -f $InstallerCacheDir)
    }

    if ($AppArgs -and $AppArgs.Count -gt 0) {
        $argList += $AppArgs
    }

    Start-Process -FilePath "powershell.exe" -Verb RunAs -ArgumentList ($argList -join " ") | Out-Null
    exit 0
}

function Install-PythonWithWinget {
    Write-Step "Installing Python 3 via winget"
    & winget install --id Python.Python.3.12 -e --accept-package-agreements --accept-source-agreements --silent
    return ($LASTEXITCODE -eq 0)
}

function Install-PythonDirect {
    param([switch]$AllowDownload)

    Write-Step "Installing Python 3 directly from python.org"
    $cachedInstaller = Resolve-InstallerPath -FileName "python-3.12.10-amd64.exe"
    $installerPath = $cachedInstaller
    $pythonUrl = "https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe"

    if (-not $installerPath) {
        if (-not $AllowDownload) {
            throw "Python installer not found in offline cache. Place python-3.12.10-amd64.exe in $(Get-InstallerCacheDir)."
        }

        $installerPath = Join-Path $env:TEMP "python-3.12.10-amd64.exe"
        Invoke-WebRequest -Uri $pythonUrl -OutFile $installerPath
    }

    & $installerPath /quiet InstallAllUsers=1 PrependPath=1 Include_launcher=1 Include_tcltk=1
    if ($LASTEXITCODE -ne 0) {
        throw "Python direct installer failed."
    }
}

function Ensure-Python {
    if (Test-PythonReady) {
        return
    }

    $installed = $false

    if ($OfflineOnly) {
        Install-PythonDirect
    }
    else {
        if (Test-WingetAvailable) {
            $installed = Install-PythonWithWinget
        }

        if (-not $installed) {
            Install-PythonDirect -AllowDownload
        }
    }

    if (-not (Test-PythonReady)) {
        throw "Python is still unavailable, or tkinter support is missing. Install Python 3.12 from python.org and retry."
    }
}

function Install-Msys2WithWinget {
    Write-Step "Installing MSYS2 via winget"
    & winget install --id MSYS2.MSYS2 -e --accept-package-agreements --accept-source-agreements --silent
    return ($LASTEXITCODE -eq 0)
}

function Install-Msys2Direct {
    param([switch]$AllowDownload)

    Write-Step "Installing MSYS2 directly from GitHub releases"
    $cachedInstaller = Resolve-InstallerPath -FileName "msys2-x86_64-latest.exe"
    $installerPath = $cachedInstaller
    $msysUrl = "https://github.com/msys2/msys2-installer/releases/latest/download/msys2-x86_64-latest.exe"

    if (-not $installerPath) {
        if (-not $AllowDownload) {
            throw "MSYS2 installer not found in offline cache. Place msys2-x86_64-latest.exe in $(Get-InstallerCacheDir)."
        }

        $installerPath = Join-Path $env:TEMP "msys2-x86_64-latest.exe"
        Invoke-WebRequest -Uri $msysUrl -OutFile $installerPath
    }

    & $installerPath in --confirm-command --accept-messages --root C:\msys64
    if ($LASTEXITCODE -ne 0) {
        throw "MSYS2 direct installer failed."
    }
}

function Ensure-Msys2 {
    if (Test-Path "C:\msys64\usr\bin\bash.exe") {
        return
    }

    $installed = $false

    if ($OfflineOnly) {
        Install-Msys2Direct
    }
    else {
        if (Test-WingetAvailable) {
            $installed = Install-Msys2WithWinget
        }

        if (-not $installed) {
            Install-Msys2Direct -AllowDownload
        }
    }

    if (Test-Path "C:\msys64\usr\bin\bash.exe") {
        return
    }

    throw "MSYS2 install did not finish successfully. Please install MSYS2 manually and retry."
}

function Run-MsysCommand {
    param([string]$Command)

    & "C:\msys64\usr\bin\bash.exe" -lc $Command
    if ($LASTEXITCODE -ne 0) {
        throw "MSYS2 command failed: $Command"
    }
}

function Test-MsysBuildDepsInstalled {
    if (-not (Test-Path "C:\msys64\usr\bin\bash.exe")) {
        return $false
    }

    & "C:\msys64\usr\bin\bash.exe" -lc "export PATH=/ucrt64/bin:`$PATH; command -v clang >/dev/null 2>&1 && command -v make >/dev/null 2>&1 && command -v pkg-config >/dev/null 2>&1 && command -v ffmpeg >/dev/null 2>&1"
    return ($LASTEXITCODE -eq 0)
}

function Ensure-BuildDependencies {
    if ($OfflineOnly) {
        if (-not (Test-MsysBuildDepsInstalled)) {
            throw "Offline mode requires preinstalled MSYS2 packages: clang, make, pkg-config, ffmpeg. Install them ahead of time or run without -OfflineOnly once on a networked machine."
        }

        Write-Step "Offline mode: build dependencies already present"
        return
    }

    Write-Step "Installing compiler and ffmpeg build dependencies in MSYS2"
    Run-MsysCommand "pacman -Sy --noconfirm"
    $pkgInstall = @(
        "pacman -S --noconfirm --needed"
        "mingw-w64-ucrt-x86_64-clang"
        "mingw-w64-ucrt-x86_64-make"
        "mingw-w64-ucrt-x86_64-pkgconf"
        "mingw-w64-ucrt-x86_64-ffmpeg"
    ) -join " "
    Run-MsysCommand $pkgInstall
}

function Ensure-HomebrewInWsl {
    if ($SkipHomebrew) {
        Write-Host "Skipping optional Homebrew setup in WSL." -ForegroundColor DarkYellow
        return
    }

    $wslCmd = Get-Command wsl.exe -ErrorAction SilentlyContinue
    if (-not $wslCmd) {
        Write-Host "WSL is not available, skipping optional Homebrew setup." -ForegroundColor DarkYellow
        return
    }

    Write-Step "Attempting optional Homebrew setup in WSL"

    try {
        & wsl.exe -e bash -lc "command -v brew >/dev/null 2>&1 || NONINTERACTIVE=1 /bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\""
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Could not complete Homebrew setup in WSL. Continuing without it." -ForegroundColor DarkYellow
            return
        }
    }
    catch {
        Write-Host "Homebrew setup in WSL failed. Continuing without it." -ForegroundColor DarkYellow
    }
}

function Build-CaptionInspectorLibrary {
    param([string]$RepoRoot)

    Write-Step "Building Caption Inspector shared library for Windows"
    $buildCmd = @(
        "export PATH=/ucrt64/bin:`$PATH"
        ('cd "{0}/src"' -f $RepoRoot.Replace("\", "/"))
        "make sharedlib"
    ) -join "; "

    Run-MsysCommand $buildCmd
}

function Install-PythonDependencies {
    param([string]$RepoRoot)

    Write-Step "Installing Python dependencies"
    & py -3 -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to upgrade pip."
    }

    & py -3 -m pip install -r (Join-Path $RepoRoot "python\requirements-app.txt")
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to install Python app dependencies."
    }
}

function Launch-App {
    param([string]$RepoRoot, [switch]$Web, [string[]]$ExtraArgs = @())

    $launchArgs = @("python\launch_app.py")
    if ($Web) {
        Write-Step "Launching Caption Inspector Streamlit web app"
        $launchArgs += "--web"
    }
    else {
        Write-Step "Launching Caption Inspector desktop app"
    }

    if ($ExtraArgs.Count -gt 0) {
        $launchArgs += $ExtraArgs
    }

    Push-Location $RepoRoot
    try {
        & py -3 @launchArgs
        if ($LASTEXITCODE -ne 0) {
            throw "Caption Inspector app failed to launch."
        }
    }
    finally {
        Pop-Location
    }
}

Ensure-Admin

$repoRoot = Split-Path -Parent $PSScriptRoot

try {
    Ensure-Python
    Ensure-Msys2
    Ensure-BuildDependencies
    Ensure-HomebrewInWsl
    Build-CaptionInspectorLibrary -RepoRoot $repoRoot
    Install-PythonDependencies -RepoRoot $repoRoot
    Launch-App -RepoRoot $repoRoot -Web:$Web -ExtraArgs $AppArgs

    Write-Host "`nCaption Inspector is ready on Windows." -ForegroundColor Green
    Show-Info "Caption Inspector is installed and launched successfully."

}
catch {
    $message = "Setup failed.`n`n$($_.Exception.Message)`n`nPlease share this message with support."
    Write-Host $message -ForegroundColor Red
    Show-ErrorMessage $message
    exit 1
}
