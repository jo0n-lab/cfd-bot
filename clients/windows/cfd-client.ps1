# List aliases only; let OpenSSH apply User, Port, keys and ProxyJump.
# Local diagnostic events exclude credentials, SSH options and configuration contents.
function Write-Diagnostic([string]$eventName, [hashtable]$details = @{}) {
    if (-not $env:CFD_BOT_DIAGNOSTICS -or $env:CFD_BOT_DIAGNOSTICS -match '^(0|off|false)$') { return }
    try {
        $directory = if ($env:CFD_BOT_DIAGNOSTICS_DIR) { $env:CFD_BOT_DIAGNOSTICS_DIR }
                     else { Join-Path $env:LOCALAPPDATA 'CFD-Control-Room\logs' }
        [IO.Directory]::CreateDirectory($directory) | Out-Null
        $record = @{event=$eventName; at=[DateTime]::UtcNow.ToString('o'); pid=$PID; details=$details}
        [IO.File]::AppendAllText((Join-Path $directory "launcher-$PID.jsonl"), ($record | ConvertTo-Json -Compress) + "`n")
    } catch { } # A log-output failure cannot alter SSH or UI behavior.
}
Write-Diagnostic 'launcher.start'

$sshDir = Join-Path $env:USERPROFILE '.ssh'
$seenFiles = New-Object 'System.Collections.Generic.HashSet[string]'
function Get-SshAliases([string]$file, [int]$depth = 0) {
    Write-Diagnostic 'function.call' @{function='Get-SshAliases'; depth=$depth}
    try {
    if ($depth -ge 16 -or -not (Test-Path -LiteralPath $file -PathType Leaf)) { return }
    $file = (Resolve-Path -LiteralPath $file).Path
    if (-not $seenFiles.Add($file)) { return }
    foreach ($line in Get-Content -LiteralPath $file -Encoding UTF8) {
        if ($line -notmatch '^\s*(Host|Include)(?:\s+|\s*=\s*)(.*)$') { continue }
        $kind, $rest = $Matches[1], $Matches[2]
        foreach ($part in [regex]::Matches($rest, '"([^"]*)"|([^\s#]+)|(#.*)')) {
            if ($part.Groups[3].Success) { break }
            $value = if ($part.Groups[1].Success) { $part.Groups[1].Value } else { $part.Groups[2].Value }
            if ($kind -ieq 'Host') {
                if ($value -match '^[A-Za-z0-9_][A-Za-z0-9_.:-]*$') { $value }
            } else {
                $pattern = if ($value.StartsWith('~/')) { Join-Path $env:USERPROFILE $value.Substring(2) }
                           elseif ([IO.Path]::IsPathRooted($value)) { $value }
                           else { Join-Path $sshDir $value }
                foreach ($included in Resolve-Path -Path $pattern -ErrorAction SilentlyContinue) {
                    Get-SshAliases $included.Path ($depth + 1)
                }
            }
        }
    }
    } finally { Write-Diagnostic 'function.return' @{function='Get-SshAliases'; depth=$depth} }
}
$Port = 8766
$ErrorActionPreference = 'Stop'
$connection = $null
try {
    $hosts = @(Get-SshAliases (Join-Path $sshDir 'config') | Sort-Object -Unique)
    if (-not $hosts.Count) { throw 'No named Host entries found in ~/.ssh/config.' }
    Write-Host 'SSH hosts from ~/.ssh/config:'
    for ($i = 0; $i -lt $hosts.Count; $i++) { Write-Host ("{0}) {1}" -f ($i + 1), $hosts[$i]) }
    $selection = 0
    if (-not [int]::TryParse((Read-Host 'Select host number'), [ref]$selection) -or
        $selection -lt 1 -or $selection -gt $hosts.Count) { throw 'Invalid selection.' }
    Write-Diagnostic 'ui.host.selected' @{selection=$selection}
    $Server = $hosts[$selection - 1]
    $connection = Start-Process ssh.exe -NoNewWindow -PassThru -ArgumentList @(
        '-N', '-T', '-o', 'ExitOnForwardFailure=yes', '-o', 'ServerAliveInterval=30',
        '-L', "127.0.0.1:${Port}:127.0.0.1:${Port}", $Server)
    Write-Diagnostic 'ssh.started' @{child_pid=$connection.Id; port=$Port}
    do {
        Start-Sleep -Milliseconds 500
        if ($connection.HasExited) { throw 'SSH connection failed. Check the SSH window.' }
        $socket = New-Object System.Net.Sockets.TcpClient
        try { $socket.Connect('127.0.0.1', $Port); $ready = $true }
        catch { Write-Diagnostic 'tunnel.wait' @{error=$_.Exception.GetType().Name}; $ready = $false }
        finally { $socket.Dispose() }
    } until ($ready)
    Write-Diagnostic 'tunnel.ready' @{port=$Port}
    Write-Diagnostic 'browser.open' @{port=$Port}
    Start-Process "http://127.0.0.1:$Port"
    [void](Read-Host 'Connected. Keep this window open. Enter to disconnect')
} catch {
    Write-Diagnostic 'launcher.exception' @{error=$_.Exception.GetType().Name; line=$_.InvocationInfo.ScriptLineNumber}
    Write-Host $_.Exception.Message
    exit 1
} finally {
    Write-Diagnostic 'ssh.cleanup'
    if ($connection -and -not $connection.HasExited) { Stop-Process -Id $connection.Id }
}
