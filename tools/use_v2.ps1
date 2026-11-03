# Dot-source this in a terminal:  . .\tools\use_v2.ps1
# Points the app at dp_analytics_v2 (the financial-schema database) for this terminal only.
$line = @(Get-Content .env | Where-Object { $_ -like "DP_DATABASE_URL=*" })
if ($line.Count -ne 1) { throw "expected exactly one DP_DATABASE_URL line in .env; nothing set" }
$url = $line[0].Substring("DP_DATABASE_URL=".Length).TrimEnd()
if ($url -notmatch "/dp_analytics(_v2)?$") { throw "unexpected database name in .env; nothing set" }
$env:DP_DATABASE_URL = $url -replace "/dp_analytics$", "/dp_analytics_v2"
python -c "from sqlalchemy.engine import make_url; from app.config import settings; print('this terminal points at:', make_url(settings.database_url).database)"
