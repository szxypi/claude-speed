<#
.SYNOPSIS
  claude-speed Windows 卸载器:结束托盘实例、删开机快捷方式、
  移除指向本目录的 statusline 接线与 CLAUDE_SPEED_REMOTES 用户环境变量。
  仓库目录本身保留,需要时手动删除。
.EXAMPLE
  .\uninstall.ps1
  .\uninstall.ps1 -DryRun   # 只打印,不改
#>
[CmdletBinding()]
param([switch]$DryRun)
$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }   # 中文/emoji 提示在 GBK 控制台也别乱码
$Dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$LinkPath = Join-Path ([Environment]::GetFolderPath('Startup')) 'ClaudeSpeed.lnk'
$Settings = Join-Path $HOME '.claude\settings.json'
$Prefix = if ($DryRun) { '[dry-run] ' } else { '' }
function Step([string]$m) { Write-Host "==> $Prefix$m" }

# 1. 结束实例
$procs = Get-CimInstance Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe'" |
    Where-Object { $_.CommandLine -like '*ClaudeSpeed.ps1*' -and $_.ProcessId -ne $PID }
foreach ($p in $procs) {
    Step "结束实例 PID $($p.ProcessId)"
    if (-not $DryRun) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
}
if (-not $procs) { Step '没有运行中的实例' }

# 2. 快捷方式
if (Test-Path $LinkPath) {
    Step "删除 $LinkPath"
    if (-not $DryRun) { Remove-Item $LinkPath -Force }
} else { Step '开机快捷方式不存在,跳过' }

# 3. statusline(仅当 command 指向本目录)
if (Test-Path $Settings) {
    $raw = Get-Content $Settings -Raw -Encoding UTF8
    $obj = $null
    if ($raw.Trim()) { try { $obj = $raw | ConvertFrom-Json } catch { Write-Host "WARN: settings.json 不是合法 JSON,跳过" -ForegroundColor Yellow } }
    $cmd = ''
    if ($obj -and $obj.PSObject.Properties['statusLine'] -and $obj.statusLine.PSObject.Properties['command']) {
        $cmd = [string]$obj.statusLine.command
    }
    if ($cmd -and $cmd.IndexOf($Dir, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
        Step "移除 settings.json 的 statusLine ($cmd)"
        if (-not $DryRun) {
            Copy-Item $Settings "$Settings.bak" -Force
            $obj.PSObject.Properties.Remove('statusLine')
            $json = $obj | ConvertTo-Json -Depth 20
            [System.IO.File]::WriteAllText($Settings, $json, [System.Text.UTF8Encoding]::new($false))
        }
    } else { Step 'statusLine 未指向本目录,不动 settings.json' }
}

# 4. 环境变量(仅当引用本目录)
$rem = [Environment]::GetEnvironmentVariable('CLAUDE_SPEED_REMOTES', 'User')
if ($rem -and $rem.IndexOf($Dir, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
    Step "删除用户环境变量 CLAUDE_SPEED_REMOTES ($rem)"
    if (-not $DryRun) { [Environment]::SetEnvironmentVariable('CLAUDE_SPEED_REMOTES', $null, 'User') }
} elseif ($rem) {
    Step "CLAUDE_SPEED_REMOTES 未引用本目录($rem),保留"
}

Write-Host 'Done.'
