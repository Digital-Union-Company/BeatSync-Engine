# D0 AMENDMENT 1: two replacement slots (worker 1, worker 2) to reach the required 3 VALID
# controlled-cold runs per worker count. See evidence\plan_amendment_1.json. Reset method,
# telemetry, validity criteria and the timed region are byte-identical to run_cold_sweep.ps1 --
# this file is generated from it by truncating after the per-slot function.
#
# D0: controlled cold cache-identity worker sweep (replacement slots).
#
# Answers the cheapest unresolved question behind COLD_PARALLEL_IDENTITY_REGRESSION: H2 measured cold
# only at 1 and 16 workers, so cold behaviour at 2, 4 and 8 -- all already-supported production
# settings -- is unknown. Measure that BEFORE designing any storage detection or adaptive policy.
#
# Reuses the H2-proven cold method verbatim: RAMMap64.exe -accepteula -Et (Empty Standby List).
# NOT -Es (Empty System Working Set), which demotes cached pages onto the standby list instead of
# discarding them and produced six false-cold slots in H2 attempt 1.
#
# MUST be run from an ELEVATED PowerShell. Bounded scope: discards cached file pages machine-wide and
# nothing else -- no file deleted or modified, no BeatSync cache/input/output touched, no process
# terminated, no driver/registry/config change. Read-only w.r.t. the media library.
#
# Run order and validity criteria are frozen in evidence\benchmark_plan.json BEFORE any timing.

[CmdletBinding()]
param([int]$SettleMs = 2000)

$ErrorActionPreference = 'Stop'

$D0     = 'C:\tmp\BeatSync-Engine-DigitalUnion\tasks\d0-cold-parallel-regression'
$PY     = 'C:\tmp\BeatSync-Engine-DigitalUnion\tasks\release-v0.1.0-rc1\source\bin\python-3.13.14-embed-amd64\python.exe'
$RAMMAP = 'C:\tmp\BeatSync-Engine-DigitalUnion\tasks\post-v0.1.0-h2-cold-cache-identity\tools\RAMMap\RAMMap64.exe'
$BENCH  = "$D0\harness\bench_identity.py"
$PLAN   = "$D0\evidence\benchmark_plan.json"

$RAMMAP_SHA   = 'E970913798481432CD590991577089E68510B861DC669DCAB24FEB06AAE0DF52'
$RESET_SWITCH = 'Et'

# Pre-registered in benchmark_plan.json. Reset telemetry is the PRIMARY cold authority; the ratio is
# corroborating only and never silently invalidates an otherwise proven slot.
$RESET_STANDBY_AFTER_MAX_MB  = 200
$RESET_STANDBY_BEFORE_MIN_MB = 5000
$RATIO_FLAG_BELOW            = 2.0

# Progress via Write-Host, NOT Write-Output: a function whose return value is assigned would
# otherwise swallow every progress line into the variable (the H2 validator bug). Capture with *>&1.
function Say { param([string]$m) Write-Host $m }

# ---------------------------------------------------------------- preflight
$id = [Security.Principal.WindowsIdentity]::GetCurrent()
$pr = New-Object Security.Principal.WindowsPrincipal($id)
if (-not $pr.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Say 'D0 STOP: not elevated. RAMMap -E requires an elevated PowerShell.'; exit 2
}
foreach ($p in @($PY, $RAMMAP, $BENCH, $PLAN)) {
    if (-not (Test-Path $p)) { Say "D0 STOP: missing $p"; exit 2 }
}
$actualSha = (Get-FileHash -Path $RAMMAP -Algorithm SHA256).Hash
if ($actualSha -ne $RAMMAP_SHA) { Say "D0 STOP: RAMMap SHA256 mismatch: $actualSha"; exit 2 }

# NOTE: not $plan -- PowerShell variable names are case-insensitive, so `$plan` and `$PLAN` are the
# same variable and assigning the parsed object would destroy the plan file path.
$planObj = Get-Content $PLAN -Raw | ConvertFrom-Json
$planSha = (Get-FileHash -Path $PLAN -Algorithm SHA256).Hash
$order = @($planObj.frozen_run_order_flat)
$frozenManifest = $planObj.library.manifest_sha256
if ($order.Count -ne 15) { Say "D0 STOP: frozen order has $($order.Count) slots, expected 15"; exit 2 }

Say 'D0 controlled cold worker SWEEP'
Say "  reset method   : RAMMap64.exe -accepteula -$RESET_SWITCH (Empty Standby List, H2-proven)"
Say "  plan sha256    : $planSha  (frozen before any timing)"
Say "  frozen order   : $($order -join ', ')"
Say "  frozen manifest: $frozenManifest"
Say "  reset condition: standby_before >= $RESET_STANDBY_BEFORE_MIN_MB MB AND standby_after <= $RESET_STANDBY_AFTER_MAX_MB MB AND exit 0"
Say "  ratio < $RATIO_FLAG_BELOW is FLAGGED, never silently dropped"
Say ''

New-Item -ItemType Directory -Force "$D0\evidence\runs" | Out-Null
New-Item -ItemType Directory -Force "$D0\evidence\rammap_io" | Out-Null
New-Item -ItemType Directory -Force "$D0\logs" | Out-Null

# ---------------------------------------------------------------- telemetry
function Get-CacheState {
    param([string]$Label)
    $names = @(
        '\Memory\Standby Cache Normal Priority Bytes',
        '\Memory\Standby Cache Reserve Bytes',
        '\Memory\Standby Cache Core Bytes',
        '\Memory\Free & Zero Page List Bytes',
        '\Memory\Cache Bytes',
        '\Memory\Modified Page List Bytes',
        '\Memory\Available MBytes'
    )
    $s = (Get-Counter -Counter $names).CounterSamples
    function V($pat) { ($s | Where-Object { $_.Path -like $pat } | Measure-Object -Property CookedValue -Sum).Sum }
    [PSCustomObject]@{
        label              = $Label
        sampled_utc        = (Get-Date).ToUniversalTime().ToString('o')
        standby_normal_mb  = [math]::Round((V '*standby cache normal*') / 1MB, 1)
        standby_reserve_mb = [math]::Round((V '*standby cache reserve*') / 1MB, 1)
        standby_core_mb    = [math]::Round((V '*standby cache core*') / 1MB, 1)
        standby_total_mb   = [math]::Round((V '*standby*') / 1MB, 1)
        free_mb            = [math]::Round((V '*free*') / 1MB, 1)
        system_cache_mb    = [math]::Round((V '*\Memory\Cache Bytes') / 1MB, 1)
        modified_mb        = [math]::Round((V '*modified page list*') / 1MB, 1)
        available_mb       = [math]::Round((V '*available mbytes*'), 1)
    }
}

# ---------------------------------------------------------------- reset
function Invoke-Reset {
    param([string]$Slot)
    $before = Get-CacheState -Label 'before_reset'
    $out = "$D0\evidence\rammap_io\$Slot.stdout.txt"
    $err = "$D0\evidence\rammap_io\$Slot.stderr.txt"
    $sw  = [Diagnostics.Stopwatch]::StartNew()
    $proc = Start-Process -FilePath $RAMMAP -ArgumentList @('-accepteula', "-$RESET_SWITCH") `
                          -Wait -PassThru -RedirectStandardOutput $out -RedirectStandardError $err
    try { $proc.WaitForExit() } catch { }
    $sw.Stop()
    Start-Sleep -Milliseconds $SettleMs
    $after = Get-CacheState -Label 'after_settle'

    $resetOk = ($proc.ExitCode -eq 0) -and
               ($before.standby_total_mb -ge $RESET_STANDBY_BEFORE_MIN_MB) -and
               ($after.standby_total_mb  -le $RESET_STANDBY_AFTER_MAX_MB)

    $reset = [PSCustomObject]@{
        slot                = $Slot
        switch              = "-$RESET_SWITCH"
        operation           = 'Empty Standby List'
        exit_code           = $proc.ExitCode
        waited_for_exit     = $true
        has_exited          = $proc.HasExited
        rammap_ms           = [math]::Round($sw.Elapsed.TotalMilliseconds, 1)
        stdout              = (Get-Content $out -Raw -EA SilentlyContinue)
        stderr              = (Get-Content $err -Raw -EA SilentlyContinue)
        settle_ms           = $SettleMs
        before              = $before
        after               = $after
        standby_before_mb   = $before.standby_total_mb
        standby_after_mb    = $after.standby_total_mb
        standby_dropped_mb  = [math]::Round($before.standby_total_mb - $after.standby_total_mb, 1)
        free_before_mb      = $before.free_mb
        free_after_mb       = $after.free_mb
        free_gained_mb      = [math]::Round($after.free_mb - $before.free_mb, 1)
        reset_condition_met = $resetOk
    }
    $reset | ConvertTo-Json -Depth 12 | Set-Content "$D0\evidence\runs\$Slot.reset.json" -Encoding utf8
    Say ("    reset exit={0} {1}ms : standby {2} -> {3} MB (dropped {4}), free {5} -> {6} MB | met={7}" -f `
        $reset.exit_code, $reset.rammap_ms, $reset.standby_before_mb, $reset.standby_after_mb, `
        $reset.standby_dropped_mb, $reset.free_before_mb, $reset.free_after_mb, $resetOk)
    $reset
}

# ---------------------------------------------------------------- bench
function Invoke-Bench {
    param([string]$Slot, [int]$Workers, [switch]$WarmRepeat)
    $env:BEATSYNC_CACHE_IDENTITY_WORKERS = "$Workers"
    $argv = @('-X', 'utf8', $BENCH, '--workers', "$Workers", '--slot', $Slot)
    if ($WarmRepeat) { $argv += '--warm-repeat' }
    & $PY @argv 2>&1 | Set-Content -Path "$D0\logs\$Slot.log" -Encoding utf8
    $exit = $LASTEXITCODE
    if ($exit -ne 0) { Say "    SLOT FAILED (python exit $exit) -- see logs\$Slot.log" }
    $exit
}

# ---------------------------------------------------------------- one slot
function Invoke-Slot {
    param([string]$Slot, [int]$Workers, [string]$Block, [int]$Position)

    Say "=== $Slot  (block $Block pos $Position, workers=$Workers) ==="
    $reset   = Invoke-Reset -Slot $Slot
    $exit    = Invoke-Bench -Slot $Slot -Workers $Workers -WarmRepeat
    $postRun = Get-CacheState -Label 'after_run'
    $postRun | ConvertTo-Json | Set-Content "$D0\evidence\runs\$Slot.postrun.json" -Encoding utf8

    $j = Get-Content "$D0\evidence\runs\$Slot.json" -Raw | ConvertFrom-Json
    $ratio = if ($j.warm_repeat_seconds) { [math]::Round($j.cache_identity_seconds / $j.warm_repeat_seconds, 4) } else { $null }

    $reasons = New-Object System.Collections.Generic.List[string]
    if (-not $reset.reset_condition_met) {
        $reasons.Add("reset condition not met (exit=$($reset.exit_code), standby $($reset.standby_before_mb) -> $($reset.standby_after_mb) MB)")
    }
    if ($exit -ne 0)                            { $reasons.Add("python exit $exit") }
    if ($j.effective_workers -ne $Workers)      { $reasons.Add("effective_workers $($j.effective_workers) != $Workers") }
    if ($j.source_count -ne 1815)               { $reasons.Add("source_count $($j.source_count) != 1815") }
    if ($j.manifest_sha256 -ne $frozenManifest) { $reasons.Add('manifest sha mismatch') }
    if ($j.none_count -ne 0)                    { $reasons.Add("none_count $($j.none_count)") }
    if ($j.exception_count -ne 0)               { $reasons.Add("exception: $($j.exception)") }
    if ($j.duplicate_position_count -ne 0)      { $reasons.Add("duplicate_position_count $($j.duplicate_position_count)") }
    if ($j.manifest_drift_count -ne 0)          { $reasons.Add("manifest_drift_count $($j.manifest_drift_count)") }
    if ($j.cache_files_present_after -ne 0)     { $reasons.Add("cache payload written: $($j.cache_files_present_after)") }

    $valid   = ($reasons.Count -eq 0)
    $flagged = $valid -and ($null -ne $ratio) -and ($ratio -lt $RATIO_FLAG_BELOW)

    Say ("    cold={0,10:N6}s  warm={1,9:N6}s  ratio={2,9}x  VALID={3}{4}{5}" -f `
        $j.cache_identity_seconds, $j.warm_repeat_seconds, $ratio, $valid, `
        $(if ($flagged) { '  [FLAGGED: low cold/warm separation]' } else { '' }), `
        $(if ($valid) { '' } else { "  [$($reasons -join '; ')]" }))
    Say ''

    [PSCustomObject]@{
        slot                   = $Slot
        block                  = $Block
        position               = $Position
        workers                = $Workers
        valid                  = $valid
        low_separation_flag    = $flagged
        invalid_reasons        = @($reasons)
        python_exit            = $exit
        cold_s                 = $j.cache_identity_seconds
        warm_s                 = $j.warm_repeat_seconds
        ratio                  = $ratio
        source_count           = $j.source_count
        effective_workers      = $j.effective_workers
        none_count             = $j.none_count
        exception              = $j.exception
        exception_count        = $j.exception_count
        duplicate_positions    = $j.duplicate_position_count
        manifest_drift         = $j.manifest_drift_count
        manifest_sha256        = $j.manifest_sha256
        backend_token          = $j.backend_token
        config_token           = $j.config_token
        cache_contract_version = $j.cache_contract_version
        analysis_version       = $j.analysis_version
        video_analysis_sha256  = $j.video_analysis_sha256
        digest                 = $j.ordered_keys_sha256
        warm_digest            = $j.warm_repeat_ordered_keys_sha256
        cache_files_after      = $j.cache_files_present_after
        reset_condition_met    = $reset.reset_condition_met
        reset_exit_code        = $reset.exit_code
        rammap_ms              = $reset.rammap_ms
        standby_before_mb      = $reset.standby_before_mb
        standby_after_mb       = $reset.standby_after_mb
        standby_dropped_mb     = $reset.standby_dropped_mb
        standby_after_run_mb   = $postRun.standby_total_mb
        standby_rewarm_mb      = [math]::Round($postRun.standby_total_mb - $reset.standby_after_mb, 1)
    }
}


# ---------------------------------------------------------------- replacement slots
Say '=== AMENDMENT 1: replacement slots, order fixed in advance: w1 then w2 ==='
Say ''
$repl = @(
    @{ slot = 'd0_r1_w1'; w = 1 },
    @{ slot = 'd0_r2_w2'; w = 2 }
)
foreach ($r in $repl) {
    $null = Invoke-Slot -Slot $r.slot -Workers $r.w -Block 'R' -Position 1
}
Say 'Replacement slots finished. Statistics are derived from evidenceuns\*.json.'
