<#
.SYNOPSIS
  claude-speed Windows 安装器:开机自启的托盘程序 + 可选 statusline 接线。幂等,可重复运行。

.EXAMPLE
  .\install.ps1                       # 仅托盘程序(写入 shell:startup 快捷方式并立即启动)
  .\install.ps1 -Statusline           # 同时把 statusline-speed.py 接进 ~/.claude/settings.json
  .\install.ps1 -Remotes 'user@host'  # 写用户级环境变量 CLAUDE_SPEED_REMOTES(供 collect.py 用)
  .\install.ps1 -Remotes ''           # 删除该环境变量
  .\install.ps1 -NoAutostart          # 不建快捷方式,只启动一次
  .\install.ps1 -DryRun               # 只打印将要做的事,不写任何东西

.PARAMETER Python
  指定 python.exe;默认自动探测(CLAUDE_SPEED_PYTHON → python → py -3)。
#>
[CmdletBinding()]
param(
    [switch]$Statusline,
    [string]$Python = '',
    [string]$Remotes,
    [switch]$NoAutostart,
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }   # 中文/emoji 提示在 GBK 控制台也别乱码
$Dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Tray = Join-Path $Dir 'ClaudeSpeed.ps1'
$StatuslinePy = Join-Path $Dir 'statusline-speed.py'
$LinkPath = Join-Path ([Environment]::GetFolderPath('Startup')) 'ClaudeSpeed.lnk'
$Settings = Join-Path $HOME '.claude\settings.json'
$Prefix = if ($DryRun) { '[dry-run] ' } else { '' }

function Step([string]$m) { Write-Host "==> $Prefix$m" }

# ---------- Python ----------
function Test-PythonExe([string]$Exe, [string[]]$PreArgs) {
    try {
        $cmd = Get-Command $Exe -ErrorAction Stop
        $path = $cmd.Source; if (-not $path) { $path = $cmd.Path }
        if ($path -like '*\WindowsApps\*') { return $null }
        $out = & $path @PreArgs --version 2>&1
        if ($LASTEXITCODE -eq 0 -and "$out" -match 'Python 3') {
            return [pscustomobject]@{ Exe = $path; PreArgs = @($PreArgs); Version = "$out".Trim() }
        }
    } catch { }
    return $null
}
$py = $null
$cands = @()
if ($Python) { $cands += ,@($Python, @()) }
if ($env:CLAUDE_SPEED_PYTHON) { $cands += ,@($env:CLAUDE_SPEED_PYTHON, @()) }
$cands += ,@('python', @()); $cands += ,@('py', @('-3'))
foreach ($c in $cands) { $py = Test-PythonExe $c[0] $c[1]; if ($py) { break } }
if (-not $py) {
    Write-Host 'ERROR: 找不到可用的 Python 3(python / py -3 均不可用,或只是 Microsoft Store 占位 stub)。' -ForegroundColor Red
    Write-Host '       请安装 https://www.python.org/downloads/windows/ 后重试,或用 -Python 指定路径。'
    exit 1
}
Step ("Python: {0} {1} ({2})" -f $py.Exe, ($py.PreArgs -join ' '), $py.Version)

# ---------- 宿主 ----------
$hostExe = $null
$pw = Get-Command pwsh.exe -ErrorAction SilentlyContinue
if ($pw) { $hostExe = $pw.Source }
if (-not $hostExe) { $hostExe = Join-Path $PSHOME 'powershell.exe' }
if (-not (Test-Path $hostExe)) { $hostExe = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" }
$trayArgs = "-NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$Tray`""
if ($Python) { $trayArgs += " -Python `"$($py.Exe)`"" }
Step "宿主: $hostExe"

# ---------- 结束已运行实例 ----------
function Stop-Instances {
    $procs = Get-CimInstance Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe'" |
        Where-Object { $_.CommandLine -like '*ClaudeSpeed.ps1*' -and $_.ProcessId -ne $PID }
    foreach ($p in $procs) {
        Step "结束旧实例 PID $($p.ProcessId)"
        if (-not $DryRun) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    }
    if (-not $DryRun -and $procs) { Start-Sleep -Milliseconds 500 }
}
Stop-Instances

# ---------- 环境变量 CLAUDE_SPEED_REMOTES ----------
if ($PSBoundParameters.ContainsKey('Remotes')) {
    if ([string]::IsNullOrWhiteSpace($Remotes)) {
        Step '删除用户环境变量 CLAUDE_SPEED_REMOTES'
        if (-not $DryRun) { [Environment]::SetEnvironmentVariable('CLAUDE_SPEED_REMOTES', $null, 'User') }
        $env:CLAUDE_SPEED_REMOTES = $null
    } else {
        Step "写用户环境变量 CLAUDE_SPEED_REMOTES = $Remotes"
        if (-not $DryRun) { [Environment]::SetEnvironmentVariable('CLAUDE_SPEED_REMOTES', $Remotes, 'User') }
        $env:CLAUDE_SPEED_REMOTES = $Remotes   # 本进程也带上,Start-Process 的子进程才能继承
    }
}

# ---------- 开机自启快捷方式 ----------
if (-not $NoAutostart) {
    Step "创建快捷方式 $LinkPath"
    if (-not $DryRun) {
        $ws = New-Object -ComObject WScript.Shell
        $lnk = $ws.CreateShortcut($LinkPath)
        $lnk.TargetPath = $hostExe
        $lnk.Arguments = $trayArgs
        $lnk.WorkingDirectory = $Dir
        $lnk.WindowStyle = 7
        $lnk.Description = 'claude-speed tray'
        $lnk.Save()
    }
} else {
    Step '跳过开机自启(-NoAutostart)'
}

# ---------- statusline ----------
if ($Statusline) {
    $cmdLine = "`"$($py.Exe)`" $($py.PreArgs -join ' ') `"$StatuslinePy`"" -replace '\s{2,}', ' '
    Step "接线 statusline → $Settings (备份 .bak)"
    Step "  command = $cmdLine"
    if (-not $DryRun) {
        $obj = [pscustomobject]@{}
        if (Test-Path $Settings) {
            Copy-Item $Settings "$Settings.bak" -Force
            $raw = Get-Content $Settings -Raw -Encoding UTF8
            if ($raw.Trim()) { $obj = $raw | ConvertFrom-Json }
        } else {
            New-Item -ItemType Directory -Force (Split-Path $Settings) | Out-Null
        }
        $sl = [pscustomobject]@{ type = 'command'; command = $cmdLine; padding = 0 }
        if ($obj.PSObject.Properties['statusLine']) { $obj.statusLine = $sl }
        else { $obj | Add-Member -NotePropertyName statusLine -NotePropertyValue $sl }
        $json = $obj | ConvertTo-Json -Depth 20
        [System.IO.File]::WriteAllText($Settings, $json, [System.Text.UTF8Encoding]::new($false))
    }
}

# ---------- 启动 ----------
Step "启动托盘: $hostExe $trayArgs"
if (-not $DryRun) {
    Start-Process -FilePath $hostExe -ArgumentList $trayArgs -WorkingDirectory $Dir -WindowStyle Hidden
    Start-Sleep -Seconds 2
    $alive = Get-CimInstance Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe'" |
        Where-Object { $_.CommandLine -like '*ClaudeSpeed.ps1*' }
    if ($alive) { Write-Host "Done. 托盘应出现 ⚪/🟢 图标(PID $($alive.ProcessId -join ','))。" }
    else { Write-Host 'WARN: 托盘进程 2 秒内退出了,可加 -LogFile 手动运行 ClaudeSpeed.ps1 排查。' -ForegroundColor Yellow }
} else {
    Write-Host 'Dry-run 完成,未做任何修改。'
}
