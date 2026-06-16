<#
.SYNOPSIS
  Launch nff-mock (one standalone virtual ESP32) + nff-mock-controller in dev,
  pointing at a HOST-NATIVE nff-fleet (started with `nff-fleet start`).

.DESCRIPTION
  Idempotent - safe to re-run. It:
    1. builds nff-mock:dev and nff-mock-controller:dev (skip with -SkipBuild),
    2. provisions the standalone mock's cert if it doesn't exist yet,
    3. (re)runs both containers off ./.env.dev,
    4. probes whether the running fleet exposes /internal/provision (the controller
       needs it; a stale fleet must be restarted).

  Containers reach the host fleet via host.docker.internal. Certs are read from
  -DataDir (bind-mounted read-only at /data). Config lives in ./.env.dev.

.EXAMPLE
  .\dev.ps1                       # build + run both, default mock id mock-01
  .\dev.ps1 -DeviceId demo-7      # use a different standalone identity
  .\dev.ps1 -SkipBuild            # skip image builds (faster re-run)
  .\dev.ps1 -Down                 # stop + remove both containers, then exit
#>
[CmdletBinding()]
param(
    [string]$DeviceId = "mock-01",
    [string]$DataDir = "C:\data",
    # Fleet binds every device to a project; default = the "Welcome to nff" demo project.
    [string]$ProjectId = "1aeef260-9384-4831-8cf4-97511690ed44",
    [int]$FleetMcpPort = 3001,
    [int]$ControllerPort = 3004,
    [switch]$SkipBuild,
    [switch]$Down
)

$ErrorActionPreference = "Stop"
$repoDir   = $PSScriptRoot                      # ...\nff-mock
$rootDir   = Split-Path $repoDir -Parent        # ...\nff_cli_mcp
$envFile   = Join-Path $repoDir ".env.dev"
$deviceCtr = "nff-mock-$DeviceId"
$ctrlCtr   = "nff-mock-controller"

function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

# --- -Down: tear everything down and exit -------------------------------------
if ($Down) {
    Step "Removing containers"
    docker rm -f $deviceCtr $ctrlCtr 2>$null | Out-Null
    Write-Host "Removed $deviceCtr and $ctrlCtr." -ForegroundColor Green
    return
}

if (-not (Test-Path $envFile)) { throw "missing env file: $envFile" }

# --- 1. Build images ----------------------------------------------------------
if (-not $SkipBuild) {
    Step "Building nff-mock:dev"
    docker build -q -t nff-mock:dev -f (Join-Path $repoDir "device\Dockerfile.dev") (Join-Path $repoDir "device") | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "nff-mock:dev build failed" }

    Step "Building nff-mock-controller:dev"
    docker build -q -t nff-mock-controller:dev -f (Join-Path $repoDir "controller\Dockerfile.dev") (Join-Path $repoDir "controller") | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "nff-mock-controller:dev build failed" }
}

# --- 2. Provision a project-bound cert for the standalone mock ----------------
# Fleet now binds device -> project (cert OID + per-project signing key), so we
# (re)issue via fleet's HTTP endpoint every run. The `nff-fleet provision new-device`
# CLI is project-unaware and would mint a cert the new ACL rejects.
Step "Provisioning '$DeviceId' into project $ProjectId (via fleet :$FleetMcpPort)"
$secLine = Get-Content $envFile | Where-Object { $_ -match '^\s*NFF_MOCKCTL_FLEET_SECRET\s*=' } | Select-Object -First 1
$fleetSecret = if ($secLine) { ($secLine -split '=', 2)[1].Trim() } else { '' }
if (-not $fleetSecret) { throw "NFF_MOCKCTL_FLEET_SECRET not found in $envFile" }
# Body via a temp file so PowerShell never mangles the JSON double quotes for curl.exe.
$provBody = '{"device_id":"' + $DeviceId + '","project_id":"' + $ProjectId + '"}'
$provTmp = New-TemporaryFile
Set-Content -Path $provTmp -Value $provBody -NoNewline -Encoding ascii
$provOut = (& curl.exe -s -w "`n%{http_code}" -X POST "http://localhost:$FleetMcpPort/internal/provision" `
    -H "X-Fleet-Secret: $fleetSecret" -H "Content-Type: application/json" --data "@$provTmp")
Remove-Item $provTmp -ErrorAction SilentlyContinue
$provLines = $provOut -split "`n"
if ($provLines[-1].Trim() -ne "200") {
    throw "fleet provision failed (HTTP $($provLines[-1])): $($provLines[0]). Is 'nff-fleet start' running with the new code on :$FleetMcpPort (and the secret matching)?"
}

# --- 3. (Re)run the standalone device -----------------------------------------
Step "Starting device container '$deviceCtr'"
docker rm -f $deviceCtr 2>$null | Out-Null
docker run -d --name $deviceCtr `
    --env-file $envFile `
    -e "NFF_MOCK_DEVICE_ID=$DeviceId" `
    -e "NFF_MOCK_PROJECT_ID=$ProjectId" `
    -e "NFF_MOCK_CMD_VERIFY_KEY=/data/keys/projects/$ProjectId/cmd_sign.pem" `
    -v "${DataDir}:/data:ro" `
    --restart unless-stopped `
    nff-mock:dev | Out-Null
if ($LASTEXITCODE -ne 0) { throw "device container failed to start" }

# --- 4. (Re)run the controller ------------------------------------------------
Step "Starting controller container '$ctrlCtr' on port $ControllerPort"
docker rm -f $ctrlCtr 2>$null | Out-Null
$dataVol = $DataDir.Replace('\', '/')   # docker -v form for controller-spawned mocks
docker run -d --name $ctrlCtr `
    --env-file $envFile `
    -e "NFF_MOCKCTL_FLEET_URL=http://host.docker.internal:$FleetMcpPort" `
    -e "NFF_MOCKCTL_FLEET_DATA_VOLUME=$dataVol" `
    -p "${ControllerPort}:3004" `
    -v /var/run/docker.sock:/var/run/docker.sock `
    --restart unless-stopped `
    nff-mock-controller:dev | Out-Null
if ($LASTEXITCODE -ne 0) { throw "controller container failed to start" }

Start-Sleep -Seconds 4

# --- 5. Status + fleet readiness probe ----------------------------------------
Step "Status"
docker ps --filter "name=$deviceCtr" --filter "name=$ctrlCtr" --format "{{.Names}}`t{{.Status}}`t{{.Ports}}"

$provUrl = "http://localhost:$FleetMcpPort/internal/provision"
$provCode = (& curl.exe -s -o NUL -w "%{http_code}" -X POST $provUrl --max-time 4)
Write-Host ""
if ($provCode -eq "404") {
    Write-Warning "Fleet on port $FleetMcpPort does not expose /internal/provision (got 404) - it is a STALE process."
    Write-Warning "Controller spawns will 502 until you restart 'nff-fleet start' (current code serves it)."
} elseif ($provCode -eq "401") {
    Write-Host "Fleet /internal/provision is live (401 = exists, secret-gated). Controller spawns are ready." -ForegroundColor Green
} else {
    Write-Warning "Fleet probe on port $FleetMcpPort returned $provCode (is 'nff-fleet start' running?)."
}

$ctrlLine = Get-Content $envFile | Where-Object { $_ -match '^\s*NFF_MOCKCTL_CONTROL_SECRET\s*=' } | Select-Object -First 1
$ctrlSecret = if ($ctrlLine) { ($ctrlLine -split '=', 2)[1].Trim() } else { '<see .env.dev>' }
$spawnHdr = '"X-Control-Secret: ' + $ctrlSecret + '"'
Write-Host ""
Write-Host "Standalone device : $deviceCtr  (id '$DeviceId')  ->  docker logs $deviceCtr"
Write-Host "Controller API    : http://localhost:$ControllerPort   (X-Control-Secret from .env.dev)"
Write-Host "Spawn a mock      : curl.exe -s -H $spawnHdr -X POST http://localhost:$ControllerPort/mocks"
Write-Host "Tear down         : .\dev.ps1 -Down"
