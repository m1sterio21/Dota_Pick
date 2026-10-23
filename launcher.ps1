# Запуск Dota Pick: находит Python, а если его нет — ставит с python.org (только для этого пользователя,
# без прав администратора), потом запускает программу. Библиотеки программа доставит сама при первом запуске.
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # без этого скачивание в PowerShell 5 идёт в разы медленнее
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$installerUrl = 'https://www.python.org/ftp/python/3.13.15/python-3.13.15-amd64.exe'

function Find-Python {
    $candidates = @()
    # 1) py-лаунчер
    if (Get-Command py -ErrorAction SilentlyContinue) {
        try {
            $exe = & py -3 -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $exe) { $candidates += $exe.Trim() }
        } catch {}
    }
    # 2) python в PATH (кроме заглушки Microsoft Store из WindowsApps — она не настоящий Python)
    $candidates += @(Get-Command python -All -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -notlike '*WindowsApps*' } | ForEach-Object { $_.Source })
    # 3) стандартные места установки (PATH мог ещё не обновиться после установки)
    $candidates += @(Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe",
                                   "$env:ProgramFiles\Python3*\python.exe" -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | ForEach-Object { $_.FullName })
    foreach ($c in $candidates) {
        if (-not (Test-Path $c)) { continue }
        try {
            $ok = & $c -c "import sys, tkinter; print(sys.version_info >= (3, 10))" 2>$null
            if ($ok -eq 'True') { return $c }
        } catch {}
    }
    return $null
}

function Fail($text) {
    Write-Host ""
    Write-Host $text -ForegroundColor Red
    Write-Host ""
    Read-Host "Нажми Enter, чтобы закрыть"
    exit 1
}

$python = Find-Python
if (-not $python) {
    Write-Host "Python не найден — устанавливаю его (один раз, 1-3 минуты)..." -ForegroundColor Cyan
    $installer = Join-Path $env:TEMP 'python-3.13.15-amd64.exe'
    try {
        Write-Host "  скачиваю установщик с python.org..."
        Invoke-WebRequest $installerUrl -OutFile $installer -UseBasicParsing
    } catch {
        Fail "Не получилось скачать Python. Проверь интернет или поставь вручную: https://www.python.org/downloads/ (галочка 'Add Python to PATH')."
    }
    Write-Host "  устанавливаю (окна не будет, просто подожди)..."
    $installArgs = '/quiet InstallAllUsers=0 InstallLauncherAllUsers=0 PrependPath=1 Include_launcher=1 Include_tcltk=1 Include_pip=1 Include_test=0'
    $proc = Start-Process $installer -ArgumentList $installArgs -Wait -PassThru
    Remove-Item $installer -ErrorAction SilentlyContinue
    $python = Find-Python
    if (-not $python) {
        Fail "Python не установился (код $($proc.ExitCode)). Поставь вручную: https://www.python.org/downloads/ (галочка 'Add Python to PATH')."
    }
    Write-Host "  Python установлен: $python" -ForegroundColor Green
}

$pythonw = Join-Path (Split-Path $python) 'pythonw.exe'
if (-not (Test-Path $pythonw)) { $pythonw = $python }
Write-Host "Запускаю Dota Pick..."
Start-Process $pythonw -ArgumentList "`"$here\run.pyw`"" -WorkingDirectory $here
