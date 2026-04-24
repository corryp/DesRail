$ErrorActionPreference = 'Stop'

# Loop REP from 1 to 10
foreach ($REP in 1..10) {
    Write-Host $REP

    $dir = "s${REP}_batch"

    # Create the directory (Force avoids errors if it already exists)
    New-Item -ItemType Directory -Force -Path $dir | Out-Null

    # Copy the contents of master into the new directory
    Copy-Item -Path "master\*" -Destination $dir -Recurse -Force
}
