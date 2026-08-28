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


def _tabela_existe(cur, nome):
    if IS_PG:
        cur.execute(
            "SELECT EXISTS (SELECT FROM information_schema.tables WHERE table_name = %s)",
            (nome,),
        )
        return cur.fetchone()[0]
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name=?", (nome,))
    return cur.fetchone() is not None


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    imobilizados_ja_existia = _tabela_existe(cur, "imobilizados")

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
        cur.execute("""
            CREATE TABLE IF NOT EXISTS imobilizados (
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
            CREATE TABLE IF NOT EXISTS cadastro_itens (
                id SERIAL PRIMARY KEY,
                codigo TEXT UNIQUE NOT NULL,
                descricao TEXT NOT NULL,
                unidade TEXT DEFAULT 'UN',
                tipo TEXT NOT NULL DEFAULT 'estoque',
                criado_por TEXT,
                criado_em TEXT,
                atualizado_por TEXT,
                atualizado_em TEXT
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
        cur.execute("""
            CREATE TABLE IF NOT EXISTS imobilizados (
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
            CREATE TABLE IF NOT EXISTS cadastro_itens (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                codigo TEXT UNIQUE NOT NULL,
                descricao TEXT NOT NULL,
                unidade TEXT DEFAULT 'UN',
                tipo TEXT NOT NULL DEFAULT 'estoque',
                criado_por TEXT,
                criado_em TEXT,
                atualizado_por TEXT,
                atualizado_em TEXT
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

    # Migração: tipo do cadastro mestre (estoque ou imobilizados).
    try:
        if IS_PG:
            cur.execute("ALTER TABLE cadastro_itens ADD COLUMN IF NOT EXISTS tipo TEXT DEFAULT 'estoque'")
        else:
            cur.execute("ALTER TABLE cadastro_itens ADD COLUMN tipo TEXT DEFAULT 'estoque'")
        conn.commit()
    except Exception:
        conn.rollback()

    # Migração: coluna que obriga o usuário a trocar a senha no primeiro acesso.
    try:
        if IS_PG:
            cur.execute("ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS precisa_trocar_senha TEXT DEFAULT '0'")
        else:
            cur.execute("ALTER TABLE usuarios ADD COLUMN precisa_trocar_senha TEXT DEFAULT '0'")
        conn.commit()
    except Exception:
        conn.rollback()

    # Migração: as mesmas colunas extras dos itens, também na tabela de imobilizados.
    colunas_imobilizados = novas_colunas + [
        ("enviado_estoque_por", "TEXT"),
        ("enviado_estoque_em", "TEXT"),
    ]
    for coluna, tipo in colunas_imobilizados:
        try:
            if IS_PG:
                cur.execute(f"ALTER TABLE imobilizados ADD COLUMN IF NOT EXISTS {coluna} {tipo}")
            else:
                cur.execute(f"ALTER TABLE imobilizados ADD COLUMN {coluna} {tipo}")
            conn.commit()
        except Exception:
            conn.rollback()

    conn.commit()

    # Migração: coluna que indica se a movimentação é do Estoque ou do Imobilizado.
    try:
        if IS_PG:
            cur.execute("ALTER TABLE movimentacoes ADD COLUMN IF NOT EXISTS tabela TEXT DEFAULT 'itens'")
        else:
            cur.execute("ALTER TABLE movimentacoes ADD COLUMN tabela TEXT DEFAULT 'itens'")
        conn.commit()
    except Exception:
        conn.rollback()

    # Migração ÚNICA: na primeira vez que a tabela "imobilizados" é criada,
    # move para lá tudo o que já estava cadastrado em "itens" (Estoque) até
    # então — porque esses dados representam registros de Imobilizado, não
    # movimentações reais de estoque.
    if not imobilizados_ja_existia:
        cur.execute("SELECT COUNT(*) FROM itens")
        total_itens_existentes = cur.fetchone()[0]
        if total_itens_existentes > 0:
            campos_copia = ["id", "codigo", "descricao", "qtde", "localizacao", "nf_entrada",
                             "data_entrada", "nf_saida", "data_saida", "vd_loja",
                             "local", "armazenagem", "status", "nro_imobilizado",
                             "nro_serie", "nro_patrimonio", "tipo_estoque", "criado_por",
                             "atualizado_por", "atualizado_em", "pedido", "val_aquis", "chamado"]
            colunas_str = ", ".join(campos_copia)
            if IS_PG:
                cur.execute(f"INSERT INTO imobilizados ({colunas_str}) SELECT {colunas_str} FROM itens")
                cur.execute("SELECT setval(pg_get_serial_sequence('imobilizados','id'), "
                            "(SELECT COALESCE(MAX(id), 1) FROM imobilizados))")
            else:
                cur.execute(f"INSERT INTO imobilizados ({colunas_str}) SELECT {colunas_str} FROM itens")
            cur.execute(q("UPDATE movimentacoes SET tabela = 'imobilizados' WHERE item_id IN "
                          f"(SELECT id FROM itens)"))
            cur.execute("DELETE FROM itens")
            conn.commit()
            print(f"[migração] {total_itens_existentes} registro(s) movido(s) de Estoque para Imobilizados "
                  f"(cadastro único, executado automaticamente).")

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
# Cadastro mestre de itens
# ---------------------------------------------------------------------

def listar_cadastro_itens(tipo=None):
    conn = get_conn()
    cur = get_cursor(conn)
    if tipo in ("estoque", "imobilizados"):
        cur.execute(q("SELECT * FROM cadastro_itens WHERE tipo = ? ORDER BY codigo"), (tipo,))
    else:
        cur.execute("SELECT * FROM cadastro_itens ORDER BY codigo")
    linhas = cur.fetchall()
    itens = [dict(r) for r in linhas]
    # Quantidade atual no destino do cadastro.
    for item in itens:
        tabela = "imobilizados" if item.get("tipo") == "imobilizados" else "itens"
        cur.execute(q(f"SELECT qtde FROM {tabela} WHERE LOWER(codigo) = LOWER(?)"), (item["codigo"],))
        linhas_destino = cur.fetchall()
        item["linhas"] = len(linhas_destino)
        total_qtde = 0
        for linha in linhas_destino:
            valor = linha["qtde"] if isinstance(linha, dict) else linha[0]
            try:
                total_qtde += int(float(valor or 0))
            except (ValueError, TypeError):
                pass
        item["quantidade"] = total_qtde
    cur.close(); conn.close()
    return itens


def buscar_cadastro_item_por_codigo(codigo):
    conn = get_conn(); cur = get_cursor(conn)
    cur.execute(q("SELECT * FROM cadastro_itens WHERE LOWER(codigo) = LOWER(?)"), (codigo,))
    row = cur.fetchone()
    item = dict(row) if row else None
    cur.close(); conn.close()
    return item


def buscar_cadastro_item_por_id(item_id):
    conn = get_conn(); cur = get_cursor(conn)
    cur.execute(q("SELECT * FROM cadastro_itens WHERE id = ?"), (item_id,))
    row = cur.fetchone()
    item = dict(row) if row else None
    cur.close(); conn.close()
    return item


def _dados_linha_inicial_cadastro(dados, usuario):
    return {
        "codigo": dados["codigo"], "descricao": dados["descricao"], "qtde": "1",
        "localizacao": "", "nf_entrada": "", "data_entrada": datetime.now().strftime("%Y-%m-%d"),
        "nf_saida": "", "data_saida": "", "vd_loja": "", "local": "",
        "armazenagem": "", "status": "", "nro_imobilizado": "", "nro_serie": "",
        "nro_patrimonio": "", "tipo_estoque": "", "criado_por": usuario,
        "pedido": "", "val_aquis": "", "chamado": "",
    }


def criar_cadastro_item(dados):
    """Cria somente o cadastro mestre.

    O lançamento operacional no Estoque/Imobilizados acontece quando o
    usuário utilizar o código na respectiva tela. O cadastro não cria uma
    linha automática nas tabelas operacionais.
    """
    conn = get_conn(); cur = get_cursor(conn)
    agora = datetime.now().strftime("%Y-%m-%d %H:%M")
    campos = ["codigo", "descricao", "unidade", "tipo", "criado_por", "criado_em"]
    valores = [dados.get("codigo", ""), dados.get("descricao", ""),
               dados.get("unidade", "UN"), dados.get("tipo", "estoque"),
               dados.get("criado_por", ""), agora]
    if IS_PG:
        cur.execute(q(f"INSERT INTO cadastro_itens ({', '.join(campos)}) VALUES ({', '.join(['?'] * len(campos))}) RETURNING *"), valores)
        item = dict(cur.fetchone())
    else:
        cur.execute(q(f"INSERT INTO cadastro_itens ({', '.join(campos)}) VALUES ({', '.join(['?'] * len(campos))})"), valores)
        novo_id = cur.lastrowid
        cur.execute(q("SELECT * FROM cadastro_itens WHERE id = ?"), (novo_id,))
        item = dict(cur.fetchone())
    conn.commit(); cur.close(); conn.close()
    return item


def atualizar_cadastro_item(item_id, dados):
    campos = ["codigo", "descricao", "unidade", "atualizado_por", "atualizado_em"]
    valores = [dados.get(c, "") for c in campos]
    conn = get_conn(); cur = get_cursor(conn)
    cur.execute(q(f"UPDATE cadastro_itens SET {', '.join(f'{c} = ?' for c in campos)} WHERE id = ?"), valores + [item_id])
    afetadas = cur.rowcount
    # Mantém descrição/código sincronizados nas linhas já existentes.
    if afetadas:
        cur.execute(q("UPDATE itens SET codigo = ?, descricao = ? WHERE LOWER(codigo) = LOWER(?)"),
                    (dados.get("codigo", ""), dados.get("descricao", ""), dados.get("codigo_anterior", dados.get("codigo", ""))))
        cur.execute(q("UPDATE imobilizados SET codigo = ?, descricao = ? WHERE LOWER(codigo) = LOWER(?)"),
                    (dados.get("codigo", ""), dados.get("descricao", ""), dados.get("codigo_anterior", dados.get("codigo", ""))))
    conn.commit(); cur.close(); conn.close()
    return afetadas > 0


def excluir_cadastro_item(item_id):
    conn = get_conn(); cur = get_cursor(conn)
    cur.execute(q("DELETE FROM cadastro_itens WHERE id = ?"), (item_id,))
    ok = cur.rowcount > 0
    conn.commit(); cur.close(); conn.close()
    return ok


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

def registrar_movimentacao(item_id, tipo, quantidade=None, usuario=None, observacao=None, tabela="itens"):
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute(
        q("INSERT INTO movimentacoes (item_id, tipo, quantidade, usuario, data_hora, observacao, tabela) "
          "VALUES (?, ?, ?, ?, ?, ?, ?)"),
        (item_id, tipo, quantidade, usuario, datetime.now().strftime("%Y-%m-%d %H:%M"), observacao, tabela),
    )
    conn.commit()
    cur.close()
    conn.close()


def listar_movimentacoes(item_id, tabela="itens"):
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute(q("SELECT * FROM movimentacoes WHERE item_id = ? AND tabela = ? ORDER BY id DESC"),
                (item_id, tabela))
    linhas = cur.fetchall()
    movs = [dict(r) for r in linhas]
    cur.close()
    conn.close()
    return movs


def excluir_itens_em_lote(ids):
    if not ids:
        return 0
    conn = get_conn()
    cur = get_cursor(conn)
    placeholders = ", ".join(["?"] * len(ids))
    cur.execute(q(f"DELETE FROM itens WHERE id IN ({placeholders})"), ids)
    afetadas = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return afetadas


# ---------------------------------------------------------------------
# Imobilizados
# ---------------------------------------------------------------------

CAMPOS_IMOBILIZADO = ["codigo", "descricao", "qtde", "localizacao", "nf_entrada",
                       "data_entrada", "nf_saida", "data_saida", "vd_loja",
                       "local", "armazenagem", "status", "nro_imobilizado",
                       "nro_serie", "nro_patrimonio", "tipo_estoque", "criado_por",
                       "pedido", "val_aquis", "chamado"]


def listar_imobilizados():
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM imobilizados ORDER BY id")
    linhas = cur.fetchall()
    itens = [dict(r) for r in linhas]
    cur.close()
    conn.close()
    return itens


def criar_imobilizado(dados):
    conn = get_conn()
    cur = get_cursor(conn)
    valores = [dados.get(c, "") for c in CAMPOS_IMOBILIZADO]
    if IS_PG:
        cur.execute(
            q(f"INSERT INTO imobilizados ({', '.join(CAMPOS_IMOBILIZADO)}) "
              f"VALUES ({', '.join(['?'] * len(CAMPOS_IMOBILIZADO))}) RETURNING id"),
            valores,
        )
        novo_id = cur.fetchone()["id"]
    else:
        cur.execute(
            q(f"INSERT INTO imobilizados ({', '.join(CAMPOS_IMOBILIZADO)}) "
              f"VALUES ({', '.join(['?'] * len(CAMPOS_IMOBILIZADO))})"),
            valores,
        )
        novo_id = cur.lastrowid
    conn.commit()
    cur.close()
    conn.close()
    return novo_id


def criar_imobilizados_em_lote(lista_dados, usuario, observacao="Importado via planilha"):
    conn = get_conn()
    cur = get_cursor(conn)
    ids_criados = []

    if IS_PG:
        for dados in lista_dados:
            valores = [dados.get(c, "") for c in CAMPOS_IMOBILIZADO]
            cur.execute(
                q(f"INSERT INTO imobilizados ({', '.join(CAMPOS_IMOBILIZADO)}) "
                  f"VALUES ({', '.join(['?'] * len(CAMPOS_IMOBILIZADO))}) RETURNING id"),
                valores,
            )
            ids_criados.append(cur.fetchone()["id"])
    else:
        for dados in lista_dados:
            valores = [dados.get(c, "") for c in CAMPOS_IMOBILIZADO]
            cur.execute(
                q(f"INSERT INTO imobilizados ({', '.join(CAMPOS_IMOBILIZADO)}) "
                  f"VALUES ({', '.join(['?'] * len(CAMPOS_IMOBILIZADO))})"),
                valores,
            )
            ids_criados.append(cur.lastrowid)

    agora = datetime.now().strftime("%Y-%m-%d %H:%M")
    mov_valores = [(item_id, "entrada", str(lista_dados[i].get("qtde", "")), usuario, agora, observacao, "imobilizados")
                   for i, item_id in enumerate(ids_criados)]
    cur.executemany(
        q("INSERT INTO movimentacoes (item_id, tipo, quantidade, usuario, data_hora, observacao, tabela) "
          "VALUES (?, ?, ?, ?, ?, ?, ?)"),
        mov_valores,
    )

    conn.commit()
    cur.close()
    conn.close()
    return len(ids_criados)


def atualizar_imobilizado(item_id, novos_dados):
    campos_permitidos = CAMPOS_IMOBILIZADO + ["atualizado_por", "atualizado_em",
                                               "enviado_estoque_por", "enviado_estoque_em"]
    sets = [c for c in campos_permitidos if c in novos_dados]
    if not sets:
        return False
    conn = get_conn()
    cur = get_cursor(conn)
    set_clause = ", ".join(f"{c} = ?" for c in sets)
    valores = [novos_dados[c] for c in sets] + [item_id]
    cur.execute(q(f"UPDATE imobilizados SET {set_clause} WHERE id = ?"), valores)
    afetadas = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return afetadas > 0


def excluir_imobilizado(item_id):
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute(q("DELETE FROM imobilizados WHERE id = ?"), (item_id,))
    afetadas = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return afetadas > 0


def excluir_imobilizados_em_lote(ids):
    if not ids:
        return 0
    conn = get_conn()
    cur = get_cursor(conn)
    placeholders = ", ".join(["?"] * len(ids))
    cur.execute(q(f"DELETE FROM imobilizados WHERE id IN ({placeholders})"), ids)
    afetadas = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return afetadas


def buscar_imobilizado_por_id(item_id):
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute(q("SELECT * FROM imobilizados WHERE id = ?"), (item_id,))
    row = cur.fetchone()
    item = dict(row) if row else None
    cur.close()
    conn.close()
    return item


def recriar_imobilizado(dados):
    """Recria um imobilizado (usado para 'desfazer' uma exclusão), preservando o ID original."""
    conn = get_conn()
    cur = get_cursor(conn)
    campos = ["id", "codigo", "descricao", "qtde", "localizacao", "nf_entrada",
              "data_entrada", "nf_saida", "data_saida", "vd_loja",
              "local", "armazenagem", "status", "nro_imobilizado",
              "nro_serie", "nro_patrimonio", "tipo_estoque", "criado_por",
              "atualizado_por", "atualizado_em", "pedido", "val_aquis", "chamado",
              "enviado_estoque_por", "enviado_estoque_em"]
    valores = [dados.get(c) for c in campos]
    cur.execute(
        q(f"INSERT INTO imobilizados ({', '.join(campos)}) VALUES ({', '.join(['?'] * len(campos))})"),
        valores,
    )
    conn.commit()
    cur.close()
    conn.close()


def enviar_imobilizado_para_estoque(imobilizado_id, usuario):
    """Gera N linhas individuais no Estoque (uma por unidade, qtde=1 cada),
    a partir de um registro de Imobilizado — sem alterar o Imobilizado original."""
    imob = buscar_imobilizado_por_id(imobilizado_id)
    if not imob:
        return None

    try:
        qtde = int(float(imob.get("qtde") or 1))
    except (ValueError, TypeError):
        qtde = 1
    qtde = max(qtde, 1)

    base = {c: imob.get(c, "") for c in CAMPOS_IMOBILIZADO if c != "qtde"}
    base["qtde"] = "1"
    base["criado_por"] = usuario
    novos_itens = [dict(base) for _ in range(qtde)]

    total = criar_itens_em_lote(
        novos_itens, usuario,
        observacao=f"Enviado do Imobilizado (código {imob.get('codigo')})"
    )

    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute(
        q("UPDATE imobilizados SET enviado_estoque_por = ?, enviado_estoque_em = ? WHERE id = ?"),
        (usuario, datetime.now().strftime("%Y-%m-%d %H:%M"), imobilizado_id),
    )
    conn.commit()
    cur.close()
    conn.close()

    return total


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
