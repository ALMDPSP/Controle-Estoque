param(
    [Parameter(Mandatory=$true)][string]$SourceDatabaseUrl,
    [Parameter(Mandatory=$true)][string]$TargetDatabaseUrl
)

$ErrorActionPreference = "Stop"
$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$dump = "render_backup_$stamp.dump"

Write-Host "[1/3] Criando dump do Render..."
pg_dump $SourceDatabaseUrl --format=custom --no-owner --no-acl --file=$dump
if ($LASTEXITCODE -ne 0) { throw "Falha no pg_dump." }

Write-Host "[2/3] Restaurando no Neon..."
pg_restore --dbname=$TargetDatabaseUrl --clean --if-exists --no-owner --no-acl $dump
if ($LASTEXITCODE -ne 0) { throw "Falha no pg_restore." }

Write-Host "[3/3] Validando conexão com Neon..."
psql $TargetDatabaseUrl -c "SELECT current_database() AS banco, now() AS horario;"
if ($LASTEXITCODE -ne 0) { throw "Falha na validação com psql." }

Write-Host "Migração concluída. Dump local preservado em: $dump"
