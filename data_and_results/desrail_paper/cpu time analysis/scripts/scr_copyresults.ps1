
# Save as copy-results.ps1 and run in the directory containing the s*_batch folders.

# Ensure the destination folder exists
$destDir = "CopiedResults"
New-Item -ItemType Directory -Path $destDir -Force | Out-Null

for ($rep = 1; $rep -le 10; $rep++) {
    Write-Host $rep
    $src = "s${rep}_batch\output\cpu_exp.csv.csv"     # Note the double .csv.csv as in your Bash script
    $dst = Join-Path $destDir "cpu_exp$rep.csv"
    Copy-Item -Path $src -Destination $dst
}
