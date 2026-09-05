"""
db.py — Camada de acesso a dados.

Funciona com SQLite localmente (arquivo estoque.db, sem precisar
configurar nada) e com PostgreSQL em produção no Render, bastando
que a variável de ambiente DATABASE_URL esteja definida (o Render
já cria essa variável automaticamente quando você conecta um banco
Postgres ao serviço web).
"""

import os
import re
import json
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

    # Cadastro mestre de produtos: código de cadastro + descrição.
    if IS_PG:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS produtos (
                id SERIAL PRIMARY KEY,
                codigo TEXT UNIQUE NOT NULL,
                descricao TEXT NOT NULL,
                qtde_por_loja INTEGER NOT NULL DEFAULT 1,
                criado_por TEXT,
                criado_em TEXT
            )
        """)
    else:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS produtos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                codigo TEXT UNIQUE NOT NULL,
                descricao TEXT NOT NULL,
                qtde_por_loja INTEGER NOT NULL DEFAULT 1,
                criado_por TEXT,
                criado_em TEXT
            )
        """)

    conn.commit()

    # Kit padrão de loja: configuração independente usada pelo simulador de inauguração.
    if IS_PG:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS kit_padrao_loja (
                id SERIAL PRIMARY KEY,
                codigo TEXT,
                descricao TEXT NOT NULL,
                quantidade INTEGER NOT NULL DEFAULT 1,
                criado_por TEXT,
                criado_em TEXT
            )
        """)
    else:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS kit_padrao_loja (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                codigo TEXT,
                descricao TEXT NOT NULL,
                quantidade INTEGER NOT NULL DEFAULT 1,
                criado_por TEXT,
                criado_em TEXT
            )
        """)
    conn.commit()

    # Auditoria de autenticação: registra sucesso, falha, bloqueio e troca de senha.
    if IS_PG:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS login_eventos (
                id SERIAL PRIMARY KEY,
                username TEXT,
                ip TEXT,
                resultado TEXT NOT NULL,
                motivo TEXT,
                data_hora TEXT NOT NULL
            )
        """)
    else:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS login_eventos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT,
                ip TEXT,
                resultado TEXT NOT NULL,
                motivo TEXT,
                data_hora TEXT NOT NULL
            )
        """)
    conn.commit()

    # Cadastro de filiais / destinos de equipamentos.
    if IS_PG:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS filiais (
                id SERIAL PRIMARY KEY,
                codigo TEXT UNIQUE NOT NULL,
                nome TEXT,
                cidade TEXT,
                uf TEXT,
                bandeira TEXT,
                previsao_abertura TEXT,
                ativo TEXT NOT NULL DEFAULT '1',
                criado_por TEXT,
                criado_em TEXT
            )
        """)
    else:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS filiais (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                codigo TEXT UNIQUE NOT NULL,
                nome TEXT,
                cidade TEXT,
                uf TEXT,
                bandeira TEXT,
                previsao_abertura TEXT,
                ativo TEXT NOT NULL DEFAULT '1',
                criado_por TEXT,
                criado_em TEXT
            )
        """)
    conn.commit()

    # Migração: identifica a bandeira da filial (DSP ou DPA).
    # O campo fica vazio nos registros antigos até que sejam classificados no cadastro.
    try:
        if IS_PG:
            cur.execute("ALTER TABLE filiais ADD COLUMN IF NOT EXISTS bandeira TEXT")
        else:
            cur.execute("ALTER TABLE filiais ADD COLUMN bandeira TEXT")
        conn.commit()
    except Exception:
        conn.rollback()

    # Migração: data prevista de abertura da filial (opcional).
    try:
        if IS_PG:
            cur.execute("ALTER TABLE filiais ADD COLUMN IF NOT EXISTS previsao_abertura TEXT")
        else:
            cur.execute("ALTER TABLE filiais ADD COLUMN previsao_abertura TEXT")
        conn.commit()
    except Exception:
        conn.rollback()

    # Acompanhamento operacional de Expansão / Ampliação / Realocação.
    # A filial é a chave do acompanhamento e permite atualização por planilha.
    if IS_PG:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS acompanhamento_expansao (
                id SERIAL PRIMARY KEY,
                filial TEXT UNIQUE NOT NULL,
                bandeira TEXT,
                descricao_filial TEXT,
                uf TEXT,
                projeto TEXT,
                status_filial TEXT,
                term_obra TEXT,
                entrada_ti TEXT,
                inauguracao TEXT,
                observacao_ti TEXT,
                atualizado_por TEXT,
                atualizado_em TEXT
            )
        """)
    else:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS acompanhamento_expansao (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filial TEXT UNIQUE NOT NULL,
                bandeira TEXT,
                descricao_filial TEXT,
                uf TEXT,
                projeto TEXT,
                status_filial TEXT,
                term_obra TEXT,
                entrada_ti TEXT,
                inauguracao TEXT,
                observacao_ti TEXT,
                atualizado_por TEXT,
                atualizado_em TEXT
            )
        """)
    conn.commit()

    # Índices leves para os filtros e gráficos do dashboard.
    for nome_indice, coluna in (
        ("idx_acomp_exp_status", "status_filial"),
        ("idx_acomp_exp_projeto", "projeto"),
        ("idx_acomp_exp_bandeira", "bandeira"),
        ("idx_acomp_exp_uf", "uf"),
    ):
        try:
            cur.execute(f"CREATE INDEX IF NOT EXISTS {nome_indice} ON acompanhamento_expansao ({coluna})")
            conn.commit()
        except Exception:
            conn.rollback()

    # Base inicial enviada para o acompanhamento. A carga ocorre somente
    # quando a tabela está vazia; uploads posteriores nunca são sobrescritos.
    try:
        cur.execute("SELECT COUNT(*) FROM acompanhamento_expansao")
        qtd_acomp = cur.fetchone()[0]
        if int(qtd_acomp or 0) == 0:
            seed_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "acompanhamento_expansao_inicial.json")
            if os.path.exists(seed_path):
                with open(seed_path, "r", encoding="utf-8") as arq_seed:
                    seed = json.load(arq_seed)
                agora_seed = datetime.now().strftime("%Y-%m-%d %H:%M")
                valores = []
                for item in seed:
                    filial = str(item.get("FILIAL") or "").strip()
                    if not filial:
                        continue
                    valores.append((
                        filial,
                        str(item.get("BANDEIRA") or "").strip(),
                        str(item.get("DESCRIÇÃO FILIAL") or "").strip(),
                        str(item.get("UF") or "").strip(),
                        str(item.get("PROJETO") or "").strip(),
                        str(item.get("STATUS FILIAL") or "").strip(),
                        str(item.get("TERM. OBRA") or "").strip(),
                        str(item.get("ENTRADA DE TI") or "").strip(),
                        str(item.get("INAUGURAÇÃO") or "").strip(),
                        str(item.get("OBSERVAÇÃO TI") or "").strip(),
                        "carga inicial",
                        agora_seed,
                    ))
                if valores:
                    cur.executemany(
                        q("INSERT INTO acompanhamento_expansao (filial,bandeira,descricao_filial,uf,projeto,status_filial,term_obra,entrada_ti,inauguracao,observacao_ti,atualizado_por,atualizado_em) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"),
                        valores,
                    )
                    conn.commit()
    except Exception:
        conn.rollback()

    # Auditoria de importações e validações de arquivos.
    if IS_PG:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS importacoes (
                id SERIAL PRIMARY KEY,
                tipo TEXT NOT NULL,
                arquivo TEXT,
                total_linhas INTEGER DEFAULT 0,
                validas INTEGER DEFAULT 0,
                criadas INTEGER DEFAULT 0,
                atualizadas INTEGER DEFAULT 0,
                ignoradas INTEGER DEFAULT 0,
                usuario TEXT,
                status TEXT,
                detalhes TEXT,
                data_hora TEXT NOT NULL
            )
        """)
    else:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS importacoes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tipo TEXT NOT NULL,
                arquivo TEXT,
                total_linhas INTEGER DEFAULT 0,
                validas INTEGER DEFAULT 0,
                criadas INTEGER DEFAULT 0,
                atualizadas INTEGER DEFAULT 0,
                ignoradas INTEGER DEFAULT 0,
                usuario TEXT,
                status TEXT,
                detalhes TEXT,
                data_hora TEXT NOT NULL
            )
        """)
    conn.commit()

    # Configurações persistentes do sistema. A meta de lojas usada na simulação
    # fica aqui para que Dashboard, relatórios e Loja 3D usem o mesmo valor.
    if IS_PG:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS configuracoes (
                chave TEXT PRIMARY KEY,
                valor TEXT NOT NULL,
                atualizado_por TEXT,
                atualizado_em TEXT
            )
        """)
    else:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS configuracoes (
                chave TEXT PRIMARY KEY,
                valor TEXT NOT NULL,
                atualizado_por TEXT,
                atualizado_em TEXT
            )
        """)
    conn.commit()
    cur.execute(q("SELECT valor FROM configuracoes WHERE chave = ?"), ("meta_lojas_expansao",))
    if cur.fetchone() is None:
        cur.execute(
            q("INSERT INTO configuracoes (chave, valor, atualizado_por, atualizado_em) VALUES (?, ?, ?, ?)"),
            ("meta_lojas_expansao", "10", "sistema", datetime.now().strftime("%Y-%m-%d %H:%M")),
        )
        conn.commit()

    # Na primeira execução, carrega o kit padrão informado para uma loja.
    cur.execute("SELECT COUNT(*) FROM kit_padrao_loja")
    if cur.fetchone()[0] == 0:
        kit_inicial = [
            (None, "Micro Dell", 14),
            (None, "Monitores", 13),
            (None, "Scanner com fio", 12),
            (None, "Scanner sem fio", 1),
            (None, "Impressora Epson", 5),
            (None, "Impressora i9", 1),
            (None, "Impressora L42", 1),
            (None, "Impressora Lexmark", 1),
            (None, "Teclado", 9),
            (None, "Mouse", 9),
            (None, "Gaveta", 4),
            (None, "Pinpad", 4),
            (None, "Tira-teima", 1),
            (None, "Teclado TEC55", 4),
            (None, "Display tela cliente", 4),
            (None, "Kit coletor (coletor, bandoleira e capa)", 1),
            (None, "Kit AP (AP e POE)", 1),
            (None, "Firewall", 1),
            (None, "Headset", 1),
            (None, "Switch", 1),
            (None, "Rack", 1),
            (None, "ATA", 1),
            (None, "VOIP", 1),
            (None, "Telefone sem fio", 1),
        ]
        agora_kit = datetime.now().strftime("%Y-%m-%d %H:%M")
        cur.executemany(
            q("INSERT INTO kit_padrao_loja (codigo, descricao, quantidade, criado_por, criado_em) VALUES (?, ?, ?, ?, ?)"),
            [(codigo, descricao, quantidade, "sistema", agora_kit) for codigo, descricao, quantidade in kit_inicial],
        )
        conn.commit()

    # Migração: quantidade necessária de cada produto para montar uma loja completa.
    # Produtos existentes recebem 1 como padrão e podem ser ajustados no cadastro.
    try:
        if IS_PG:
            cur.execute("ALTER TABLE produtos ADD COLUMN IF NOT EXISTS qtde_por_loja INTEGER NOT NULL DEFAULT 1")
        else:
            cur.execute("ALTER TABLE produtos ADD COLUMN qtde_por_loja INTEGER NOT NULL DEFAULT 1")
        conn.commit()
    except Exception:
        conn.rollback()

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
        ("filial_destino", "TEXT"),
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

    # Migração MFA/TOTP: compatível com Microsoft Authenticator e Google Authenticator.
    mfa_colunas = [
        ("mfa_enabled", "TEXT DEFAULT '0'"),
        ("mfa_secret", "TEXT"),
        ("mfa_recovery_codes", "TEXT"),
        ("mfa_configurado_em", "TEXT"),
    ]
    for coluna, tipo in mfa_colunas:
        try:
            if IS_PG:
                cur.execute(f"ALTER TABLE usuarios ADD COLUMN IF NOT EXISTS {coluna} {tipo}")
            else:
                cur.execute(f"ALTER TABLE usuarios ADD COLUMN {coluna} {tipo}")
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
                             "atualizado_por", "atualizado_em", "pedido", "val_aquis", "chamado",
                             "filial_destino"]
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

    # Índices para acelerar filtros, buscas e dashboards em bases maiores.
    indices = [
        "CREATE INDEX IF NOT EXISTS idx_itens_codigo ON itens(codigo)",
        "CREATE INDEX IF NOT EXISTS idx_itens_tipo ON itens(tipo_estoque)",
        "CREATE INDEX IF NOT EXISTS idx_itens_filial ON itens(filial_destino)",
        "CREATE INDEX IF NOT EXISTS idx_imob_codigo ON imobilizados(codigo)",
        "CREATE INDEX IF NOT EXISTS idx_imob_filial ON imobilizados(filial_destino)",
        "CREATE INDEX IF NOT EXISTS idx_filiais_status ON filiais(ativo)",
        "CREATE INDEX IF NOT EXISTS idx_filiais_uf ON filiais(uf)",
        "CREATE INDEX IF NOT EXISTS idx_filiais_previsao ON filiais(previsao_abertura)",
        "CREATE INDEX IF NOT EXISTS idx_mov_data ON movimentacoes(data_hora)",
        "CREATE INDEX IF NOT EXISTS idx_import_data ON importacoes(data_hora)",
    ]
    for sql_idx in indices:
        try:
            cur.execute(sql_idx)
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
# Produtos (cadastro mestre)
# ---------------------------------------------------------------------

def _chave_codigo_natural(valor):
    """Ordena códigos como 1, 2, 3, 10 (e também códigos alfanuméricos)."""
    partes = re.split(r"(\d+)", str(valor or "").strip().lower())
    return [int(p) if p.isdigit() else p for p in partes]

def listar_produtos():
    conn = get_conn(); cur = get_cursor(conn)
    cur.execute("SELECT * FROM produtos")
    rows = cur.fetchall(); result = [dict(r) for r in rows]
    result.sort(key=lambda item: _chave_codigo_natural(item.get("codigo")))
    cur.close(); conn.close(); return result

def buscar_produto_por_codigo(codigo):
    conn = get_conn(); cur = get_cursor(conn)
    cur.execute(q("SELECT * FROM produtos WHERE codigo = ?"), (codigo,))
    row = cur.fetchone(); result = dict(row) if row else None
    cur.close(); conn.close(); return result

def criar_produto(codigo, descricao, qtde_por_loja, usuario):
    conn = get_conn(); cur = get_cursor(conn)
    agora = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        if IS_PG:
            cur.execute(q("INSERT INTO produtos (codigo, descricao, qtde_por_loja, criado_por, criado_em) VALUES (?, ?, ?, ?, ?) RETURNING id"),
                        (codigo, descricao, qtde_por_loja, usuario, agora))
            new_id = cur.fetchone()["id"]
        else:
            cur.execute(q("INSERT INTO produtos (codigo, descricao, qtde_por_loja, criado_por, criado_em) VALUES (?, ?, ?, ?, ?)"),
                        (codigo, descricao, qtde_por_loja, usuario, agora))
            new_id = cur.lastrowid
        conn.commit(); return new_id
    except Exception:
        conn.rollback(); raise
    finally:
        cur.close(); conn.close()

def atualizar_produto(produto_id, codigo, descricao, qtde_por_loja):
    conn = get_conn(); cur = get_cursor(conn)
    cur.execute(q("UPDATE produtos SET codigo = ?, descricao = ?, qtde_por_loja = ? WHERE id = ?"),
                (codigo, descricao, qtde_por_loja, produto_id))
    ok = cur.rowcount > 0; conn.commit(); cur.close(); conn.close(); return ok

def excluir_produto(produto_id):
    conn = get_conn(); cur = get_cursor(conn)
    cur.execute(q("DELETE FROM produtos WHERE id = ?"), (produto_id,))
    ok = cur.rowcount > 0; conn.commit(); cur.close(); conn.close(); return ok


# ---------------------------------------------------------------------
# Configurações do sistema
# ---------------------------------------------------------------------

def obter_configuracao(chave, padrao=None):
    conn = get_conn(); cur = get_cursor(conn)
    cur.execute(q("SELECT valor FROM configuracoes WHERE chave = ?"), (chave,))
    row = cur.fetchone()
    if row is None:
        valor = padrao
    elif isinstance(row, dict):
        valor = row.get("valor", padrao)
    else:
        try:
            valor = row["valor"]
        except Exception:
            valor = row[0]
    cur.close(); conn.close(); return valor

def salvar_configuracao(chave, valor, usuario=None):
    conn = get_conn(); cur = get_cursor(conn)
    agora = datetime.now().strftime("%Y-%m-%d %H:%M")
    cur.execute(q("SELECT chave FROM configuracoes WHERE chave = ?"), (chave,))
    existe = cur.fetchone() is not None
    if existe:
        cur.execute(q("UPDATE configuracoes SET valor = ?, atualizado_por = ?, atualizado_em = ? WHERE chave = ?"),
                    (str(valor), usuario, agora, chave))
    else:
        cur.execute(q("INSERT INTO configuracoes (chave, valor, atualizado_por, atualizado_em) VALUES (?, ?, ?, ?)"),
                    (chave, str(valor), usuario, agora))
    conn.commit(); cur.close(); conn.close(); return True

def obter_meta_lojas_expansao():
    try:
        valor = int(obter_configuracao("meta_lojas_expansao", "10") or 10)
    except (TypeError, ValueError):
        valor = 10
    return max(1, min(valor, 999))

def salvar_meta_lojas_expansao(valor, usuario=None):
    valor = max(1, min(int(valor), 999))
    salvar_configuracao("meta_lojas_expansao", valor, usuario)
    return valor


# ---------------------------------------------------------------------
# Kit padrão de loja
# ---------------------------------------------------------------------

def listar_kit_padrao_loja():
    conn = get_conn(); cur = get_cursor(conn)
    cur.execute("SELECT * FROM kit_padrao_loja ORDER BY id")
    rows = [dict(r) for r in cur.fetchall()]
    cur.close(); conn.close(); return rows

def buscar_item_kit(item_id):
    conn = get_conn(); cur = get_cursor(conn)
    cur.execute(q("SELECT * FROM kit_padrao_loja WHERE id = ?"), (item_id,))
    row = cur.fetchone(); result = dict(row) if row else None
    cur.close(); conn.close(); return result

def criar_item_kit(codigo, descricao, quantidade, usuario):
    conn = get_conn(); cur = get_cursor(conn)
    agora = datetime.now().strftime("%Y-%m-%d %H:%M")
    if IS_PG:
        cur.execute(q("INSERT INTO kit_padrao_loja (codigo, descricao, quantidade, criado_por, criado_em) VALUES (?, ?, ?, ?, ?) RETURNING id"),
                    (codigo or None, descricao, quantidade, usuario, agora))
        novo_id = cur.fetchone()["id"]
    else:
        cur.execute(q("INSERT INTO kit_padrao_loja (codigo, descricao, quantidade, criado_por, criado_em) VALUES (?, ?, ?, ?, ?)"),
                    (codigo or None, descricao, quantidade, usuario, agora))
        novo_id = cur.lastrowid
    conn.commit(); cur.close(); conn.close(); return novo_id

def atualizar_item_kit(item_id, codigo, descricao, quantidade):
    conn = get_conn(); cur = get_cursor(conn)
    cur.execute(q("UPDATE kit_padrao_loja SET codigo = ?, descricao = ?, quantidade = ? WHERE id = ?"),
                (codigo or None, descricao, quantidade, item_id))
    ok = cur.rowcount > 0; conn.commit(); cur.close(); conn.close(); return ok

def excluir_item_kit(item_id):
    conn = get_conn(); cur = get_cursor(conn)
    cur.execute(q("DELETE FROM kit_padrao_loja WHERE id = ?"), (item_id,))
    ok = cur.rowcount > 0; conn.commit(); cur.close(); conn.close(); return ok


def obter_dashboard_compacto(limite_movs=20):
    """Retorna somente os dados necessários ao Dashboard.

    Agrupa o Estoque por código/descrição/finalidade para evitar enviar milhares
    de linhas individuais ao navegador e busca as referências das movimentações
    em lote. Funciona em SQLite e PostgreSQL.
    """
    def qtd_num(valor):
        try:
            return int(float(valor or 0))
        except (TypeError, ValueError):
            return 0

    conn = get_conn()
    cur = get_cursor(conn)
    try:
        # Estoque compacto: somente 4 colunas e já agrupado em memória.
        cur.execute("SELECT codigo, descricao, qtde, tipo_estoque FROM itens ORDER BY id")
        agrupado = {}
        total_estoque = 0
        for row in cur.fetchall():
            d = dict(row)
            qtd = qtd_num(d.get("qtde"))
            if qtd <= 0:
                continue
            total_estoque += qtd
            key = (
                str(d.get("codigo") or "").strip(),
                str(d.get("descricao") or "").strip(),
                str(d.get("tipo_estoque") or "").strip(),
            )
            if key not in agrupado:
                agrupado[key] = {
                    "codigo": key[0], "descricao": key[1],
                    "tipo_estoque": key[2], "qtde": 0,
                }
            agrupado[key]["qtde"] += qtd
        itens = list(agrupado.values())

        # Imobilizados: o Dashboard precisa somente do total de unidades.
        cur.execute("SELECT qtde FROM imobilizados")
        imobilizados_total = sum(qtd_num(dict(r).get("qtde")) for r in cur.fetchall())

        # Produtos: somente código e descrição são usados nos tooltips/flyouts.
        cur.execute("SELECT codigo, descricao FROM produtos ORDER BY id")
        produtos = [dict(r) for r in cur.fetchall()]
        produtos_total = len(produtos)

        # Kit padrão usado na simulação da meta.
        cur.execute("SELECT id, codigo, descricao, quantidade FROM kit_padrao_loja ORDER BY id")
        kit = [dict(r) for r in cur.fetchall()]

        # Filiais: conjunto compacto para cálculo da visão executiva.
        cur.execute("SELECT id, codigo, nome, uf, ativo, previsao_abertura FROM filiais ORDER BY codigo")
        filiais = [dict(r) for r in cur.fetchall()]
        filiais_ativas = sum(1 for f in filiais if str(f.get("ativo") or "") == "1")

        # Meta persistida, reaproveitando a mesma conexão.
        cur.execute(q("SELECT valor FROM configuracoes WHERE chave = ?"), ("meta_lojas_expansao",))
        row = cur.fetchone()
        if row:
            rd = dict(row) if hasattr(row, "keys") else {"valor": row[0]}
            try:
                meta_lojas = max(1, min(int(rd.get("valor") or 10), 999))
            except (TypeError, ValueError):
                meta_lojas = 10
        else:
            meta_lojas = 10

        # Últimas movimentações e referências somente dos IDs necessários.
        limite_movs = max(1, min(int(limite_movs or 20), 100))
        cur.execute(q("SELECT * FROM movimentacoes ORDER BY id DESC LIMIT ?"), (limite_movs,))
        movs = [dict(r) for r in cur.fetchall()]
        ids_itens = sorted({int(m.get("item_id")) for m in movs if (m.get("tabela") or "itens") == "itens" and m.get("item_id") is not None})
        ids_imob = sorted({int(m.get("item_id")) for m in movs if m.get("tabela") == "imobilizados" and m.get("item_id") is not None})

        refs_itens = {}
        refs_imob = {}
        if ids_itens:
            ph = ",".join(["?"] * len(ids_itens))
            cur.execute(q(f"SELECT id, codigo, descricao FROM itens WHERE id IN ({ph})"), ids_itens)
            refs_itens = {str(dict(r).get("id")): dict(r) for r in cur.fetchall()}
        if ids_imob:
            ph = ",".join(["?"] * len(ids_imob))
            cur.execute(q(f"SELECT id, codigo, descricao FROM imobilizados WHERE id IN ({ph})"), ids_imob)
            refs_imob = {str(dict(r).get("id")): dict(r) for r in cur.fetchall()}

        for m in movs:
            tabela = m.get("tabela") or "itens"
            if tabela == "sistema":
                m["codigo"] = "META LOJAS"
                m["descricao"] = "Meta do lote de inauguração"
            else:
                ref = (refs_imob if tabela == "imobilizados" else refs_itens).get(str(m.get("item_id")), {})
                m["codigo"] = ref.get("codigo", "")
                m["descricao"] = ref.get("descricao", "")

        return {
            "itens": itens,
            "estoque_total": total_estoque,
            "imobilizados_total": imobilizados_total,
            "produtos": produtos,
            "produtos_total": produtos_total,
            "kit": kit,
            "filiais": filiais,
            "filiais_ativas": filiais_ativas,
            "meta_lojas": meta_lojas,
            "movimentacoes": movs,
        }
    finally:
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
              "pedido", "val_aquis", "chamado", "filial_destino"]
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
              "pedido", "val_aquis", "chamado", "filial_destino"]

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
                          "pedido", "val_aquis", "chamado", "filial_destino"]
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
              "atualizado_por", "atualizado_em", "pedido", "val_aquis", "chamado",
              "filial_destino"]
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


def listar_movimentacoes_recentes(limite=100):
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute(q("SELECT * FROM movimentacoes ORDER BY id DESC LIMIT ?"), (int(limite),))
    linhas = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return linhas


def listar_todas_movimentacoes():
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute("SELECT * FROM movimentacoes ORDER BY id DESC")
    linhas = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return linhas


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
# Auditoria de autenticação
# ---------------------------------------------------------------------

def registrar_evento_login(username, ip, resultado, motivo=None):
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute(
        q("INSERT INTO login_eventos (username, ip, resultado, motivo, data_hora) VALUES (?, ?, ?, ?, ?)") ,
        (username or "", ip or "", resultado, motivo or "", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    cur.close()
    conn.close()

def listar_eventos_login_recentes(limite=200):
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute(q("SELECT * FROM login_eventos ORDER BY id DESC LIMIT ?"), (int(limite),))
    linhas = [dict(r) for r in cur.fetchall()]
    cur.close()
    conn.close()
    return linhas


# ---------------------------------------------------------------------
# Imobilizados
# ---------------------------------------------------------------------

CAMPOS_IMOBILIZADO = ["codigo", "descricao", "qtde", "localizacao", "nf_entrada",
                       "data_entrada", "nf_saida", "data_saida", "vd_loja",
                       "local", "armazenagem", "status", "nro_imobilizado",
                       "nro_serie", "nro_patrimonio", "tipo_estoque", "criado_por",
                       "pedido", "val_aquis", "chamado", "filial_destino"]


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
                                               "filial_destino", "enviado_estoque_por", "enviado_estoque_em"]
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
              "filial_destino", "enviado_estoque_por", "enviado_estoque_em"]
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
# Filiais / destinos
# ---------------------------------------------------------------------

def listar_filiais(incluir_inativas=True):
    conn = get_conn(); cur = get_cursor(conn)
    if incluir_inativas:
        cur.execute("SELECT * FROM filiais ORDER BY codigo")
    else:
        cur.execute(q("SELECT * FROM filiais WHERE ativo = ? ORDER BY codigo"), ("1",))
    rows = [dict(r) for r in cur.fetchall()]
    rows.sort(key=lambda f: _chave_codigo_natural(f.get("codigo")))
    cur.close(); conn.close(); return rows

def buscar_filial_por_id(filial_id):
    conn = get_conn(); cur = get_cursor(conn)
    cur.execute(q("SELECT * FROM filiais WHERE id = ?"), (filial_id,))
    row = cur.fetchone(); result = dict(row) if row else None
    cur.close(); conn.close(); return result

def buscar_filial_por_codigo(codigo):
    conn = get_conn(); cur = get_cursor(conn)
    cur.execute(q("SELECT * FROM filiais WHERE codigo = ?"), (str(codigo or "").strip(),))
    row = cur.fetchone(); result = dict(row) if row else None
    cur.close(); conn.close(); return result

def importar_filiais_em_lote(linhas, usuario):
    """Cria/atualiza filiais em uma única transação.

    A rotina é otimizada para planilhas grandes e funciona tanto em SQLite
    quanto em PostgreSQL/Render. Antes de gravar, carrega as filiais atuais
    em memória para preservar campos vazios vindos da planilha.
    """
    linhas = list(linhas or [])
    if not linhas:
        return {"criadas": 0, "atualizadas": 0, "sem_alteracao": 0}

    conn = get_conn()
    cur = get_cursor(conn)
    agora = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        cur.execute("SELECT id, codigo, nome, cidade, uf, bandeira, previsao_abertura, ativo FROM filiais")
        existentes = {}
        for row in cur.fetchall():
            d = dict(row)
            existentes[str(d.get("codigo") or "").strip()] = d

        inserts = []
        updates = []
        criadas = 0
        atualizadas = 0
        sem_alteracao = 0

        for item in linhas:
            codigo = str(item.get("codigo") or "").strip()
            if not codigo:
                continue
            nome = str(item.get("nome") or "").strip()
            cidade = str(item.get("cidade") or "").strip()
            uf = str(item.get("uf") or "").strip().upper()
            bandeira = str(item.get("bandeira") or "").strip().upper()
            status = str(item.get("status") or "").strip()
            previsao_abertura = str(item.get("previsao_abertura") or "").strip()
            if bandeira not in ("", "DSP", "DPA"):
                bandeira = ""

            existente = existentes.get(codigo)
            if existente:
                novo_nome = nome if nome else str(existente.get("nome") or "")
                nova_cidade = cidade if cidade else str(existente.get("cidade") or "")
                nova_uf = uf if uf else str(existente.get("uf") or "").upper()
                nova_bandeira = bandeira if bandeira else str(existente.get("bandeira") or "").upper()
                novo_status = status if status else str(existente.get("ativo") or "1")
                nova_previsao = previsao_abertura if previsao_abertura else str(existente.get("previsao_abertura") or "")
                mudou = (
                    str(existente.get("nome") or "") != novo_nome
                    or str(existente.get("cidade") or "") != nova_cidade
                    or str(existente.get("uf") or "").upper() != nova_uf
                    or str(existente.get("bandeira") or "").upper() != nova_bandeira
                    or str(existente.get("ativo") or "") != novo_status
                    or str(existente.get("previsao_abertura") or "") != nova_previsao
                )
                if mudou:
                    updates.append((novo_nome, nova_cidade, nova_uf, nova_bandeira, nova_previsao, novo_status, existente["id"]))
                    atualizadas += 1
                else:
                    sem_alteracao += 1
            else:
                novo_status = status or "1"
                inserts.append((codigo, nome, cidade, uf, bandeira, previsao_abertura, novo_status, usuario, agora))
                criadas += 1

        # executemany é suportado de forma consistente pelo SQLite e pelo
        # psycopg2/RealDictCursor usado no Render. Para ~2 mil lojas é rápido
        # e evita incompatibilidades observadas com helpers de batch.
        if updates:
            cur.executemany(
                q("UPDATE filiais SET nome=?, cidade=?, uf=?, bandeira=?, previsao_abertura=?, ativo=? WHERE id=?"),
                updates,
            )
        if inserts:
            cur.executemany(
                q("INSERT INTO filiais (codigo,nome,cidade,uf,bandeira,previsao_abertura,ativo,criado_por,criado_em) VALUES (?,?,?,?,?,?,?,?,?)"),
                inserts,
            )

        conn.commit()
        return {"criadas": criadas, "atualizadas": atualizadas, "sem_alteracao": sem_alteracao}
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()

def criar_filial(codigo, nome, cidade, uf, ativo, usuario, bandeira=None, previsao_abertura=None):
    conn = get_conn(); cur = get_cursor(conn)
    agora = datetime.now().strftime("%Y-%m-%d %H:%M")
    bandeira = (str(bandeira or "").strip().upper() if bandeira else "")
    if bandeira not in ("DSP", "DPA"):
        bandeira = ""
    try:
        if IS_PG:
            cur.execute(q("INSERT INTO filiais (codigo,nome,cidade,uf,bandeira,previsao_abertura,ativo,criado_por,criado_em) VALUES (?,?,?,?,?,?,?,?,?) RETURNING id"),
                        (codigo,nome,cidade,uf,bandeira,previsao_abertura or "",ativo,usuario,agora))
            novo_id = cur.fetchone()["id"]
        else:
            cur.execute(q("INSERT INTO filiais (codigo,nome,cidade,uf,bandeira,previsao_abertura,ativo,criado_por,criado_em) VALUES (?,?,?,?,?,?,?,?,?)"),
                        (codigo,nome,cidade,uf,bandeira,previsao_abertura or "",ativo,usuario,agora))
            novo_id = cur.lastrowid
        conn.commit(); return novo_id
    except Exception:
        conn.rollback(); raise
    finally:
        cur.close(); conn.close()

def atualizar_filial(filial_id, codigo, nome, cidade, uf, ativo, bandeira=None, previsao_abertura=None):
    conn = get_conn(); cur = get_cursor(conn)
    bandeira = (str(bandeira or "").strip().upper() if bandeira else "")
    if bandeira not in ("DSP", "DPA"):
        bandeira = ""
    try:
        cur.execute(q("SELECT codigo FROM filiais WHERE id = ?"), (filial_id,))
        row = cur.fetchone()
        if not row:
            return False
        codigo_antigo = row["codigo"] if isinstance(row, dict) else row[0]
        cur.execute(q("UPDATE filiais SET codigo=?, nome=?, cidade=?, uf=?, bandeira=?, previsao_abertura=?, ativo=? WHERE id=?"),
                    (codigo,nome,cidade,uf,bandeira,previsao_abertura or "",ativo,filial_id))
        if str(codigo_antigo) != str(codigo):
            cur.execute(q("UPDATE itens SET filial_destino = ? WHERE filial_destino = ?"), (codigo, codigo_antigo))
            cur.execute(q("UPDATE imobilizados SET filial_destino = ? WHERE filial_destino = ?"), (codigo, codigo_antigo))
        conn.commit(); return True
    except Exception:
        conn.rollback(); raise
    finally:
        cur.close(); conn.close()

def _valor_escalar(row, chave=None, default=0):
    """Retorna um escalar de SQLite Row/tupla ou PostgreSQL RealDictRow."""
    if row is None:
        return default
    # RealDictRow herda comportamento de mapping; converter para dict evita
    # qualquer acesso posicional como row[0], que gera KeyError no Render.
    try:
        if hasattr(row, "keys"):
            dados = dict(row)
            if chave and chave in dados:
                return dados.get(chave, default)
            return next(iter(dados.values()), default)
    except Exception:
        pass
    try:
        return row[0]
    except (KeyError, IndexError, TypeError):
        return default


def contar_referencias_filial(codigo):
    """Conta vínculos da filial de forma segura em SQLite e PostgreSQL."""
    conn = get_conn(); cur = get_cursor(conn)
    try:
        cur.execute(q("SELECT COUNT(*) AS total FROM itens WHERE filial_destino = ?"), (codigo,))
        row_itens = cur.fetchone()
        a = int(_valor_escalar(row_itens, "total", 0) or 0)
        cur.execute(q("SELECT COUNT(*) AS total FROM imobilizados WHERE filial_destino = ?"), (codigo,))
        row_imob = cur.fetchone()
        b = int(_valor_escalar(row_imob, "total", 0) or 0)
        return a + b
    finally:
        cur.close(); conn.close()


def excluir_filiais_em_lote(ids, desvincular_equipamentos=True):
    """Exclui várias filiais em uma única transação.

    Os equipamentos permanecem cadastrados. Quando solicitado, apenas o campo
    filial_destino é limpo antes da exclusão. A implementação é compatível com
    SQLite e PostgreSQL/RealDictCursor e evita abrir uma conexão por filial.
    """
    ids_limpos = []
    for valor in ids or []:
        try:
            filial_id = int(valor)
        except (TypeError, ValueError):
            continue
        if filial_id > 0 and filial_id not in ids_limpos:
            ids_limpos.append(filial_id)
    if not ids_limpos:
        return [], []

    conn = get_conn(); cur = get_cursor(conn)
    try:
        ph_ids = ",".join(["?"] * len(ids_limpos))
        cur.execute(q(f"SELECT id, codigo FROM filiais WHERE id IN ({ph_ids})"), ids_limpos)
        rows = [dict(r) for r in cur.fetchall()]
        por_id = {int(r["id"]): r for r in rows}
        encontradas = [por_id[i] for i in ids_limpos if i in por_id]
        nao_encontradas = [i for i in ids_limpos if i not in por_id]
        if not encontradas:
            return [], nao_encontradas

        codigos = [str(r.get("codigo") or "").strip() for r in encontradas]
        codigos_validos = [c for c in codigos if c]
        refs_por_codigo = {c: 0 for c in codigos_validos}

        if codigos_validos:
            ph_cod = ",".join(["?"] * len(codigos_validos))
            for tabela in ("itens", "imobilizados"):
                cur.execute(q(f"SELECT filial_destino, COUNT(*) AS total FROM {tabela} WHERE filial_destino IN ({ph_cod}) GROUP BY filial_destino"), codigos_validos)
                for row in cur.fetchall():
                    d = dict(row)
                    codigo = str(d.get("filial_destino") or "").strip()
                    refs_por_codigo[codigo] = refs_por_codigo.get(codigo, 0) + int(d.get("total") or 0)

            total_refs = sum(refs_por_codigo.values())
            if total_refs and not desvincular_equipamentos:
                return [], nao_encontradas
            if desvincular_equipamentos:
                cur.execute(q(f"UPDATE itens SET filial_destino = NULL WHERE filial_destino IN ({ph_cod})"), codigos_validos)
                cur.execute(q(f"UPDATE imobilizados SET filial_destino = NULL WHERE filial_destino IN ({ph_cod})"), codigos_validos)

        cur.execute(q(f"DELETE FROM filiais WHERE id IN ({ph_ids})"), ids_limpos)
        conn.commit()
        excluidas = [{
            "id": int(r["id"]),
            "codigo": r.get("codigo") or str(r["id"]),
            "desvinculados": int(refs_por_codigo.get(str(r.get("codigo") or "").strip(), 0)),
        } for r in encontradas]
        return excluidas, nao_encontradas
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close(); conn.close()


def excluir_filial(filial_id, desvincular_equipamentos=False):
    filial = buscar_filial_por_id(filial_id)
    if not filial:
        return False, 0
    if not desvincular_equipamentos:
        refs = contar_referencias_filial(str(filial.get("codigo") or "").strip())
        if refs:
            return False, refs
    excluidas, _ = excluir_filiais_em_lote([filial_id], desvincular_equipamentos=desvincular_equipamentos)
    if not excluidas:
        return False, 0
    return True, int(excluidas[0].get("desvinculados") or 0)


# ---------------------------------------------------------------------
# Acompanhamento de Expansão
# ---------------------------------------------------------------------

def listar_acompanhamento_expansao():
    conn = get_conn(); cur = get_cursor(conn)
    try:
        cur.execute("""
            SELECT id, filial, bandeira, descricao_filial, uf, projeto, status_filial,
                   term_obra, entrada_ti, inauguracao, observacao_ti, atualizado_por, atualizado_em
            FROM acompanhamento_expansao
            ORDER BY CASE WHEN UPPER(COALESCE(status_filial,'')) = 'PENDENTE' THEN 0 ELSE 1 END,
                     filial
        """)
        return [dict(r) for r in cur.fetchall()]
    finally:
        cur.close(); conn.close()


def buscar_acompanhamento_expansao_por_id(registro_id):
    conn = get_conn(); cur = get_cursor(conn)
    try:
        cur.execute(q("""
            SELECT id, filial, bandeira, descricao_filial, uf, projeto, status_filial,
                   term_obra, entrada_ti, inauguracao, observacao_ti, atualizado_por, atualizado_em
            FROM acompanhamento_expansao WHERE id=?
        """), (registro_id,))
        row = cur.fetchone()
        return dict(row) if row else None
    finally:
        cur.close(); conn.close()


def criar_acompanhamento_expansao(dados, usuario):
    conn = get_conn(); cur = get_cursor(conn)
    agora = datetime.now().strftime("%Y-%m-%d %H:%M")
    filial = str(dados.get("filial") or "").strip()
    if not filial:
        cur.close(); conn.close()
        raise ValueError("O campo Filial é obrigatório.")
    try:
        cur.execute(q("SELECT id FROM acompanhamento_expansao WHERE filial=?"), (filial,))
        if cur.fetchone():
            raise ValueError(f"Já existe um acompanhamento cadastrado para a filial {filial}.")
        cur.execute(
            q("""INSERT INTO acompanhamento_expansao
                 (filial,bandeira,descricao_filial,uf,projeto,status_filial,term_obra,entrada_ti,inauguracao,observacao_ti,atualizado_por,atualizado_em)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"""),
            (
                filial,
                str(dados.get("bandeira") or "").strip(),
                str(dados.get("descricao_filial") or "").strip(),
                str(dados.get("uf") or "").strip(),
                str(dados.get("projeto") or "").strip(),
                str(dados.get("status_filial") or "").strip(),
                str(dados.get("term_obra") or "").strip(),
                str(dados.get("entrada_ti") or "").strip(),
                str(dados.get("inauguracao") or "").strip(),
                str(dados.get("observacao_ti") or "").strip(),
                usuario,
                agora,
            ),
        )
        conn.commit()
        try:
            registro_id = cur.lastrowid
        except Exception:
            registro_id = None
        if not registro_id:
            cur.execute(q("SELECT id FROM acompanhamento_expansao WHERE filial=?"), (filial,))
            row = cur.fetchone()
            registro_id = dict(row).get("id") if row else None
        return buscar_acompanhamento_expansao_por_id(registro_id) if registro_id else None
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close(); conn.close()


def atualizar_acompanhamento_expansao(registro_id, dados, usuario):
    conn = get_conn(); cur = get_cursor(conn)
    agora = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        cur.execute(
            q("""UPDATE acompanhamento_expansao
                 SET filial=?, bandeira=?, descricao_filial=?, uf=?, projeto=?, status_filial=?,
                     term_obra=?, entrada_ti=?, inauguracao=?, observacao_ti=?, atualizado_por=?, atualizado_em=?
                 WHERE id=?"""),
            (
                str(dados.get("filial") or "").strip(),
                str(dados.get("bandeira") or "").strip(),
                str(dados.get("descricao_filial") or "").strip(),
                str(dados.get("uf") or "").strip(),
                str(dados.get("projeto") or "").strip(),
                str(dados.get("status_filial") or "").strip(),
                str(dados.get("term_obra") or "").strip(),
                str(dados.get("entrada_ti") or "").strip(),
                str(dados.get("inauguracao") or "").strip(),
                str(dados.get("observacao_ti") or "").strip(),
                usuario, agora, registro_id,
            ),
        )
        if cur.rowcount == 0:
            conn.rollback()
            return False
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close(); conn.close()


def importar_acompanhamento_expansao_em_lote(linhas, usuario):
    conn = get_conn(); cur = get_cursor(conn)
    agora = datetime.now().strftime("%Y-%m-%d %H:%M")
    try:
        cur.execute("""
            SELECT id, filial, bandeira, descricao_filial, uf, projeto, status_filial,
                   term_obra, entrada_ti, inauguracao, observacao_ti
            FROM acompanhamento_expansao
        """)
        existentes = {str(dict(r).get("filial") or "").strip(): dict(r) for r in cur.fetchall()}
        inserts, updates = [], []
        criadas = atualizadas = sem_alteracao = 0
        campos = ("bandeira","descricao_filial","uf","projeto","status_filial","term_obra","entrada_ti","inauguracao","observacao_ti")

        for item in linhas:
            filial = str(item.get("filial") or "").strip()
            if not filial:
                continue
            dados = {campo: str(item.get(campo) or "").strip() for campo in campos}
            atual = existentes.get(filial)
            if atual:
                mudou = any(str(atual.get(campo) or "").strip() != dados[campo] for campo in campos)
                if mudou:
                    updates.append((
                        dados["bandeira"], dados["descricao_filial"], dados["uf"], dados["projeto"],
                        dados["status_filial"], dados["term_obra"], dados["entrada_ti"], dados["inauguracao"],
                        dados["observacao_ti"], usuario, agora, atual["id"],
                    ))
                    atualizadas += 1
                else:
                    sem_alteracao += 1
            else:
                inserts.append((
                    filial, dados["bandeira"], dados["descricao_filial"], dados["uf"], dados["projeto"],
                    dados["status_filial"], dados["term_obra"], dados["entrada_ti"], dados["inauguracao"],
                    dados["observacao_ti"], usuario, agora,
                ))
                criadas += 1

        if updates:
            cur.executemany(
                q("UPDATE acompanhamento_expansao SET bandeira=?, descricao_filial=?, uf=?, projeto=?, status_filial=?, term_obra=?, entrada_ti=?, inauguracao=?, observacao_ti=?, atualizado_por=?, atualizado_em=? WHERE id=?"),
                updates,
            )
        if inserts:
            cur.executemany(
                q("INSERT INTO acompanhamento_expansao (filial,bandeira,descricao_filial,uf,projeto,status_filial,term_obra,entrada_ti,inauguracao,observacao_ti,atualizado_por,atualizado_em) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"),
                inserts,
            )
        conn.commit()
        return {"criadas": criadas, "atualizadas": atualizadas, "sem_alteracao": sem_alteracao}
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close(); conn.close()


# ---------------------------------------------------------------------
# Auditoria de importações / saúde do sistema
# ---------------------------------------------------------------------

def registrar_importacao(tipo, arquivo, total_linhas=0, validas=0, criadas=0, atualizadas=0, ignoradas=0, usuario=None, status="concluida", detalhes=None):
    conn=get_conn(); cur=get_cursor(conn)
    try:
        cur.execute(q("INSERT INTO importacoes (tipo,arquivo,total_linhas,validas,criadas,atualizadas,ignoradas,usuario,status,detalhes,data_hora) VALUES (?,?,?,?,?,?,?,?,?,?,?)"),
                    (tipo,arquivo,int(total_linhas or 0),int(validas or 0),int(criadas or 0),int(atualizadas or 0),int(ignoradas or 0),usuario,status,detalhes,datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally:
        cur.close(); conn.close()

def listar_importacoes_recentes(limite=20):
    conn=get_conn(); cur=get_cursor(conn)
    try:
        cur.execute(q("SELECT * FROM importacoes ORDER BY id DESC LIMIT ?"), (int(limite or 20),))
        return [dict(r) for r in cur.fetchall()]
    finally:
        cur.close(); conn.close()

def obter_saude_sistema():
    conn=get_conn(); cur=get_cursor(conn)
    try:
        cur.execute("SELECT 1 AS ok")
        _=cur.fetchone()
        contagens={}
        for tabela in ("itens","imobilizados","produtos","filiais","movimentacoes"):
            cur.execute(f"SELECT COUNT(*) AS total FROM {tabela}")
            contagens[tabela]=int(_valor_escalar(cur.fetchone(),"total",0) or 0)
        cur.execute("SELECT COUNT(*) AS total FROM filiais WHERE COALESCE(TRIM(uf),'') = ''")
        filiais_sem_uf=int(_valor_escalar(cur.fetchone(),"total",0) or 0)
        cur.execute("SELECT COUNT(*) AS total FROM filiais WHERE ativo IN ('1','inaugurar','pendente') AND COALESCE(TRIM(bandeira),'') = ''")
        filiais_sem_bandeira=int(_valor_escalar(cur.fetchone(),"total",0) or 0)
        cur.execute("SELECT COUNT(*) AS total FROM filiais WHERE ativo = 'inaugurar' AND COALESCE(TRIM(previsao_abertura),'') = ''")
        inaug_sem_data=int(_valor_escalar(cur.fetchone(),"total",0) or 0)
        cur.execute("SELECT data_hora FROM movimentacoes ORDER BY id DESC LIMIT 1")
        row=cur.fetchone(); ultima_mov=(dict(row).get('data_hora') if row and hasattr(row,'keys') else (row[0] if row else None))
        cur.execute("SELECT data_hora, tipo, arquivo, status FROM importacoes ORDER BY id DESC LIMIT 1")
        row=cur.fetchone(); ultima_importacao=dict(row) if row else None
        return {
            "database":"PostgreSQL" if IS_PG else "SQLite",
            "database_ok":True,
            "contagens":contagens,
            "inconsistencias":{
                "filiais_sem_uf":filiais_sem_uf,
                "filiais_sem_bandeira":filiais_sem_bandeira,
                "inauguracoes_sem_data":inaug_sem_data,
                "total":filiais_sem_uf+filiais_sem_bandeira+inaug_sem_data,
            },
            "ultima_movimentacao":ultima_mov,
            "ultima_importacao":ultima_importacao,
        }
    finally:
        cur.close(); conn.close()

# ---------------------------------------------------------------------
# Usuários
# ---------------------------------------------------------------------

def listar_usuarios():
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute("SELECT id, username, role, criado_em, precisa_trocar_senha, "
                "COALESCE(mfa_enabled, '0') AS mfa_enabled, mfa_configurado_em "
                "FROM usuarios ORDER BY id")
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


def salvar_mfa_usuario(user_id, secret_protegido, recovery_codes_json):
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute(
        q("UPDATE usuarios SET mfa_enabled = '1', mfa_secret = ?, mfa_recovery_codes = ?, mfa_configurado_em = ? WHERE id = ?"),
        (secret_protegido, recovery_codes_json, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), user_id),
    )
    afetadas = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return afetadas > 0


def desativar_mfa_usuario(user_id):
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute(
        q("UPDATE usuarios SET mfa_enabled = '0', mfa_secret = NULL, mfa_recovery_codes = NULL, mfa_configurado_em = NULL WHERE id = ?"),
        (user_id,),
    )
    afetadas = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return afetadas > 0


def atualizar_codigos_recuperacao_mfa(user_id, recovery_codes_json):
    conn = get_conn()
    cur = get_cursor(conn)
    cur.execute(
        q("UPDATE usuarios SET mfa_recovery_codes = ? WHERE id = ?"),
        (recovery_codes_json, user_id),
    )
    afetadas = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()
    return afetadas > 0


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
