<#
.SYNOPSIS
    One-time setup for the Xtenda Contract Checker.

.DESCRIPTION
    Installs what the checker needs and puts a shortcut on your Desktop:
      * Python 3 (via winget, if it isn't already there)
      * the Python packages: pymupdf, pillow, pytesseract, numpy
      * Tesseract OCR (via winget) - required for scanned contracts

    Everything runs locally. No contract ever leaves the machine.

    Run it once:
        powershell -ExecutionPolicy Bypass -File ".\Install-ContractChecker.ps1"

    If winget is blocked on this laptop, install Python and Tesseract yourself
    from python.org and github.com/UB-Mannheim/tesseract/wiki, then re-run this
    script - it will skip what is already present and just do the pip installs.
#>

[CmdletBinding()]
param([switch]$NoShortcut)

$ErrorActionPreference = 'Stop'
$here = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$app  = Join-Path $here 'xtenda_contract_check.py'

function Say($m, $c = 'Gray') { Write-Host "  $m" -ForegroundColor $c }

Write-Host ""
Write-Host "Xtenda Contract Checker - setup" -ForegroundColor Cyan
Write-Host ""

if (-not (Test-Path -LiteralPath $app)) {
    throw "xtenda_contract_check.py is not next to this script. Keep the two files together."
}

# ---- Python ---------------------------------------------------------------
$py = $null
foreach ($c in @('py', 'python')) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd) {
        try {
            $v = & $c -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
            if ($v -and [version]$v -ge [version]'3.8') { $py = $c; break }
        } catch { }
    }
}

if (-not $py) {
    Say "Python not found - installing via winget..." Yellow
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        throw "winget is not available. Install Python 3 from python.org, then re-run this script."
    }
    winget install --id Python.Python.3.12 -e --accept-source-agreements --accept-package-agreements
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User')
    $py = 'py'
    if (-not (Get-Command $py -ErrorAction SilentlyContinue)) {
        throw "Python installed but not on PATH yet. Close this window, open a new one, and re-run."
    }
}
Say "Python: $(& $py -c 'import sys;print(sys.version.split()[0])')" Green

# ---- Packages -------------------------------------------------------------
Say "Installing Python packages..." Yellow
& $py -m pip install --upgrade pip --quiet
& $py -m pip install --upgrade pymupdf pillow pytesseract numpy --quiet
if ($LASTEXITCODE -ne 0) { throw "pip install failed. If you are behind the corporate proxy, set HTTPS_PROXY and re-run." }
Say "Packages: pymupdf, pillow, pytesseract, numpy" Green

# ---- Tesseract ------------------------------------------------------------
$tessPaths = @(
    "$env:ProgramFiles\Tesseract-OCR\tesseract.exe",
    "${env:ProgramFiles(x86)}\Tesseract-OCR\tesseract.exe",
    "$env:LOCALAPPDATA\Programs\Tesseract-OCR\tesseract.exe"
)
$tess = $tessPaths | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $tess -and (Get-Command tesseract -ErrorAction SilentlyContinue)) {
    $tess = (Get-Command tesseract).Source
}

if (-not $tess) {
    Say "Tesseract OCR not found - installing via winget..." Yellow
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install --id UB-Mannheim.TesseractOCR -e --accept-source-agreements --accept-package-agreements
        $tess = $tessPaths | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    }
}

if ($tess) {
    Say "Tesseract: $tess" Green
} else {
    Say "Tesseract NOT installed." Red
    Say "Scanned contracts - about half of every batch - cannot be checked without it." Red
    Say "Get it from https://github.com/UB-Mannheim/tesseract/wiki and re-run this script." Red
}

# ---- Shortcut -------------------------------------------------------------
if (-not $NoShortcut) {
    $pyw = (Get-Command pythonw -ErrorAction SilentlyContinue).Source
    if (-not $pyw) {
        $base = Split-Path (& $py -c "import sys;print(sys.executable)") -Parent
        $cand = Join-Path $base 'pythonw.exe'
        if (Test-Path -LiteralPath $cand) { $pyw = $cand }
    }
    if ($pyw) {
        $lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'Contract Checker.lnk'
        $ws  = New-Object -ComObject WScript.Shell
        $sc  = $ws.CreateShortcut($lnk)
        $sc.TargetPath       = $pyw
        $sc.Arguments        = "`"$app`""
        $sc.WorkingDirectory = $here
        $sc.Description      = 'Review and rename Xtenda PBL loan contracts'
        $sc.Save()
        Say "Desktop shortcut: Contract Checker" Green
        Say "You can also drag a folder of contracts onto that shortcut." DarkGray
    } else {
        Say "Could not find pythonw.exe - no shortcut made. Use Run-ContractChecker.bat." Yellow
    }
}

Write-Host ""
Write-Host "  Setup done." -ForegroundColor Cyan
Write-Host ""
