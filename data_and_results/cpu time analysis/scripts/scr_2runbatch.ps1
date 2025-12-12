# Run from the parent directory that contains s1_batch, s2_batch, ...

$ErrorActionPreference = 'Stop'

# Optional: limit the number of running jobs (throttle)
$MaxJobs = 6

function Get-DeslearnPath {
    param([string]$Dir)
    $candidates = @('DESLEARN.exe','DESLEARN.cmd','DESLEARN.bat','DESLEARN')
    foreach ($name in $candidates) {
        $p = Join-Path $Dir $name
        if (Test-Path $p) { return (Resolve-Path $p).Path }
    }
    return $null
}

$dirs = Get-ChildItem -Directory -Name 's*_batch'

foreach ($dir in $dirs) {
    if ($dir -match '^s(\d+)_batch$') {
        $rep = [int]$Matches[1]
    } else {
        Write-Warning ("Skipping '{0}': name doesn't match s{{REP}}_batch" -f $dir)
        continue
    }

    $wd = (Resolve-Path $dir).Path
    $exe = Get-DeslearnPath -Dir $wd
    if (-not $exe) {
        Write-Error ("No DESLEARN executable found in '{0}' (looked for DESLEARN.exe/cmd/bat/none)." -f $wd)
        continue
    }

    $finalArg = 101 * $rep
    $outLog   = Join-Path $wd 'deslearn.out.log'
    $errLog   = Join-Path $wd 'deslearn.err.log'

    foreach ($log in @($outLog, $errLog)) {
        if (Test-Path $log) { Rename-Item $log "$log.old" -Force }
    }

    Write-Host ("[{0}] Queueing {1} arg={2}" -f (Get-Date -Format 'u'), $dir, $finalArg)
    Write-Host ("  Executable: {0}" -f $exe)
    Write-Host ("  WorkingDir: {0}" -f $wd)

    while ((Get-Job | Where-Object { $_.State -match 'Running|NotStarted' }).Count -ge $MaxJobs) {
        Start-Sleep -Seconds 1
    }

    Start-Job -Name ("deslearn_{0}" -f $rep) -ScriptBlock {
        param($wd,$exe,$finalArg,$outLog,$errLog)
        $ErrorActionPreference = 'Stop'
        Push-Location $wd
        try {
            Write-Host ("[{0}] Starting {1} arg={2}" -f (Get-Date -Format 'u'), $wd, $finalArg)
            & $exe 20 100000 100 $finalArg *> $outLog 2> $errLog
            Write-Host ("[{0}] Finished {1} arg={2}" -f (Get-Date -Format 'u'), $wd, $finalArg)
        }
        catch {
            ("[{0}] ERROR: {1}" -f (Get-Date -Format 'u'), $_.Exception.Message) | Out-File -FilePath $errLog -Append
            throw
        }
        finally {
            Pop-Location
        }
    } -ArgumentList $wd,$exe,$finalArg,$outLog,$errLog | Out-Null
}

Write-Host "Launched jobs."

# Optional: wait for all to complete and summarize
Get-Job | Wait-Job | Out-Null
Get-Job | ForEach-Object {
    Write-Host ("[{0}] Job {1} -> {2}" -f (Get-Date -Format 'u'), $_.Name, $_.State)
    Remove-Job -Id $_.Id -Force
}

Write-Host "All done."
