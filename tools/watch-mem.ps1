# watch-mem.ps1 - 记录游戏进程的内存占用时间序列（替代打不开的任务管理器）
#
# 用法（双击 watch-mem.bat，或）：
#   powershell -ExecutionPolicy Bypass -File watch-mem.ps1
#   powershell -ExecutionPolicy Bypass -File watch-mem.ps1 -IntervalSec 3 -MaxMinutes 240
#
# 行为：等 jjxf47/jjxf46 进程出现 -> 每 N 秒记一行 -> 游戏全部退出后自动停并打印摘要。
# 输出：..\_build_tools\memlog\mem-<时间>.csv（相对本脚本）

param(
    [int]$IntervalSec = 5,
    [int]$MaxMinutes = 240,
    [string]$OutCsv = "",
    # 默认抓 4.7/4.6 两个底座进程；进程名变了就改这里，不用改代码
    [string[]]$ProcessName = @("jjxf47", "jjxf46")
)

$ErrorActionPreference = "SilentlyContinue"

if (-not $OutCsv) {
    $dir = Join-Path $PSScriptRoot "..\_build_tools\memlog"
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    $OutCsv = Join-Path $dir ("mem-" + (Get-Date -Format "yyyyMMdd-HHmmss") + ".csv")
}
$OutCsv = [IO.Path]::GetFullPath($OutCsv)

$cores = (Get-CimInstance Win32_ComputerSystem).NumberOfLogicalProcessors
if (-not $cores) { $cores = 1 }

$header = "时间,进程数,工作集MB,私有MB,提交MB,CPU%,系统可用GB,提交可用GB"
$header | Out-File -FilePath $OutCsv -Encoding utf8

Write-Host ""
Write-Host "  内存采样已启动" -ForegroundColor Cyan
Write-Host "  间隔 ${IntervalSec}s / 最长 ${MaxMinutes} 分钟"
Write-Host "  文件: $OutCsv"
Write-Host "  等待游戏进程 ($($ProcessName -join "/")) 出现 ... 出现后开始记录，游戏全部退出即结束。"
Write-Host ""

$deadline = (Get-Date).AddMinutes($MaxMinutes)
$seen = $false
$prevCpuTotal = $null
$prevStamp = $null
$peakWs = 0
$samples = @()

while ((Get-Date) -lt $deadline) {
    $now = Get-Date
    $ps = @(Get-Process -Name $ProcessName -ErrorAction SilentlyContinue)

    if ($ps.Count -eq 0) {
        if ($seen) {
            $os = Get-CimInstance Win32_OperatingSystem
            $avail = [math]::Round($os.FreePhysicalMemory / 1MB, 2)
            $cfree = [math]::Round($os.FreeVirtualMemory / 1MB, 2)
            $row = "{0},{1},{2},{3},{4},{5},{6},{7}" -f $now.ToString("HH:mm:ss"), 0, 0, 0, 0, 0, $avail, $cfree
            $row | Out-File -FilePath $OutCsv -Append -Encoding utf8
            Write-Host "  游戏已退出，采样结束。" -ForegroundColor Green
            break
        }
        Start-Sleep -Seconds $IntervalSec
        continue
    }

    if (-not $seen) {
        $seen = $true
        Write-Host "  检测到游戏进程，开始记录 ..." -ForegroundColor Green
    }

    $ws = 0
    $priv = 0
    $commit = 0
    $cpuNow = 0
    foreach ($p in $ps) {
        $ws += $p.WorkingSet64
        $priv += $p.PrivateMemorySize64
        $commit += $p.PagedMemorySize64
        $cpuNow += $p.TotalProcessorTime.TotalSeconds
    }
    $wsMB = [math]::Round($ws / 1MB, 1)
    $privMB = [math]::Round($priv / 1MB, 1)
    $commitMB = [math]::Round($commit / 1MB, 1)

    $cpuPct = 0.0
    if ($prevStamp -and $null -ne $prevCpuTotal) {
        $wall = ($now - $prevStamp).TotalSeconds
        if ($wall -gt 0) {
            $cpuPct = [math]::Round(($cpuNow - $prevCpuTotal) / $wall / $cores * 100, 1)
            if ($cpuPct -lt 0) { $cpuPct = 0 }
            if ($cpuPct -gt 100) { $cpuPct = 100 }
        }
    }
    $prevCpuTotal = $cpuNow
    $prevStamp = $now

    $os = Get-CimInstance Win32_OperatingSystem
    $avail = [math]::Round($os.FreePhysicalMemory / 1MB, 2)
    $cfree = [math]::Round($os.FreeVirtualMemory / 1MB, 2)

    $row = "{0},{1},{2},{3},{4},{5},{6},{7}" -f $now.ToString("HH:mm:ss"), $ps.Count, $wsMB, $privMB, $commitMB, $cpuPct, $avail, $cfree
    $row | Out-File -FilePath $OutCsv -Append -Encoding utf8

    if ($wsMB -gt $peakWs) { $peakWs = $wsMB }
    $samples += [pscustomobject]@{ ws = $wsMB; commit = $commitMB; avail = $avail; cpu = $cpuPct }

    Write-Host ("  {0}  工作集 {1,7:N1} MB   提交 {2,7:N1} MB   可用 {3,5:N2} GB" -f $now.ToString("HH:mm:ss"), $wsMB, $commitMB, $avail)

    Start-Sleep -Seconds $IntervalSec
}

if (-not $seen) {
    Write-Host "  超时：这段时间内游戏进程从未出现。" -ForegroundColor Yellow
    exit 0
}

$n = $samples.Count
$sortedWs = @($samples | Sort-Object ws)
$idx = [int][math]::Floor($n * 0.9)
if ($idx -ge $n) { $idx = $n - 1 }
$p90 = $sortedWs[$idx].ws
$tailCount = [math]::Max(1, [int][math]::Floor($n * 0.2))
$tail = @($samples | Select-Object -Last $tailCount)
$tailAvg = [math]::Round((($tail | Measure-Object ws -Average).Average), 1)
$peakCommit = ($samples | Measure-Object commit -Maximum).Maximum
$minAvail = ($samples | Measure-Object avail -Minimum).Minimum
$avgCpu = [math]::Round((($samples | Measure-Object cpu -Average).Average), 1)

$summary = @()
$summary += "采样点数: $n"
$summary += "峰值工作集: $peakWs MB"
$summary += "P90 工作集: $p90 MB"
$summary += "末段稳态工作集: $tailAvg MB"
$summary += "峰值提交: $peakCommit MB"
$summary += "最低系统可用: $minAvail GB"
$summary += "平均 CPU: $avgCpu %"

Write-Host ""
Write-Host "===== 摘要 =====" -ForegroundColor Cyan
$summary | ForEach-Object { Write-Host "  $_" }
Write-Host ""
Write-Host "  明细: $OutCsv"

$summaryFile = [IO.Path]::ChangeExtension($OutCsv, ".txt")
($summary + "" + "明细: $OutCsv") | Out-File -FilePath $summaryFile -Encoding utf8
