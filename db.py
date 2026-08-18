"""
db.py — Camada de acesso a dados.

Funciona com SQLite localmente (arquivo estoque.db, sem precisar
configurar nada) e com PostgreSQL em produção no Render, bastando
que a variável de ambiente DATABASE_URL esteja definida (o Render
já cria essa variável automaticamente quando você conecta um banco
Postgres ao serviço web).
"""

import os
from datetime import datetime

from werkzeug.security import generate_password_hash

DATABASE_URL = os.environ.get("DATABASE_URL", "")
IS_PG = DATABASE_URL.startswith("postgres")

if IS_PG:
    # Render às vezes fornece a URL como "postgres://", mas o driver
    # psycopg2 exige "postgresql://".
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    import psycopg2
    import psycopg2.extras
else:
    import sqlite3
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    SQLITE_PATH = os.path.join(BASE_DIR, "estoque.db")


def get_conn():
    if IS_PG:
        return psycopg2.connect(DATABASE_URL)
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def q(sql):
    """Converte os placeholders '?' (estilo sqlite) para '%s' (estilo postgres)."""
    return sql.replace("?", "%s") if IS_PG else sql


def get_cursor(conn):
    if IS_PG:
        return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    return conn.cursor()


def row_to_dict(row):
    return dict(row) if row is not None else None


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    if IS_PG:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS itens (
                id SERIAL PRIMARY KEY,
                codigo TEXT NOT NULL,
                descricao TEXT,
                qtde TEXT,
                localizacao TEXT,
                nf_entrada TEXT,
                data_entrada TEXT,
                nf_saida TEXT,
                data_saida TEXT,
                vd_loja TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id SERIAL PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                criado_em TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS movimentacoes (
                id SERIAL PRIMARY KEY,
                item_id INTEGER NOT NULL,
                tipo TEXT NOT NULL,
                quantidade TEXT,
                usuario TEXT,
                data_hora TEXT,
                observacao TEXT
            )
        """)
    else:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS itens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                codigo TEXT NOT NULL,
                descricao TEXT,
                qtde TEXT,
                localizacao TEXT,
                nf_entrada TEXT,
                data_entrada TEXT,
                nf_saida TEXT,
                data_saida TEXT,
                vd_loja TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS usuarios (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'user',
                criado_em TEXT
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS movimentacoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                tipo TEXT NOT NULL,
                quantidade TEXT,
                usuario TEXT,
                data_hora TEXT,
                observacao TEXT
            )
        """)

    conn.commit()

    # Migração: adiciona as colunas novas em bancos que já existiam antes
    # (sem apagar nenhum dado já cadastrado).
    novas_colunas = [
        ("local", "TEXT"),
        ("armazenagem", "TEXT"),
        ("status", "TEXT"),
        ("nro_imobilizado", "TEXT"),
        ("nro_serie", "TEXT"),
        ("nro_patrimonio", "TEXT"),
        ("tipo_estoque", "TEXT"),
        ("criado_por", "TEXT"),
        ("atualizado_por", "TEXT"),
        ("atualizado_em", "TEXT"),
        ("pedido", "TEXT"),
        ("val_aquis", "TEXT"),
        ("chamado", "TEXT"),
    ]
    for coluna, tipo in novas_colunas:
        try:
            if IS_PG:
                cur.execute(f"ALTER TABLE itens ADD COLUMN IF NOT EXISTS {coluna} {tipo}")
            else:
                cur.execute(f"ALTER TABLE itens ADD COLUMN {coluna} {tipo}")
            conn.commit()
        except Exception:
            conn.rollback()  # coluna já existe (comum no SQLite, que não tem "IF NOT EXISTS")

    # Migração: coluna que obriga o usuário a trocar a senha no primeiro acesso.
    try:
        if IS_PG:
            cur.execute("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS precisa_trocar_senha TEXT DEFAULT '0'")
        else:
            cur.execute("ALTER TABLE usuarios ADD COLUMN precisa_trocar_senha TEXT DEFAULT '0'")
        conn.commit()
    except Exception:
        conn.rollback()

    # Cria o primeiro usuário administrador automaticamente, se ainda
    # não existir nenhum usuário cadastrado.
    cur.execute("SELECT COUNT(*) FROM usuarios")
    row = cur.fetchone()
    total = row[0]
    if total == 0:
        admin_user = os.environ.get("ADMIN_USER", "admin")
        admin_pass = os.environ.get("ADMIN_PASS", "admin123")
        cur.execute(
            q("INSERT INTO usuarios (username, password_hash, role, criado_em) VALUES (?, ?, ?, ?)"),
            (admin_user, generate_password_hash(admin_pass), "admin", datetime.now().isoformat()),
        )
        conn.commit()
        print(f"[setup] Usuário administrador criado: '{admin_user}'. "
              f"{'(senha definida por ADMIN_PASS)' if os.environ.get('ADMIN_PASS') else '(senha padrão admin123 — troque assim que possível!)'}")

    cur.close()
    conn.close()


# ---------------------------------------------------------------------
# Itens
# ---------------------------------------------------------------------

def listar_itens():
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM itens ORDER BY id")
    linhas = cur.fetchall()
    itens = [dict(r) for r in linhas]
    cur.close()
    conn.close()
    return itens


def criar_item(dados):
    conn = get_conn()
    cur = get_cursor(conn)
    campos = ["codigo", "descricao", "qtde", "localizacao", "nf_entrada",
              "data_entrada", "nf_saida", "data_saida", "vd_loja",
              "local", "armazenagem", "status", "nro_imobilizado",
              "nro_serie", "nro_patrimonio", "tipo_estoque", "criado_por",
              "pedido", "val_aquis", "chamado"]
    valores = [dados.get(c, "") for c in campos]

    if IS_PG:
        cur.execute(
            q(f"INSERT INTO itens ({', '.join(campos)}) VALUES ({', '.join(['?'] * len(campos))}) RETURNING id"),
            valores,
        )
        novo_id = cur.fetchone()["id"]
    else:
        cur.execute(
            q(f"INSERT INTO itens ({', '.join(campos)}) VALUES ({', '.join(['?'] * len(campos))})"),
            valores,
        )
        novo_id = cur.lastrowid

    conn.commit()
    cur.close()
    conn.close()
    return novo_id


def criar_itens_em_lote(lista_dados, usuario, observacao="Importado via planilha"):
    """Insere muitos itens de uma vez (uma única transação) — usado na importação
    de planilhas Excel. Muito mais rápido do que chamar criar_item() em loop."""
    campos = ["codigo", "descricao", "qtde", "localizacao", "nf_entrada",
              "data_entrada", "nf_saida", "data_saida", "vd_loja",
              "local", "armazenagem", "status", "nro_imobilizado",
              "nro_serie", "nro_patrimonio", "tipo_estoque", "criado_por",
              "pedido", "val_aquis", "chamado"]

    conn = get_conn()
    cur = get_cursor(conn)
    ids_criados = []

    if IS_PG:
        for dados in lista_dados:
            valores = [dados.get(c, "") for c in campos]
            cur.execute(
                q(f"INSERT INTO itens ({', '.join(campos)}) VALUES ({', '.join(['?'] * len(campos))}) RETURNING id"),
                valores,
            )
            ids_criados.append(cur.fetchone()["id"])
    else:
        for dados in lista_dados:
            valores = [dados.get(c, "") for c in campos]
            cur.execute(
                q(f"INSERT INTO itens ({', '.join(campos)}) VALUES ({', '.join(['?'] * len(campos))})"),
                valores,
            )
            ids_criados.append(cur.lastrowid)

    agora = datetime.now().strftime("%Y-%m-%d %H:%M")
    mov_valores = [(item_id, "entrada", str(lista_dados[i].get("qtde", "")), usuario, agora, observacao)
                   for i, item_id in enumerate(ids_criados)]
    cur.executemany(
        q("INSERT INTO movimentacoes (item_id, tipo, quantidade, usuario, data_hora, observacao) "
          "VALUES (?, ?, ?, ?, ?, ?)"),
        mov_valores,
    )

    conn.commit()
    cur.close()
    conn.close()
    return len(ids_criados)


def atualizar_item(item_id, novos_dados):
    campos_permitidos = ["codigo", "descricao", "qtde", "localizacao", "nf_entrada",
                          "data_entrada", "nf_saida", "data_saida", "vd_loja",
                          "local", "armazenagem", "status", "nro_imobilizado",
                          "nro_serie", "nro_patrimonio", "tipo_estoque",
                          "atualizado_por", "atualizado_em",
                          "pedido", "val_aquis", "chamado"]
    sets = [c for c in campos_permitidos if c in novos_dados]
    if not sets:
        return False

    conn = get_conn()
    cur = get_cursor(conn)
    set_clause = ", ".join(f"{c} = ?" for c in sets)
    valores = [novos_dados[c] for c in sets] + [item_id]
    cur.execute(q(f"UPDATE itens SET {set_clause} WHERE id = ?"), valores)
    afetadas = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return afetadas > 0


def excluir_item(item_id):
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute(q("DELETE FROM itens WHERE id = ?"), (item_id,))
    afetadas = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return afetadas > 0


def buscar_item_por_id(item_id):
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute(q("SELECT * FROM itens WHERE id = ?"), (item_id,))
    row = cur.fetchone()
    item = dict(row) if row else None
    cur.close()
    conn.close()
    return item


def recriar_item(dados):
    """Recria um item (usado para 'desfazer' uma exclusão), preservando o ID original."""
    conn = get_conn()
    cur = get_cursor(conn)
    campos = ["id", "codigo", "descricao", "qtde", "localizacao", "nf_entrada",
              "data_entrada", "nf_saida", "data_saida", "vd_loja",
              "local", "armazenagem", "status", "nro_imobilizado",
              "nro_serie", "nro_patrimonio", "tipo_estoque", "criado_por",
              "atualizado_por", "atualizado_em", "pedido", "val_aquis", "chamado"]
    valores = [dados.get(c) for c in campos]
    cur.execute(
        q(f"INSERT INTO itens ({', '.join(campos)}) VALUES ({', '.join(['?'] * len(campos))})"),
        valores,
    )
    conn.commit()
    cur.close()
    conn.close()


# ---------------------------------------------------------------------
# Movimentações (histórico)
# ---------------------------------------------------------------------

def registrar_movimentacao(item_id, tipo, quantidade=None, usuario=None, observacao=None):
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute(
        q("INSERT INTO movimentacoes (item_id, tipo, quantidade, usuario, data_hora, observacao) "
          "VALUES (?, ?, ?, ?, ?, ?)"),
        (item_id, tipo, quantidade, usuario, datetime.now().strftime("%Y-%m-%d %H:%M"), observacao),
    )
    conn.commit()
    cur.close()
    conn.close()


def listar_movimentacoes(item_id):
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute(q("SELECT * FROM movimentacoes WHERE item_id = ? ORDER BY id DESC"), (item_id,))
    linhas = cur.fetchall()
    movs = [dict(r) for r in linhas]
    cur.close()
    conn.close()
    return movs


# ---------------------------------------------------------------------
# Usuários
# ---------------------------------------------------------------------

def listar_usuarios():
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute("SELECT id, username, role, criado_em, precisa_trocar_senha FROM usuarios ORDER BY id")
    linhas = cur.fetchall()
    usuarios = [dict(r) for r in linhas]
    cur.close()
    conn.close()
    return usuarios


def buscar_usuario_por_username(username):
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute(q("SELECT * FROM usuarios WHERE username = ?"), (username,))
    row = cur.fetchone()
    usuario = dict(row) if row else None
    cur.close()
    conn.close()
    return usuario


def buscar_usuario_por_id(user_id):
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute(q("SELECT * FROM usuarios WHERE id = ?"), (user_id,))
    row = cur.fetchone()
    usuario = dict(row) if row else None
    cur.close()
    conn.close()
    return usuario


def criar_usuario(username, password, role="user"):
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute(
        q("INSERT INTO usuarios (username, password_hash, role, criado_em, precisa_trocar_senha) "
          "VALUES (?, ?, ?, ?, ?)"),
        (username, generate_password_hash(password), role, datetime.now().isoformat(), "1"),
    )
    conn.commit()
    cur.close()
    conn.close()


def trocar_senha(user_id, nova_senha):
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute(
        q("UPDATE usuarios SET password_hash = ?, precisa_trocar_senha = '0' WHERE id = ?"),
        (generate_password_hash(nova_senha), user_id),
    )
    conn.commit()
    cur.close()
    conn.close()


def forcar_troca_senha(user_id):
    """Marca um usuário já existente para ser obrigado a trocar a senha no próximo login."""
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute(q("UPDATE usuarios SET precisa_trocar_senha = '1' WHERE id = ?"), (user_id,))
    afetadas = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return afetadas > 0


def excluir_usuario(user_id):
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute(q("DELETE FROM usuarios WHERE id = ?"), (user_id,))
    afetadas = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return afetadas > 0


def contar_admins():
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute(q("SELECT COUNT(*) as total FROM usuarios WHERE role = ?"), ("admin",))
    row = cur.fetchone()
    total = row["total"] if IS_PG or isinstance(row, dict) else row[0]
    cur.close()
    conn.close()
    return total
