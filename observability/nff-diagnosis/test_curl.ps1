# Manual HTTP tests for the nff-diagnosis server.
# Start the server first:
#   $env:ANTHROPIC_API_KEY = "sk-ant-..."; nff-diag server start
#
# Run all:        .\test_curl.ps1
# Run one test:   .\test_curl.ps1 health

param([string]$Test = "all")

$BASE = "http://127.0.0.1:8080"

function Run-Test($name, [scriptblock]$block) {
    Write-Host "===== $name =====" -ForegroundColor Cyan
    try { & $block } catch { Write-Host "ERROR: $_" -ForegroundColor Red }
    Write-Host ""
}

function Post-Json($url, $json) {
    try {
        Invoke-RestMethod -Method Post -Uri $url -ContentType "application/json" -Body $json |
            ConvertTo-Json -Depth 10
    } catch {
        $code = [int]$_.Exception.Response.StatusCode
        $body = $_.ErrorDetails.Message
        if (-not $body) { $body = $_.Exception.Message }
        Write-Host "HTTP $code" -ForegroundColor Red
        Write-Host $body
    }
}

function Get-StatusCode($method, $url, $json) {
    try {
        $params = @{ Method = $method; Uri = $url }
        if ($json) { $params.ContentType = "application/json"; $params.Body = $json }
        Invoke-RestMethod @params | Out-Null
        return 200
    } catch {
        return [int]$_.Exception.Response.StatusCode
    }
}

# --------------------------------------------------------------------------

function Test-Health {
    Run-Test "GET /docs (server alive)" {
        $code = Get-StatusCode "GET" "$BASE/docs"
        Write-Host "HTTP $code"
    }
}

function Test-IngestNullPtr {
    Run-Test "POST /ingest - null-pointer crash (guru meditation)" {
        $body = @'
{
  "build_id": "4403478db375356900ef6b11befc74463a8ed2e18d9d411fdc490af0f83213b0",
  "panic_type": "guru_meditation",
  "exception_cause": 29,
  "fault_pc": "0x400d1606",
  "fault_addr": "0x00000000",
  "registers": {
    "PC": "0x400d1606",
    "PS": "0x00060530",
    "A0": "0x800d3698",
    "A1": "0x3ffb2240",
    "EXCCAUSE": "0x0000001d",
    "EXCVADDR": "0x00000000"
  },
  "backtrace": ["0x400d1603", "0x400d3695", "0x400880dd"],
  "cpu": 1,
  "raw": "Guru Meditation Error: Core 1 paniced (StoreProhibited).\nPC: 0x400d1606\nEXCCAUSE: 0x0000001d EXCVADDR: 0x00000000\nBacktrace: 0x400d1603:0x3ffb2240 0x400d3695:0x3ffb2270"
}
'@
        Post-Json "$BASE/ingest" $body
    }
}

function Test-IngestWatchdog {
    Run-Test "POST /ingest - watchdog timeout" {
        $body = @'
{
  "build_id": "deadbeef0000000000000000000000000000000000000000000000000000cafe",
  "panic_type": "idf_watchdog",
  "registers": {},
  "backtrace": [],
  "raw": "Task watchdog got triggered.\n - IDLE0 (CPU 0)\nAbort() was called at PC 0x40085d6c on core 0\nBacktrace: 0x40085d6c:0x3ffb0f30\nRebooting..."
}
'@
        Post-Json "$BASE/ingest" $body
    }
}

function Test-IngestStackOverflow {
    Run-Test "POST /ingest - stack overflow (repeated PC)" {
        $body = @'
{
  "build_id": "aaaa000000000000000000000000000000000000000000000000000000001234",
  "panic_type": "guru_meditation",
  "exception_cause": 28,
  "fault_pc": "0x400d2a10",
  "fault_addr": "0x3ffb0000",
  "registers": {
    "PC": "0x400d2a10",
    "EXCCAUSE": "0x0000001c",
    "EXCVADDR": "0x3ffb0000"
  },
  "backtrace": ["0x400d2a10","0x400d2a10","0x400d2a10","0x400d2a10","0x400d2a10","0x400d2a10"],
  "task_name": "poll_sensors",
  "raw": "Guru Meditation Error: Core 0 paniced (LoadProhibited).\nPC: 0x400d2a10 EXCCAUSE: 0x0000001c EXCVADDR: 0x3ffb0000\nBacktrace: 0x400d2a10:0x3ffb0010 0x400d2a10:0x3ffb0010\nRebooting..."
}
'@
        Post-Json "$BASE/ingest" $body
    }
}

function Test-MissingBuildId {
    Run-Test "POST /ingest - missing build_id (expect 422)" {
        $body = '{"panic_type":"guru_meditation","raw":"oops"}'
        $code = Get-StatusCode "POST" "$BASE/ingest" $body
        Write-Host "HTTP $code"
    }
}

function Test-MissingRaw {
    Run-Test "POST /ingest - missing raw field (expect 422)" {
        $body = '{"build_id":"abc123","panic_type":"guru_meditation"}'
        $code = Get-StatusCode "POST" "$BASE/ingest" $body
        Write-Host "HTTP $code"
    }
}

function Test-AuthNoConfig {
    Run-Test "POST /api/auth/login - no Supabase config (expect 503)" {
        $body = '{"email":"test@example.com","password":"secret"}'
        $code = Get-StatusCode "POST" "$BASE/api/auth/login" $body
        Write-Host "HTTP $code"
    }
}

# --------------------------------------------------------------------------

switch ($Test) {
    "health"                { Test-Health }
    "ingest_null_ptr"       { Test-IngestNullPtr }
    "ingest_watchdog"       { Test-IngestWatchdog }
    "ingest_stack_overflow" { Test-IngestStackOverflow }
    "missing_build_id"      { Test-MissingBuildId }
    "missing_raw"           { Test-MissingRaw }
    "auth_no_config"        { Test-AuthNoConfig }
    "all" {
        Test-Health
        Test-IngestNullPtr
        Test-IngestWatchdog
        Test-IngestStackOverflow
        Test-MissingBuildId
        Test-MissingRaw
        Test-AuthNoConfig
    }
    default {
        Write-Host "Unknown test: $Test" -ForegroundColor Red
        Write-Host "Available: health ingest_null_ptr ingest_watchdog ingest_stack_overflow missing_build_id missing_raw auth_no_config all"
        exit 1
    }
}
