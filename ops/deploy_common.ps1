function ConvertTo-ProcessStartDate {
    [CmdletBinding()]
    param([Parameter(Mandatory = $true)][object]$Value)

    if ($Value -is [datetime]) {
        return [datetime]$Value
    }
    if ($Value -is [datetimeoffset]) {
        return ([datetimeoffset]$Value).LocalDateTime
    }

    $text = [string]$Value
    try {
        return [Management.ManagementDateTimeConverter]::ToDateTime($text)
    } catch {
        $parsed = [datetime]::MinValue
        if ([datetime]::TryParse($text, [ref]$parsed)) {
            return $parsed
        }
        throw "Unsupported process creation date: $text"
    }
}

function Get-ApplicationFileSha256 {
    param([Parameter(Mandatory = $true)][string]$Path)

    # 使用框架原语而不是 Get-FileHash；后者属于 Microsoft.PowerShell.Utility，
    # 部分精简 Windows CI 环境即使存在 powershell.exe 也未必提供该命令。
    $stream = [IO.File]::Open(
        $Path,
        [IO.FileMode]::Open,
        [IO.FileAccess]::Read,
        [IO.FileShare]::ReadWrite
    )
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        return -join ($sha.ComputeHash($stream) | ForEach-Object { $_.ToString('x2') })
    } finally {
        $sha.Dispose()
        $stream.Dispose()
    }
}

function Get-ApplicationSourceRevision {
    param([Parameter(Mandatory = $true)][string]$Root)

    $headOutput = @(& git -C $Root rev-parse HEAD 2>$null)
    if ($LASTEXITCODE -ne 0 -or $headOutput.Count -eq 0) {
        throw 'unable to resolve deployment git revision'
    }
    $head = ([string]$headOutput[-1]).Trim()
    if ([string]::IsNullOrWhiteSpace($head)) {
        throw 'deployment git revision is empty'
    }

    # 此列表必须与 tools/run_forward_paper.py::FORWARD_PIPELINE_TOOL_PATHS
    # 逐字节一致。这些子进程会从磁盘执行决策或时点代码，因此属于部署应用身份；
    # 无关维护工具不属于该身份。
    $forwardPipelineTools = @(
        'tools/audit_qmt_warmup_convergence.py',
        'tools/run_forward_paper.py',
        'tools/snapshot_qmt_gics3_sector_ledger.py',
        'tools/snapshot_qmt_pit_metadata.py'
    )
    $sourcePaths = @('src', 'web/chanlun_chart', 'ops', 'windows_run.bat') + $forwardPipelineTools
    $paths = @(& git -C $Root -c core.quotePath=false ls-files --cached --others --exclude-standard -- @sourcePaths 2>$null)
    if ($LASTEXITCODE -ne 0) {
        throw 'unable to enumerate application source files'
    }
    $runtimeConfig = 'src/chanlun/config.py'
    if ((Test-Path -LiteralPath (Join-Path $Root $runtimeConfig) -PathType Leaf) -and $paths -notcontains $runtimeConfig) {
        $paths += $runtimeConfig
    }
    $paths = [string[]]@($paths | Sort-Object -Unique)
    # Sort-Object 受区域规则影响，而 Python 前向程序使用序数排序。显式比较器确保
    # 部署、验证与前向证据在不同运行时得到同一个源码身份。
    [Array]::Sort($paths, [StringComparer]::Ordinal)
    $existing = @($paths | Where-Object { Test-Path -LiteralPath (Join-Path $Root $_) -PathType Leaf })
    # 不把路径名通过管道送入原生程序。Windows PowerShell 5.1 可能在标准输入前加
    # BOM，Git 会把它当作首个路径的一部分，并仅在部分代码页下失败。直接计算
    # SHA-256 内容哈希可跨 PowerShell/Python 保持稳定，且仍绑定应用实际使用的字节。
    $hashByPath = @{}
    foreach ($path in $existing) {
        try {
            $hashByPath[$path] = Get-ApplicationFileSha256 -Path (Join-Path $Root $path)
        } catch {
            throw "unable to hash application source file '$path': $($_.Exception.Message)"
        }
    }
    $manifest = New-Object System.Collections.Generic.List[string]
    $manifest.Add("HEAD`t$head")
    foreach ($path in $paths) {
        $hash = if ($hashByPath.ContainsKey($path)) { $hashByPath[$path] } else { 'deleted' }
        # 这里必须使用双引号；PowerShell 单引号字符串会保留字面量 `t，不能生成制表符。
        $manifest.Add(("{0}`t{1}" -f $path, $hash))
    }
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [Text.Encoding]::UTF8.GetBytes(($manifest -join "`n"))
        $digest = -join ($sha.ComputeHash($bytes) | ForEach-Object { $_.ToString('x2') })
    } finally {
        $sha.Dispose()
    }
    return ('{0}.tree.{1}' -f $head, $digest.Substring(0, 24))
}
