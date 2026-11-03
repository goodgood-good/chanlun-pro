# Keep the user's existing scheduler identity and process lifetime semantics.
function Get-NoConsoleLauncher {
    [CmdletBinding()]
    param([Parameter(Mandatory=$true)][string]$ProjectRoot)
    $source = Join-Path $PSScriptRoot 'no_console_launcher.cs'
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $digest = $sha.ComputeHash([IO.File]::ReadAllBytes($source)) }
    finally { $sha.Dispose() }
    $version = ([BitConverter]::ToString($digest)).Replace('-', '').Substring(0, 20).ToLowerInvariant()
    # Scheduled tasks must survive ordinary cache cleanup.
    $directory = Join-Path $ProjectRoot 'ops\.runtime\no_console_launcher'
    [IO.Directory]::CreateDirectory($directory) | Out-Null
    $target = Join-Path $directory ('no-console-{0}.exe' -f $version)
    if (-not (Test-Path -LiteralPath $target -PathType Leaf)) {
        $compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
        if (-not (Test-Path -LiteralPath $compiler)) {
            $compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe'
        }
        if (-not (Test-Path -LiteralPath $compiler)) { throw 'Windows .NET Framework C# compiler is unavailable' }
        $temporary = Join-Path $directory ('build-{0}.exe' -f [guid]::NewGuid().ToString('N'))
        $info = New-Object Diagnostics.ProcessStartInfo
        $info.FileName = $compiler
        $info.Arguments = '/nologo /target:winexe /optimize+ /out:"{0}" "{1}"' -f $temporary, $source
        $info.UseShellExecute = $false
        $info.CreateNoWindow = $true
        $info.RedirectStandardOutput = $true
        $info.RedirectStandardError = $true
        $build = [Diagnostics.Process]::Start($info)
        try {
            $output = $build.StandardOutput.ReadToEnd()
            $errors = $build.StandardError.ReadToEnd()
            $build.WaitForExit()
            if ($build.ExitCode -ne 0) { throw ('No-console launcher compilation failed: {0} {1}' -f $output, $errors) }
            try { [IO.File]::Move($temporary, $target) }
            catch { if (-not (Test-Path -LiteralPath $target)) { throw } }
        } finally {
            $build.Dispose()
            if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
        }
    }
    return [IO.Path]::GetFullPath($target)
}

function Resolve-NoConsoleExecutable {
    param([Parameter(Mandatory=$true)][string]$FilePath)
    if ([IO.Path]::IsPathRooted($FilePath)) { return $FilePath }
    return (Get-Command -Name $FilePath -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
}

function New-NoConsoleTaskAction {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory=$true)][string]$ProjectRoot,
        [Parameter(Mandatory=$true)][string]$FilePath,
        [Parameter(Mandatory=$true)][string]$Argument,
        [Parameter(Mandatory=$true)][string]$Label,
        [switch]$OwnProcessTree,
        [string]$WorkingDirectory = $ProjectRoot
    )
    $launcher = Get-NoConsoleLauncher -ProjectRoot $ProjectRoot
    $executable = Resolve-NoConsoleExecutable -FilePath $FilePath
    $safeLabel = $Label -replace '[^a-zA-Z0-9_.-]', '_'
    $prefix = Join-Path $ProjectRoot ('ops\logs\no_console\{0}' -f $safeLabel)
    [IO.Directory]::CreateDirectory((Split-Path -Parent $prefix)) | Out-Null
    $mode = if ($OwnProcessTree) { '--tree ' } else { '' }
    New-ScheduledTaskAction -Execute $launcher `
        -Argument ('{0}"{1}" "{2}" {3}' -f $mode, $prefix, $executable, $Argument) `
        -WorkingDirectory $WorkingDirectory
}

function Start-NoConsoleProcess {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory=$true)][string]$ProjectRoot,
        [Parameter(Mandatory=$true)][string]$FilePath,
        [Parameter(Mandatory=$true)][string[]]$ArgumentList,
        [Parameter(Mandatory=$true)][string]$RedirectStandardOutput,
        [Parameter(Mandatory=$true)][string]$RedirectStandardError,
        [string]$WorkingDirectory = $ProjectRoot
    )
    if (-not ('Chanlun.Operations.NoConsoleLauncher' -as [type])) {
        [void][Reflection.Assembly]::LoadFrom((Get-NoConsoleLauncher -ProjectRoot $ProjectRoot))
    }
    $executable = Resolve-NoConsoleExecutable -FilePath $FilePath
    return [Chanlun.Operations.NoConsoleLauncher]::Start($executable,
        ($ArgumentList -join ' '), $WorkingDirectory, $RedirectStandardOutput, $RedirectStandardError)
}
