# Pull the latest daily DB backup of the Argus server to this computer, verify it, keep the last 12.
# Runs weekly from Task Scheduler (task "ArgusServerBackupPull"); safe to run by hand.
# Backups hold customer e-mails and password hashes: they live only in $Dest (outside the project, not in git).
$ErrorActionPreference = 'Stop'
$Server = 'root@203.0.113.10'
$Dest   = 'D:\Programming\_backups\argus-server'
$Py     = 'D:\Programming\argus\.venv\Scripts\python.exe'
New-Item -ItemType Directory -Force $Dest | Out-Null
$Log = Join-Path $Dest 'pull.log'

function Say($text) { "$(Get-Date -Format s) $text" | Add-Content -Encoding ASCII $Log; Write-Host $text }

try {
    $latest = (& ssh -o BatchMode=yes -o ConnectTimeout=20 $Server 'ls -t /opt/argus-server/backups/argus-accounts-*.sqlite.gz | head -1').Trim()
    if (-not $latest) { throw 'no backups on the server' }
    $name = Split-Path $latest -Leaf
    $target = Join-Path $Dest $name
    if (-not (Test-Path $target)) {
        & scp -o BatchMode=yes -o ConnectTimeout=20 "${Server}:$latest" $target
        if ($LASTEXITCODE) { throw 'scp failed' }
    }
    # integrity: gunzip + PRAGMA integrity_check + some accounts table
    $check = @'
import gzip, sqlite3, sys, tempfile, pathlib
src = pathlib.Path(sys.argv[1])
with tempfile.TemporaryDirectory() as d:
    db = pathlib.Path(d) / "x.sqlite"
    db.write_bytes(gzip.open(src, "rb").read())
    c = sqlite3.connect(db)
    assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok", "integrity_check"
    print("accounts:", c.execute("select count(*) from accounts").fetchone()[0])
    c.close()
'@
    $tmp = Join-Path $env:TEMP 'argus_backup_check.py'
    [IO.File]::WriteAllText($tmp, $check, (New-Object Text.UTF8Encoding($false)))
    $out = & $Py -I $tmp $target
    if ($LASTEXITCODE) { throw 'backup failed the integrity check' }
    Get-ChildItem $Dest -Filter 'argus-accounts-*.sqlite.gz' | Sort-Object LastWriteTime -Descending | Select-Object -Skip 12 | Remove-Item -Force
    Say "ok $name ($out)"
} catch {
    Say "FAILED: $($_.Exception.Message)"
    exit 1
}
