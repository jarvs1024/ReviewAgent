<#
.SYNOPSIS
    批量同步 reviewagent/ 下所有 .py 文件到服务器 (排除 __pycache__).
.DESCRIPTION
    用于部署或修复文件损坏. 同步完成后需手动:
      1) 服务器 python3 -m compileall -q reviewagent 验证语法
      2) systemctl restart reviewagent-webhook reviewagent-worker@1..3
.PARAMETER Session
    SSH session alias (默认 ai-test-86).
.PARAMETER DryRun
    只列出要上传的文件, 不实际上传.
.EXAMPLE
    .\scripts\sync_reviewagent.ps1
    .\scripts\sync_reviewagent.ps1 -DryRun
    .\scripts\sync_reviewagent.ps1 -Session ai-test-86
#>
param(
    [string]$Session = 'ai-test-86',
    [switch]$DryRun
)

$localRoot = Join-Path $PSScriptRoot '..\reviewagent'
$remoteRoot = '/home/workflow/ReviewAgent/reviewagent'
$script = 'C:\Users\2268\.agents\skills\ssh-remote\scripts\ssh_ops.py'

if (-not (Test-Path $localRoot)) {
    Write-Error "local root not found: $localRoot"
    exit 1
}

$files = Get-ChildItem -Path $localRoot -Recurse -Filter '*.py' |
    Where-Object { $_.FullName -notmatch '\\__pycache__\\' }

$count = 0
$fail = 0
foreach ($f in $files) {
    $rel = $f.FullName.Substring($localRoot.Length + 1)
    $remote = "$remoteRoot/$($rel -replace '\\','/')"
    $count++
    if ($DryRun) {
        Write-Output "[dry] [$count] $rel"
        continue
    }
    Write-Output "[$count] $rel"
    & python $script upload --session $Session --yes --trust-host --i-know --trust-override --local $f.FullName --remote $remote 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Output "    FAIL: $rel"
        $fail++
    }
}
if ($DryRun) {
    Write-Output "=== would upload $count files ==="
} else {
    Write-Output "=== uploaded $count files, $fail failures ==="
    if ($fail -eq 0) {
        Write-Output "next: ssh exec 'python3 -m compileall -q reviewagent && systemctl restart reviewagent-webhook reviewagent-worker@1 reviewagent-worker@2 reviewagent-worker@3'"
    }
}
