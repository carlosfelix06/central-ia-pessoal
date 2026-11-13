[CmdletBinding()]
param([int]$Last = 30)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$code = @'
import os, sqlite3, sys
db = os.getenv('CENTRAL_DB', 'central.sqlite3')
if not os.path.exists(db):
    sys.exit('Sem banco de auditoria ainda: ' + db)
con = sqlite3.connect('file:' + db + '?mode=ro', uri=True)
for row in con.execute('SELECT at, task, event FROM audit ORDER BY rowid DESC LIMIT ?', (int(sys.argv[1]),)):
    print(*row, sep=' | ')
'@
Push-Location $root
try { & "$root\.venv\Scripts\python.exe" -c $code $Last } finally { Pop-Location }
