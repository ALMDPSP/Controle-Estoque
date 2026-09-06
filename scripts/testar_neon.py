import os
import sys

url = os.environ.get("DATABASE_URL", "").strip()
if not url:
    print("ERRO: DATABASE_URL não definida.")
    sys.exit(1)

try:
    import psycopg2
    conn = psycopg2.connect(url, connect_timeout=12)
    cur = conn.cursor()
    cur.execute("SELECT current_database(), current_user, now()")
    banco, usuario, agora = cur.fetchone()
    cur.close(); conn.close()
    print(f"OK - banco={banco} usuario={usuario} horario={agora}")
except Exception as exc:
    print(f"ERRO ao conectar no Neon: {exc}")
    sys.exit(2)
