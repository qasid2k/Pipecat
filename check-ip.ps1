# Checks whether this laptop's IP still matches what the Asterisk dialplan
# expects. Run this whenever a call connects but you hear nothing, or after
# reconnecting to Wi-Fi.
#
#   .\check-ip.ps1

$Server   = "10.0.3.164"       # the Asterisk server
$Expected = "192.168.100.67"   # the IP currently written in extensions.conf
$Port     = 8090
$CallUuid = "40325ec2-5efd-4bd3-805f-53576e581d13"

Write-Host ""
Write-Host "Checking how this laptop reaches Asterisk ($Server)..." -ForegroundColor Cyan

# Which of our addresses would the OS actually use to reach the server?
# This is the one Asterisk must dial back -- not just "any" local IP.
try {
    $current = (Find-NetRoute -RemoteIPAddress $Server -ErrorAction Stop |
                Where-Object { $_.IPAddress } |
                Select-Object -First 1).IPAddress
} catch {
    Write-Host "  No route to $Server. Are you on the office network / VPN?" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "  Dialplan expects : $Expected"
Write-Host "  This laptop is   : $current"
Write-Host ""

if ($current -eq $Expected) {
    Write-Host "  MATCH - the dialplan is still correct." -ForegroundColor Green
} else {
    Write-Host "  CHANGED - your IP moved, the dialplan is now WRONG." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Fix: on the server, edit /etc/asterisk/extensions.conf to:" -ForegroundColor Yellow
    Write-Host "      same => n,AudioSocket($CallUuid,${current}:$Port)"
    Write-Host "  then reload:"
    Write-Host "      asterisk -rx 'dialplan reload'"
    Write-Host ""
    Write-Host "  (Also update `$Expected at the top of this script to $current)"
}

# Is the server reachable at all?
$reachable = Test-Connection -ComputerName $Server -Count 1 -Quiet -ErrorAction SilentlyContinue
Write-Host ""
Write-Host ("  Server reachable : " + $(if ($reachable) { "YES" } else { "NO  <-- network problem" }))

# Is our agent actually listening?
$listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    $procName = (Get-Process -Id $listening[0].OwningProcess -ErrorAction SilentlyContinue).ProcessName
    Write-Host "  Port $Port       : LISTENING (pid $($listening[0].OwningProcess), $procName)"
} else {
    Write-Host "  Port $Port       : nothing listening - start bot.py" -ForegroundColor Yellow
}
Write-Host ""
