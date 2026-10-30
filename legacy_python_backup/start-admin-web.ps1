$ErrorActionPreference = "Stop"

$port = 8787
$listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if (-not $listener) {
    $sshArgs = @(
        "-N",
        "-i", "C:\Users\Administrator\.ssh\menxun_nas_deploy_ed25519",
        "-p", "7",
        "-L", "${port}:127.0.0.1:8787",
        "-o", "ExitOnForwardFailure=yes",
        "yimaneili@mouss.synology.me"
    )
    Start-Process -FilePath "ssh.exe" -ArgumentList $sshArgs -WindowStyle Hidden
    Start-Sleep -Seconds 2
}

Start-Process "http://127.0.0.1:$port"
