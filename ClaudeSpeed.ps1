<#
.SYNOPSIS
  ClaudeSpeed — Windows 系统托盘版(对应 macOS 的 main.swift)。

.DESCRIPTION
  每 Interval 秒运行 collect.py:stdout 第 1 行 = 标题(如 🟢71 / 🟡≈45 🤖2 / ⚠️🔴22 / ⚪),
  其余行 = 下拉明细。标题被解析成动态绘制的托盘图标(灯色圆 + 白色数字 +
  ⚠️ 红角标 + 🤖 蓝点),明细进右键菜单与悬停提示。
  纯 PowerShell + WinForms,零第三方依赖;兼容 Windows PowerShell 5.1 与 pwsh 7。
  本文件必须以 UTF-8 with BOM 保存。

.PARAMETER Python
  Python 可执行文件。默认依次探测:环境变量 CLAUDE_SPEED_PYTHON、python、py -3,
  取第一个 --version 成功且不是 WindowsApps 商店占位 stub 的。
.PARAMETER Interval
  刷新间隔(秒),默认 3。
.PARAMETER Collector
  collect.py 路径,默认脚本同目录。
.PARAMETER Timeout
  单次采集超时(秒),默认 10;超时则 kill 并显示 ⚪。
.PARAMETER LogFile
  调试日志:每轮标题、图标更新与异常追加写入该文件。
#>
[CmdletBinding()]
param(
    [string]$Python = '',
    [double]$Interval = 3,
    [string]$Collector = '',
    [int]$Timeout = 10,
    [string]$LogFile = ''
)

$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms, System.Drawing

# ---------- 常量:emoji 一律由码点生成,不依赖源码字面编码 ----------
$E_GREEN  = [char]::ConvertFromUtf32(0x1F7E2)   # 🟢
$E_YELLOW = [char]::ConvertFromUtf32(0x1F7E1)   # 🟡
$E_RED    = [char]::ConvertFromUtf32(0x1F534)   # 🔴
$E_WHITE  = [char]::ConvertFromUtf32(0x26AA)    # ⚪
$E_WARN   = [char]::ConvertFromUtf32(0x26A0)    # ⚠ (⚠️ = 26A0 + FE0F)
$E_ROBOT  = [char]::ConvertFromUtf32(0x1F916)   # 🤖
$E_SIGMA  = [char]::ConvertFromUtf32(0x03A3)    # Σ
$C_APPROX = [char]0x2248                        # ≈
$C_GEQ    = [char]0x2265                        # ≥

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
if (-not $Collector) { $Collector = Join-Path $ScriptDir 'collect.py' }
$IsPS7 = $PSVersionTable.PSVersion.Major -ge 6
# NotifyIcon.Text 上限:.NET Framework 63,.NET (Core) 127,超长抛 ArgumentOutOfRange
$TipLimit = if ($IsPS7) { 127 } else { 63 }

function Write-Log([string]$msg) {
    if (-not $LogFile) { return }
    try {
        $line = '{0} {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss.fff'), $msg
        [System.IO.File]::AppendAllText($LogFile, $line + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($true))
    } catch { }
}

# ---------- 单实例 ----------
$createdNew = $false
$mutex = New-Object System.Threading.Mutex($true, 'Global\ClaudeSpeedTray', [ref]$createdNew)
if (-not $createdNew) {
    Write-Log 'another instance is running, exit'
    exit 0
}

# ---------- P/Invoke:释放 GetHicon 产生的句柄 ----------
if (-not ('ClaudeSpeed.Native' -as [type])) {
    Add-Type -Namespace ClaudeSpeed -Name Native -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll", SetLastError = true)]
public static extern bool DestroyIcon(System.IntPtr hIcon);
'@
}

# ---------- Python 探测 ----------
function Test-PythonExe {
    param([string]$Exe, [string[]]$PreArgs)
    try {
        $cmd = Get-Command $Exe -ErrorAction Stop
        $path = $cmd.Source
        if (-not $path) { $path = $cmd.Path }
        if ($path -and $path -like '*\WindowsApps\*') { return $null }   # 商店占位 stub
        $psi = New-Object System.Diagnostics.ProcessStartInfo
        $psi.FileName = $path
        $psi.Arguments = (@($PreArgs) + '--version') -join ' '
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $p = [System.Diagnostics.Process]::Start($psi)
        $out = $p.StandardOutput.ReadToEnd() + $p.StandardError.ReadToEnd()
        if (-not $p.WaitForExit(5000)) { try { $p.Kill() } catch { }; return $null }
        if ($p.ExitCode -eq 0 -and $out -match 'Python 3') {
            return [pscustomobject]@{ Exe = $path; PreArgs = @($PreArgs) }
        }
    } catch { }
    return $null
}

function Find-Python {
    param([string]$Explicit)
    $candidates = @()
    if ($Explicit) { $candidates += ,@($Explicit, @()) }
    if ($env:CLAUDE_SPEED_PYTHON) { $candidates += ,@($env:CLAUDE_SPEED_PYTHON, @()) }
    $candidates += ,@('python', @())
    $candidates += ,@('py', @('-3'))
    foreach ($c in $candidates) {
        $r = Test-PythonExe -Exe $c[0] -PreArgs $c[1]
        if ($r) { return $r }
    }
    return $null
}

$Py = Find-Python -Explicit $Python
if (-not $Py) {
    Write-Log 'python not found'
    [System.Windows.Forms.MessageBox]::Show(
        "ClaudeSpeed: 找不到可用的 Python 3。`n请安装 Python 或设置环境变量 CLAUDE_SPEED_PYTHON / 使用 -Python 参数。",
        'ClaudeSpeed', 'OK', 'Error') | Out-Null
    exit 1
}
Write-Log ("python = {0} {1}; collector = {2}; host = PS {3}" -f $Py.Exe, ($Py.PreArgs -join ' '), $Collector, $PSVersionTable.PSVersion)

# ---------- 采集:同步等待但带超时,python 通常 <300ms ----------
function Invoke-Collector {
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Py.Exe
    $args = @($Py.PreArgs) + ('"' + $Collector + '"')
    $psi.Arguments = $args -join ' '
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [System.Text.UTF8Encoding]::new($false)
    $psi.StandardErrorEncoding = [System.Text.UTF8Encoding]::new($false)
    $psi.WorkingDirectory = $ScriptDir
    # 子进程继承当前环境(含 CLAUDE_SPEED_REMOTES 等),再强制 UTF-8 输出
    $psi.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
    $psi.EnvironmentVariables['PYTHONUTF8'] = '1'
    $p = [System.Diagnostics.Process]::Start($psi)
    # 两路异步读,避免管道缓冲区塞满导致 WaitForExit 死锁
    $outTask = $p.StandardOutput.ReadToEndAsync()
    $errTask = $p.StandardError.ReadToEndAsync()
    if (-not $p.WaitForExit($Timeout * 1000)) {
        try { $p.Kill() } catch { }
        Write-Log ("collector timeout after {0}s, killed" -f $Timeout)
        return $E_WHITE
    }
    [void]$outTask.Wait(2000)
    [void]$errTask.Wait(500)
    $out = $outTask.Result
    if ($p.ExitCode -ne 0) {
        $err = ''
        try { $err = $errTask.Result } catch { }
        Write-Log ("collector exit {0}: {1}" -f $p.ExitCode, ($err -replace '\s+', ' ').Trim())
    }
    if (-not $out) { return $E_WHITE }
    return $out
}

# ---------- 标题解析 ----------
function Parse-Title([string]$title) {
    $lamp = 'none'
    if     ($title -like "*$E_GREEN*")  { $lamp = 'green' }
    elseif ($title -like "*$E_YELLOW*") { $lamp = 'yellow' }
    elseif ($title -like "*$E_RED*")    { $lamp = 'red' }
    elseif ($title -like "*$E_WHITE*")  { $lamp = 'idle' }
    $num = ''
    $numRe = '[' + $C_APPROX + $C_GEQ + ']?(\d{1,3})'
    if ($lamp -in 'green', 'yellow', 'red') {
        $lampCh = switch ($lamp) { 'green' { $E_GREEN } 'yellow' { $E_YELLOW } 'red' { $E_RED } }
        $after = $title.Substring($title.IndexOf($lampCh) + $lampCh.Length)
        if ($after -match ('^\s*' + $numRe)) { $num = $Matches[1] }
    } elseif ($title -match ($E_SIGMA + '(\d{1,3})')) {
        $num = $Matches[1]   # 纯后台模式:🤖3 Σ140 → 灰底显示总吞吐
    }
    [pscustomobject]@{
        Lamp  = $lamp
        Num   = $num
        Warn  = ($title -like "*$E_WARN*")
        Robot = ($title -like "*$E_ROBOT*")
    }
}

# ---------- 图标绘制 ----------
function New-TrayIcon($info) {
    $bmp = New-Object System.Drawing.Bitmap 32, 32
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    try {
        $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
        $g.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
        $g.Clear([System.Drawing.Color]::Transparent)
        $color = switch ($info.Lamp) {
            'green'  { [System.Drawing.Color]::FromArgb(46, 160, 67) }
            'yellow' { [System.Drawing.Color]::FromArgb(212, 160, 23) }
            'red'    { [System.Drawing.Color]::FromArgb(207, 34, 46) }
            default  { [System.Drawing.Color]::FromArgb(140, 140, 140) }
        }
        $rect = New-Object System.Drawing.Rectangle 1, 1, 30, 30
        if ($info.Num) {
            $brush = New-Object System.Drawing.SolidBrush $color
            $g.FillEllipse($brush, $rect); $brush.Dispose()
            $size = switch ($info.Num.Length) { 1 { 20 } 2 { 16 } default { 12 } }
            $font = New-Object System.Drawing.Font('Arial', $size, [System.Drawing.FontStyle]::Bold, [System.Drawing.GraphicsUnit]::Pixel)
            $fmt = New-Object System.Drawing.StringFormat
            $fmt.Alignment = 'Center'; $fmt.LineAlignment = 'Center'
            $g.DrawString($info.Num, $font, [System.Drawing.Brushes]::White,
                (New-Object System.Drawing.RectangleF 0, 1, 32, 32), $fmt)
            $font.Dispose(); $fmt.Dispose()
        } else {
            $pen = New-Object System.Drawing.Pen $color, 5
            $g.DrawEllipse($pen, (New-Object System.Drawing.Rectangle 4, 4, 24, 24)); $pen.Dispose()
        }
        if ($info.Warn) {
            $tri = [System.Drawing.Point[]]@(
                (New-Object System.Drawing.Point 20, 12),
                (New-Object System.Drawing.Point 32, 12),
                (New-Object System.Drawing.Point 26, 0))
            $g.FillPolygon([System.Drawing.Brushes]::White, $tri)
            $tri2 = [System.Drawing.Point[]]@(
                (New-Object System.Drawing.Point 22, 11),
                (New-Object System.Drawing.Point 30, 11),
                (New-Object System.Drawing.Point 26, 3))
            $g.FillPolygon([System.Drawing.Brushes]::Red, $tri2)
        }
        if ($info.Robot) {
            $g.FillEllipse([System.Drawing.Brushes]::White, (New-Object System.Drawing.Rectangle 19, 19, 13, 13))
            $blue = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::FromArgb(30, 120, 240))
            $g.FillEllipse($blue, (New-Object System.Drawing.Rectangle 21, 21, 9, 9)); $blue.Dispose()
        }
    } finally { $g.Dispose() }
    $h = $bmp.GetHicon()
    $bmp.Dispose()
    return $h
}

function Limit-Tip([string]$s, [int]$max) {
    if ($s.Length -le $max) { return $s }
    $s = $s.Substring(0, $max - 1)
    if ([char]::IsHighSurrogate($s[$s.Length - 1])) { $s = $s.Substring(0, $s.Length - 1) }
    return $s + [char]0x2026
}

# ---------- UI ----------
[System.Windows.Forms.Application]::EnableVisualStyles()
$script:hIcon = [IntPtr]::Zero
$script:busy = $false
$script:lastOutput = ''
$script:lastTitle = ''

$notify = New-Object System.Windows.Forms.NotifyIcon
$menu = New-Object System.Windows.Forms.ContextMenuStrip
$menu.ShowImageMargin = $false
$notify.ContextMenuStrip = $menu

function Set-Icon($info) {
    $h = New-TrayIcon $info
    $old = $script:hIcon
    $notify.Icon = [System.Drawing.Icon]::FromHandle($h)
    $script:hIcon = $h
    if ($old -ne [IntPtr]::Zero) { [void][ClaudeSpeed.Native]::DestroyIcon($old) }
}

function Update-Menu([string[]]$lines) {
    $menu.SuspendLayout()
    $menu.Items.Clear()
    $rows = @($lines | Select-Object -Skip 1)
    if ($rows.Count -eq 0) { $rows = @($lines[0]) }
    foreach ($r in $rows) {
        $lbl = New-Object System.Windows.Forms.ToolStripLabel ($r -replace '&', '&&')
        $lbl.Padding = New-Object System.Windows.Forms.Padding 4, 2, 4, 2
        [void]$menu.Items.Add($lbl)
    }
    [void]$menu.Items.Add((New-Object System.Windows.Forms.ToolStripSeparator))
    $refresh = New-Object System.Windows.Forms.ToolStripMenuItem '立即刷新'
    $refresh.add_Click({ Invoke-Refresh })
    [void]$menu.Items.Add($refresh)
    $copy = New-Object System.Windows.Forms.ToolStripMenuItem '复制明细'
    $copy.add_Click({
        try { [System.Windows.Forms.Clipboard]::SetText($script:lastOutput) } catch { Write-Log "clipboard: $_" }
    })
    [void]$menu.Items.Add($copy)
    $quit = New-Object System.Windows.Forms.ToolStripMenuItem '退出'
    $quit.add_Click({ Stop-App })
    [void]$menu.Items.Add($quit)
    $menu.ResumeLayout()
}

function Invoke-Refresh {
    if ($script:busy) { return }
    $script:busy = $true
    try {
        $out = Invoke-Collector
        $script:lastOutput = $out
        $lines = @($out -split "`r?`n" | Where-Object { $_ -ne '' })
        if ($lines.Count -eq 0) { $lines = @($E_WHITE) }
        $title = $lines[0]
        $info = Parse-Title $title
        Set-Icon $info
        Update-Menu $lines
        $tip = (@($title) + @($lines | Select-Object -Skip 1 -First 3)) -join "`n"
        $notify.Text = Limit-Tip $tip $TipLimit
        if ($title -ne $script:lastTitle) {
            $script:lastTitle = $title
            Write-Log ("title={0} lamp={1} num={2} warn={3} robot={4} rows={5} icon updated" -f
                $title, $info.Lamp, $info.Num, $info.Warn, $info.Robot, ($lines.Count - 1))
        }
    } catch {
        Write-Log ("refresh error: {0}`n{1}" -f $_, $_.ScriptStackTrace)
        try { Set-Icon (Parse-Title $E_WHITE); $notify.Text = $E_WHITE } catch { }
    } finally {
        $script:busy = $false
    }
}

function Stop-App {
    try { $timer.Stop() } catch { }
    try { $notify.Visible = $false; $notify.Dispose() } catch { }
    if ($script:hIcon -ne [IntPtr]::Zero) { [void][ClaudeSpeed.Native]::DestroyIcon($script:hIcon); $script:hIcon = [IntPtr]::Zero }
    Write-Log 'exit'
    [System.Windows.Forms.Application]::Exit()
}

# 左键单击也弹菜单(ShowContextMenu 是私有方法,反射调用)
$showMenu = [System.Windows.Forms.NotifyIcon].GetMethod('ShowContextMenu',
    [System.Reflection.BindingFlags]'NonPublic,Instance')
$notify.add_MouseUp({
    param($s, $e)
    if ($e.Button -eq [System.Windows.Forms.MouseButtons]::Left) {
        if ($showMenu) { $showMenu.Invoke($notify, $null) }
        else { $menu.Show([System.Windows.Forms.Cursor]::Position) }
    }
})

$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = [int]($Interval * 1000)
$timer.add_Tick({ Invoke-Refresh })

Set-Icon (Parse-Title $E_WHITE)
$notify.Text = 'ClaudeSpeed'
$notify.Visible = $true
Write-Log 'tray icon shown'
Invoke-Refresh
$timer.Start()

$ctx = New-Object System.Windows.Forms.ApplicationContext
try {
    [System.Windows.Forms.Application]::Run($ctx)
} finally {
    try { $timer.Dispose() } catch { }
    try { $notify.Visible = $false; $notify.Dispose() } catch { }
    if ($script:hIcon -ne [IntPtr]::Zero) { [void][ClaudeSpeed.Native]::DestroyIcon($script:hIcon) }
    try { $mutex.ReleaseMutex(); $mutex.Dispose() } catch { }
}
