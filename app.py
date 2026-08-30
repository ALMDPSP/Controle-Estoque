"""
Controle de Estoque - Entrada e Saída de Equipamentos
------------------------------------------------------
Programa em Python (Flask) com interface web (HTTP).

- Localmente: guarda os dados num banco SQLite (estoque.db), sem
  precisar configurar nada.
- No Render (produção): usa o PostgreSQL do próprio Render, através
  da variável de ambiente DATABASE_URL.

Tem login por usuário/senha e um botão para exportar os dados para
Excel (.xlsx) a qualquer momento.

Como rodar localmente:
    pip install -r requirements.txt
    python app.py

Depois abra no navegador:
    http://localhost:5000
    usuário: admin  senha: admin123  (troque depois de entrar)
"""

import io
import os
import unicodedata
import zipfile
import secrets
import hmac
import time
from datetime import datetime
from functools import wraps

from flask import (
    Flask, request, jsonify, render_template, redirect,
    url_for, session, send_file, flash,
)
from werkzeug.security import check_password_hash
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

import db

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "troque-esta-chave-em-producao")
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("SESSION_COOKIE_SECURE", "0") == "1",
)

# Proteções leves de autenticação. O limite é mantido em memória do processo
# para não exigir serviços externos e não altera nenhuma API já existente.
LOGIN_ATTEMPTS = {}
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 10 * 60

def _client_ip():
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return forwarded or request.remote_addr or "desconhecido"

def _login_key(username):
    return f"{_client_ip()}|{(username or '').strip().lower()}"

def _prune_login_attempts(key):
    agora = time.time()
    tentativas = [t for t in LOGIN_ATTEMPTS.get(key, []) if agora - t < LOGIN_WINDOW_SECONDS]
    if tentativas:
        LOGIN_ATTEMPTS[key] = tentativas
    else:
        LOGIN_ATTEMPTS.pop(key, None)
    return tentativas

def _login_wait_seconds(username):
    key = _login_key(username)
    tentativas = _prune_login_attempts(key)
    if len(tentativas) < LOGIN_MAX_FAILURES:
        return 0
    return max(1, int(LOGIN_WINDOW_SECONDS - (time.time() - tentativas[0])))

def _register_login_failure(username):
    key = _login_key(username)
    tentativas = _prune_login_attempts(key)
    tentativas.append(time.time())
    LOGIN_ATTEMPTS[key] = tentativas

def _clear_login_failures(username):
    LOGIN_ATTEMPTS.pop(_login_key(username), None)

def _csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token

def _csrf_ok():
    recebido = request.form.get("csrf_token", "")
    esperado = session.get("_csrf_token", "")
    return bool(recebido and esperado and hmac.compare_digest(recebido, esperado))

@app.context_processor
def _inject_security_helpers():
    return {"csrf_token": _csrf_token()}


# ---------------------------------------------------------------------
# Autenticação
# ---------------------------------------------------------------------

def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", proximo=request.path))
        if session.get("precisa_trocar_senha") and request.endpoint not in ("trocar_senha", "logout"):
            return redirect(url_for("trocar_senha"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", proximo=request.path))
        if session.get("precisa_trocar_senha"):
            return redirect(url_for("trocar_senha"))
        if session.get("role") != "admin":
            return jsonify({"erro": "Apenas administradores podem fazer isso."}), 403
        return view(*args, **kwargs)
    return wrapped


def role_required(*roles):
    permitidos=set(roles)
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("user_id"):
                return redirect(url_for("login", proximo=request.path))
            if session.get("precisa_trocar_senha"):
                return redirect(url_for("trocar_senha"))
            role=session.get("role") or "user"
            # compatibilidade: perfis antigos 'user' funcionam como operador
            if role == "user": role = "operador"
            if role not in permitidos:
                return jsonify({"erro": "Seu perfil não possui permissão para esta operação."}), 403
            return view(*args, **kwargs)
        return wrapped
    return decorator


def edit_required(view):
    return role_required("admin", "gestor", "operador")(view)


def manager_required(view):
    return role_required("admin", "gestor")(view)


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html", erro=None, username_value="")

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")

    if not _csrf_ok():
        db.registrar_evento_login(username, _client_ip(), "falha", "token de segurança inválido")
        return render_template("login.html", erro="A sessão de segurança expirou. Atualize a página e tente novamente.", username_value=username), 400

    espera = _login_wait_seconds(username)
    if espera > 0:
        minutos = max(1, (espera + 59) // 60)
        db.registrar_evento_login(username, _client_ip(), "bloqueado", "limite de tentativas excedido")
        return render_template(
            "login.html",
            erro=f"Muitas tentativas de acesso. Aguarde aproximadamente {minutos} minuto(s) e tente novamente.",
            username_value=username,
        ), 429

    usuario = db.buscar_usuario_por_username(username)

    if not usuario or not check_password_hash(usuario["password_hash"], password):
        _register_login_failure(username)
        db.registrar_evento_login(username, _client_ip(), "falha", "usuário ou senha inválidos")
        return render_template("login.html", erro="Usuário ou senha inválidos.", username_value=username), 401

    _clear_login_failures(username)
    session["user_id"] = usuario["id"]
    session["username"] = usuario["username"]
    session["role"] = usuario["role"]
    session["precisa_trocar_senha"] = usuario.get("precisa_trocar_senha") == "1"
    db.registrar_evento_login(usuario["username"], _client_ip(), "sucesso", "login realizado")

    if session["precisa_trocar_senha"]:
        return redirect(url_for("trocar_senha"))
    proximo = request.args.get("proximo") or url_for("dashboard")
    return redirect(proximo)


@app.route("/trocar-senha", methods=["GET", "POST"])
@login_required
def trocar_senha():
    if not session.get("precisa_trocar_senha"):
        return redirect(url_for("dashboard"))

    if request.method == "GET":
        return render_template("trocar_senha.html", username=session.get("username"), erro=None)

    if not _csrf_ok():
        return render_template(
            "trocar_senha.html",
            username=session.get("username"),
            erro="A sessão de segurança expirou. Atualize a página e tente novamente.",
        ), 400

    nova = request.form.get("nova_senha", "")
    confirmar = request.form.get("confirmar_senha", "")

    if len(nova) < 6:
        return render_template("trocar_senha.html", username=session.get("username"),
                                erro="A senha precisa ter pelo menos 6 caracteres.")
    if nova != confirmar:
        return render_template("trocar_senha.html", username=session.get("username"),
                                erro="As senhas não conferem.")

    db.trocar_senha(session["user_id"], nova)
    db.registrar_evento_login(session.get("username"), _client_ip(), "senha_alterada", "senha atualizada pelo usuário")
    session["precisa_trocar_senha"] = False
    session["_csrf_token"] = secrets.token_urlsafe(32)
    return redirect(url_for("dashboard"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------
# Páginas
# ---------------------------------------------------------------------

@app.route("/")
@login_required
def index():
    return render_template(
        "index.html",
        username=session.get("username"),
        role=session.get("role") or "user",
        is_admin=session.get("role") == "admin",
    )


@app.route("/dashboard")
@login_required
def dashboard():
    return render_template(
        "dashboard.html",
        username=session.get("username"),
        role=session.get("role") or "user",
        is_admin=session.get("role") == "admin",
    )


@app.route("/historico")
@login_required
def pagina_historico():
    return render_template(
        "historico.html",
        username=session.get("username"),
        role=session.get("role") or "user",
        is_admin=session.get("role") == "admin",
    )


@app.route("/relatorios")
@login_required
def pagina_relatorios():
    return render_template(
        "relatorios.html",
        username=session.get("username"),
        role=session.get("role") or "user",
        is_admin=session.get("role") == "admin",
    )


@app.route("/usuarios")
@admin_required
def pagina_usuarios():
    return render_template(
        "usuarios.html",
        username=session.get("username"),
        role=session.get("role") or "admin",
        is_admin=True,
        usuarios=db.listar_usuarios(),
    )


@app.route("/api/movimentacoes-recentes")
@login_required
def api_movimentacoes_recentes():
    try:
        limite=max(1,min(int(request.args.get("limite", 80)),500))
    except (TypeError,ValueError):
        limite=80
    movs=db.listar_movimentacoes_recentes(limite)
    itens={str(x.get("id")):x for x in db.listar_itens()}
    imobs={str(x.get("id")):x for x in db.listar_imobilizados()}
    for m in movs:
        tabela=m.get("tabela") or "itens"
        ref=(imobs if tabela=="imobilizados" else itens).get(str(m.get("item_id")), {})
        m["codigo"]=ref.get("codigo","")
        m["descricao"]=ref.get("descricao","")
    return jsonify(movs)


@app.route("/api/auditoria-login")
@admin_required
def api_auditoria_login():
    try:
        limite=max(1,min(int(request.args.get("limite", 200)),500))
    except (TypeError,ValueError):
        limite=200
    eventos=db.listar_eventos_login_recentes(limite)
    # Não expõe o IP na interface para usuários da aplicação.
    for evento in eventos:
        evento.pop("ip", None)
    return jsonify(eventos)


@app.route("/api/busca-global")
@login_required
def api_busca_global():
    termo=unicodedata.normalize("NFD", (request.args.get("q") or "").strip().lower())
    termo="".join(c for c in termo if unicodedata.category(c)!="Mn")
    if len(termo)<1:
        return jsonify([])
    resultados=[]
    campos=("codigo","descricao","nro_serie","nro_patrimonio","nro_imobilizado","localizacao","local","armazenagem","vd_loja","filial_destino","pedido","chamado")
    def combina(obj):
        alvo=" ".join(str(obj.get(c) or "") for c in campos).lower()
        alvo=unicodedata.normalize("NFD",alvo)
        alvo="".join(c for c in alvo if unicodedata.category(c)!="Mn")
        return termo in alvo
    for nome,lista in (("Estoque",db.listar_itens()),("Imobilizados",db.listar_imobilizados())):
        for obj in lista:
            if combina(obj):
                resultados.append({
                    "origem":nome,"id":obj.get("id"),"codigo":obj.get("codigo","") or "",
                    "descricao":obj.get("descricao","") or "","quantidade":obj.get("qtde","") or "",
                    "tipo_estoque":obj.get("tipo_estoque","") or "","status":obj.get("status","") or "",
                    "localizacao":obj.get("localizacao","") or "","nro_serie":obj.get("nro_serie","") or "",
                    "nro_patrimonio":obj.get("nro_patrimonio","") or "",
                    "filial_destino":obj.get("filial_destino","") or ""
                })
            if len(resultados)>=80: break
        if len(resultados)>=80: break
    for prod in db.listar_produtos():
        alvo=f"{prod.get('codigo','')} {prod.get('descricao','')}".lower()
        alvo=unicodedata.normalize("NFD",alvo)
        alvo="".join(c for c in alvo if unicodedata.category(c)!="Mn")
        if termo in alvo:
            resultados.append({"origem":"Cadastro de Produtos","id":prod.get("id"),"codigo":prod.get("codigo","") or "","descricao":prod.get("descricao","") or "","quantidade":"","tipo_estoque":"","status":"","localizacao":"","nro_serie":"","nro_patrimonio":"","filial_destino":""})
        if len(resultados)>=100: break
    return jsonify(resultados)


@app.route("/api/status-sistema")
@login_required
def api_status_sistema():
    return jsonify({
        "ultimo_backup":session.get("ultimo_backup"),
        "perfil":session.get("role") or "user",
        "usuario":session.get("username")
    })


def _preencher_planilha_dict(ws, dados, titulo=None):
    if titulo:
        ws.append([titulo])
        ws.merge_cells(start_row=1,start_column=1,end_row=1,end_column=max(1,len(dados[0]) if dados else 1))
        ws["A1"].font=Font(bold=True,size=14)
    if not dados:
        ws.append(["Sem dados"])
        return
    colunas=[]
    for obj in dados:
        for chave in obj.keys():
            if chave not in colunas: colunas.append(chave)
    ws.append(colunas)
    cab_row=ws.max_row
    fill=PatternFill("solid", fgColor="1F4E78")
    for c in ws[cab_row]:
        c.font=Font(bold=True,color="FFFFFF")
        c.fill=fill
        c.alignment=Alignment(horizontal="center")
    for obj in dados:
        ws.append([obj.get(c,"") for c in colunas])
    ws.freeze_panes=f"A{cab_row+1}"
    ws.auto_filter.ref=f"A{cab_row}:{get_column_letter(len(colunas))}{ws.max_row}"
    for idx,col in enumerate(colunas,1):
        largura=max(len(str(col)),12)
        for row in ws.iter_rows(min_row=cab_row+1,min_col=idx,max_col=idx):
            largura=max(largura,min(len(str(row[0].value or "")),45))
        ws.column_dimensions[get_column_letter(idx)].width=min(largura+2,48)


def _workbook_consolidado():
    wb=Workbook()
    wb.remove(wb.active)
    fontes=[
        ("Estoque",db.listar_itens()),
        ("Imobilizados",db.listar_imobilizados()),
        ("Produtos",db.listar_produtos()),
        ("Kit padrão",db.listar_kit_padrao_loja()),
        ("Movimentações",db.listar_todas_movimentacoes()),
    ]
    for nome,dados in fontes:
        ws=wb.create_sheet(nome[:31])
        _preencher_planilha_dict(ws,dados)
    return wb


@app.route("/export-consolidado")
@login_required
def exportar_consolidado():
    wb=_workbook_consolidado()
    buf=io.BytesIO(); wb.save(buf); buf.seek(0)
    return send_file(buf,as_attachment=True,download_name=f"relatorio_consolidado_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/export-movimentacoes")
@login_required
def exportar_movimentacoes():
    inicio=(request.args.get("inicio") or "").strip()
    fim=(request.args.get("fim") or "").strip()
    movs=db.listar_todas_movimentacoes()
    if inicio:
        movs=[m for m in movs if str(m.get("data_hora") or "")[:10] >= inicio]
    if fim:
        movs=[m for m in movs if str(m.get("data_hora") or "")[:10] <= fim]
    itens={str(x.get("id")):x for x in db.listar_itens()}
    imobs={str(x.get("id")):x for x in db.listar_imobilizados()}
    for m in movs:
        ref=(imobs if (m.get("tabela") or "itens")=="imobilizados" else itens).get(str(m.get("item_id")),{})
        m["codigo"]=ref.get("codigo","")
        m["descricao"]=ref.get("descricao","")
    wb=Workbook(); ws=wb.active; ws.title="Movimentações"; _preencher_planilha_dict(ws,movs)
    buf=io.BytesIO(); wb.save(buf); buf.seek(0)
    faixa=f"_{inicio or 'inicio'}_{fim or 'hoje'}"
    return send_file(buf,as_attachment=True,download_name=f"movimentacoes{faixa}.xlsx",mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/backup")
@login_required
def gerar_backup():
    mem=io.BytesIO()
    with zipfile.ZipFile(mem,"w",zipfile.ZIP_DEFLATED) as z:
        wb=_workbook_consolidado()
        x=io.BytesIO(); wb.save(x); x.seek(0)
        z.writestr("estoque_backup.xlsx",x.read())
        if not db.IS_PG and os.path.exists(db.SQLITE_PATH):
            z.write(db.SQLITE_PATH,arcname="estoque.db")
        z.writestr("LEIA-ME.txt",f"Backup gerado em {datetime.now().strftime('%d/%m/%Y %H:%M')} por {session.get('username')}.\nContém Estoque, Imobilizados, Produtos, Kit padrão e Histórico de movimentações.\n")
    session["ultimo_backup"]=datetime.now().strftime("%d/%m/%Y %H:%M")
    mem.seek(0)
    return send_file(mem,as_attachment=True,download_name=f"backup_controle_estoque_{datetime.now().strftime('%Y%m%d_%H%M')}.zip",mimetype="application/zip")


@app.route("/produtos")
@login_required
def pagina_produtos():
    return render_template("produtos.html", username=session.get("username"), role=session.get("role") or "user", is_admin=session.get("role") == "admin")


@app.route("/filiais")
@login_required
def pagina_filiais():
    return render_template("filiais.html", username=session.get("username"), role=session.get("role") or "user", is_admin=session.get("role") == "admin")


@app.route("/leitor-codigo")
@login_required
def pagina_leitor_codigo():
    return render_template("leitor_codigo.html", username=session.get("username"), role=session.get("role") or "user", is_admin=session.get("role") == "admin")


@app.route("/acesso-celular")
@login_required
def pagina_acesso_celular():
    host = (request.host or "").split(":")[0].lower()
    porta = request.environ.get("SERVER_PORT") or "5000"
    if host in ("localhost", "127.0.0.1", "0.0.0.0"):
        ip = descobrir_ip_local()
        url_celular = f"http://{ip}:{porta}"
        modo = "rede_local"
    else:
        esquema = request.headers.get("X-Forwarded-Proto", request.scheme or "http").split(",")[0].strip()
        url_celular = f"{esquema}://{request.host}"
        modo = "publico"
    return render_template(
        "acesso_celular.html",
        username=session.get("username"),
        role=session.get("role") or "user",
        is_admin=session.get("role") == "admin",
        url_celular=url_celular,
        modo_acesso=modo,
    )


@app.route("/service-worker.js")
def service_worker():
    return send_file(
        os.path.join(app.static_folder, "service-worker.js"),
        mimetype="application/javascript",
        max_age=0,
    )


@app.route("/loja-virtual")
@login_required
def pagina_loja_virtual():
    return render_template("loja_virtual.html", username=session.get("username"), role=session.get("role") or "user", is_admin=session.get("role") == "admin")


@app.route("/api/filiais", methods=["GET"])
@login_required
def api_listar_filiais():
    incluir_inativas = request.args.get("inativas", "1") != "0"
    return jsonify(db.listar_filiais(incluir_inativas=incluir_inativas))


@app.route("/api/filiais", methods=["POST"])
@manager_required
def api_criar_filial():
    dados = request.get_json(force=True)
    codigo = (dados.get("codigo") or "").strip()
    nome = (dados.get("nome") or "").strip()
    cidade = (dados.get("cidade") or "").strip()
    uf = (dados.get("uf") or "").strip().upper()[:2]
    ativo = "1" if str(dados.get("ativo", "1")) not in ("0", "false", "False") else "0"
    if not codigo:
        return jsonify({"erro": "Código da filial é obrigatório."}), 400
    try:
        novo_id = db.criar_filial(codigo, nome, cidade, uf, ativo, session.get("username"))
    except Exception:
        return jsonify({"erro": "Já existe uma filial cadastrada com este código."}), 409
    return jsonify({"ok": True, "id": novo_id}), 201


@app.route("/api/filiais/<int:filial_id>", methods=["PUT"])
@manager_required
def api_atualizar_filial(filial_id):
    dados = request.get_json(force=True)
    codigo = (dados.get("codigo") or "").strip()
    nome = (dados.get("nome") or "").strip()
    cidade = (dados.get("cidade") or "").strip()
    uf = (dados.get("uf") or "").strip().upper()[:2]
    ativo = "1" if str(dados.get("ativo", "1")) not in ("0", "false", "False") else "0"
    if not codigo:
        return jsonify({"erro": "Código da filial é obrigatório."}), 400
    try:
        ok = db.atualizar_filial(filial_id, codigo, nome, cidade, uf, ativo)
    except Exception:
        return jsonify({"erro": "Já existe outra filial com este código."}), 409
    return (jsonify({"ok": True}) if ok else (jsonify({"erro": "Filial não encontrada."}), 404))


@app.route("/api/filiais/<int:filial_id>", methods=["DELETE"])
@manager_required
def api_excluir_filial(filial_id):
    ok, referencias = db.excluir_filial(filial_id)
    if referencias:
        return jsonify({"erro": f"Esta filial está vinculada a {referencias} equipamento(s). Inative a filial em vez de excluir."}), 409
    return (jsonify({"ok": True}) if ok else (jsonify({"erro": "Filial não encontrada."}), 404))


ALIASES_FILIAIS_EXCEL = {
    "codigo": [
        "codigo", "codigodafilial", "codigofilial", "codigodaloja", "codigoloja",
        "codfilial", "codloja", "filial", "numerodafilial", "numerodaloja",
        "nrofilial", "nroloja", "numfilial", "numloja",
    ],
    "nome": [
        "nome", "nomedafilial", "nomedaloja", "identificacao", "identificacaodafilial",
        "nomefantasia", "descricao", "descricaodafilial", "descricaodaloja",
    ],
    "cidade": ["cidade", "municipio", "localidade", "cidadedafilial", "cidadedaloja"],
    "uf": ["uf", "estado", "siglaestado", "estadouf"],
    "ativo": ["ativo", "status", "situacao", "situacaodafilial", "situacaodaloja"],
}


def _mapear_colunas_filiais(cabecalho):
    mapa = {}
    for indice, titulo in enumerate(cabecalho):
        normalizado = _normalizar(titulo)
        for campo, aliases in ALIASES_FILIAIS_EXCEL.items():
            if normalizado in aliases:
                mapa[indice] = campo
                break
    return mapa


def _codigo_excel_para_texto(celula):
    valor = celula.value
    if valor is None:
        return ""
    if isinstance(valor, bool):
        return str(valor)
    if isinstance(valor, int):
        texto = str(valor)
    elif isinstance(valor, float) and valor.is_integer():
        texto = str(int(valor))
    else:
        return str(valor).strip()

    # Preserva zeros à esquerda quando a planilha usa formato como 0000/000000.
    formato = str(getattr(celula, "number_format", "") or "").strip()
    if formato and set(formato) <= {"0"}:
        texto = texto.zfill(len(formato))
    return texto


def _status_filial_excel(valor):
    norm = _normalizar(valor)
    if not norm:
        return "1"
    if norm in {"0", "nao", "n", "inativo", "inativa", "fechado", "fechada", "desativado", "desativada"}:
        return "0"
    return "1"


@app.route("/filiais/modelo.xlsx")
@login_required
def baixar_modelo_filiais():
    wb = Workbook()
    ws = wb.active
    ws.title = "Filiais"
    headers = ["Código da filial", "Nome / identificação", "Cidade", "UF", "Situação"]
    ws.append(headers)
    ws.append(["0123", "Filial Exemplo", "São Paulo", "SP", "Ativa"])
    ws.append(["0456", "Loja Centro", "Santos", "SP", "Ativa"])
    fill = PatternFill("solid", fgColor="1F4E78")
    for c in ws[1]:
        c.font = Font(color="FFFFFF", bold=True)
        c.fill = fill
        c.alignment = Alignment(horizontal="center")
    larguras = [20, 34, 24, 10, 14]
    for i, largura in enumerate(larguras, 1):
        ws.column_dimensions[get_column_letter(i)].width = largura
    ws.freeze_panes = "A2"
    buf = io.BytesIO()
    wb.save(buf); buf.seek(0)
    return send_file(
        buf, as_attachment=True, download_name="modelo_importacao_filiais.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/api/filiais/importar", methods=["POST"])
@manager_required
def api_importar_filiais():
    arquivo = request.files.get("arquivo")
    if not arquivo or not arquivo.filename:
        return jsonify({"erro": "Selecione uma planilha Excel."}), 400
    if not arquivo.filename.lower().endswith((".xlsx", ".xlsm")):
        return jsonify({"erro": "Envie um arquivo Excel no formato .xlsx ou .xlsm."}), 400

    try:
        try:
            wb = load_workbook(arquivo, read_only=True, data_only=True)
            ws = wb.active
        except Exception:
            return jsonify({"erro": "Não consegui abrir a planilha. Confirme se o arquivo é um Excel válido (.xlsx)."}), 400

        if ws is None:
            return jsonify({"erro": "A planilha não possui uma aba com dados."}), 400

        linhas = ws.iter_rows()
        cabecalho = None
        linha_cabecalho = 0
        # Procura o cabeçalho nas primeiras 10 linhas para aceitar planilhas com título no topo.
        for numero, linha in enumerate(linhas, start=1):
            valores = [c.value for c in linha]
            if any(v is not None and str(v).strip() for v in valores):
                mapa_teste = _mapear_colunas_filiais(valores)
                if "codigo" in mapa_teste.values():
                    cabecalho = linha
                    linha_cabecalho = numero
                    mapa = mapa_teste
                    break
            if numero >= 10:
                break

        if cabecalho is None:
            return jsonify({
                "erro": "Não encontrei a coluna de código da filial. Use títulos como 'Código da filial', 'Código da loja', 'Filial' ou 'Loja'."
            }), 400

        registros_por_codigo = {}
        vazias = 0
        duplicadas = 0
        linhas_invalidas = 0

        for linha in linhas:
            if not linha or all(c.value is None or not str(c.value).strip() for c in linha):
                vazias += 1
                continue
            dados = {}
            for indice, campo in mapa.items():
                if indice >= len(linha):
                    continue
                celula = linha[indice]
                if campo == "codigo":
                    dados[campo] = _codigo_excel_para_texto(celula)
                else:
                    dados[campo] = _valor_para_texto(celula.value)

            codigo = str(dados.get("codigo") or "").strip()
            if not codigo:
                linhas_invalidas += 1
                continue
            dados["codigo"] = codigo
            dados["nome"] = str(dados.get("nome") or "").strip()
            dados["cidade"] = str(dados.get("cidade") or "").strip()
            dados["uf"] = str(dados.get("uf") or "").strip().upper()[:2]
            dados["ativo"] = _status_filial_excel(dados.get("ativo"))

            if codigo in registros_por_codigo:
                duplicadas += 1
            registros_por_codigo[codigo] = dados

        registros = list(registros_por_codigo.values())
        if not registros:
            return jsonify({"erro": "Nenhuma filial válida foi encontrada na planilha."}), 400

        resumo = db.importar_filiais_em_lote(registros, session.get("username"))
        return jsonify({
            "ok": True,
            **resumo,
            "processadas": len(registros),
            "duplicadas": duplicadas,
            "ignoradas": linhas_invalidas,
            "linha_cabecalho": linha_cabecalho,
            "arquivo": arquivo.filename,
        })
    except Exception as e:
        print(f"[erro] Falha ao importar filiais: {e}")
        return jsonify({"erro": f"Erro ao processar a planilha de filiais: {e}"}), 500


@app.route("/api/produtos", methods=["GET"])
@login_required
def api_listar_produtos():
    return jsonify(db.listar_produtos())


@app.route("/api/produtos", methods=["POST"])
@manager_required
def api_criar_produto():
    dados = request.get_json(force=True)
    codigo = (dados.get("codigo") or "").strip()
    descricao = (dados.get("descricao") or "").strip()
    try:
        qtde_por_loja = int(dados.get("qtde_por_loja") or 1)
    except (TypeError, ValueError):
        qtde_por_loja = 0
    if not codigo or not descricao:
        return jsonify({"erro": "Código de cadastro e descrição são obrigatórios."}), 400
    if qtde_por_loja < 1:
        return jsonify({"erro": "A quantidade necessária por loja deve ser no mínimo 1."}), 400
    try:
        novo_id = db.criar_produto(codigo, descricao, qtde_por_loja, session.get("username"))
    except Exception:
        return jsonify({"erro": "Já existe um produto cadastrado com este código."}), 409
    return jsonify({"ok": True, "id": novo_id}), 201


@app.route("/api/produtos/<int:produto_id>", methods=["PUT"])
@manager_required
def api_atualizar_produto(produto_id):
    dados = request.get_json(force=True)
    codigo = (dados.get("codigo") or "").strip()
    descricao = (dados.get("descricao") or "").strip()
    try:
        qtde_por_loja = int(dados.get("qtde_por_loja") or 1)
    except (TypeError, ValueError):
        qtde_por_loja = 0
    if not codigo or not descricao:
        return jsonify({"erro": "Código de cadastro e descrição são obrigatórios."}), 400
    if qtde_por_loja < 1:
        return jsonify({"erro": "A quantidade necessária por loja deve ser no mínimo 1."}), 400
    try:
        ok = db.atualizar_produto(produto_id, codigo, descricao, qtde_por_loja)
    except Exception:
        return jsonify({"erro": "Já existe outro produto com este código."}), 409
    return (jsonify({"ok": True}) if ok else (jsonify({"erro": "Produto não encontrado."}), 404))


@app.route("/api/produtos/<int:produto_id>", methods=["DELETE"])
@manager_required
def api_excluir_produto(produto_id):
    ok = db.excluir_produto(produto_id)
    return (jsonify({"ok": True}) if ok else (jsonify({"erro": "Produto não encontrado."}), 404))


# ---------------------------------------------------------------------
# API - Kit padrão de loja
# ---------------------------------------------------------------------

@app.route("/api/kit-padrao", methods=["GET"])
@login_required
def api_listar_kit_padrao():
    return jsonify(db.listar_kit_padrao_loja())

@app.route("/api/kit-padrao", methods=["POST"])
@manager_required
def api_criar_item_kit_padrao():
    dados = request.get_json(force=True)
    codigo = (dados.get("codigo") or "").strip() or None
    descricao = (dados.get("descricao") or "").strip()
    try:
        quantidade = int(dados.get("quantidade") or 1)
    except (TypeError, ValueError):
        quantidade = 0
    if not descricao:
        return jsonify({"erro": "Descrição do item é obrigatória."}), 400
    if quantidade < 1:
        return jsonify({"erro": "A quantidade do kit deve ser no mínimo 1."}), 400
    novo_id = db.criar_item_kit(codigo, descricao, quantidade, session.get("username"))
    return jsonify({"ok": True, "id": novo_id}), 201

@app.route("/api/kit-padrao/<int:item_id>", methods=["PUT"])
@manager_required
def api_atualizar_item_kit_padrao(item_id):
    dados = request.get_json(force=True)
    codigo = (dados.get("codigo") or "").strip() or None
    descricao = (dados.get("descricao") or "").strip()
    try:
        quantidade = int(dados.get("quantidade") or 1)
    except (TypeError, ValueError):
        quantidade = 0
    if not descricao:
        return jsonify({"erro": "Descrição do item é obrigatória."}), 400
    if quantidade < 1:
        return jsonify({"erro": "A quantidade do kit deve ser no mínimo 1."}), 400
    ok = db.atualizar_item_kit(item_id, codigo, descricao, quantidade)
    return (jsonify({"ok": True}) if ok else (jsonify({"erro": "Item do kit não encontrado."}), 404))

@app.route("/api/kit-padrao/<int:item_id>", methods=["DELETE"])
@manager_required
def api_excluir_item_kit_padrao(item_id):
    ok = db.excluir_item_kit(item_id)
    return (jsonify({"ok": True}) if ok else (jsonify({"erro": "Item do kit não encontrado."}), 404))


@app.route("/imobilizados")
@login_required
def pagina_imobilizados():
    return render_template(
        "imobilizados.html",
        username=session.get("username"),
        role=session.get("role") or "user",
        is_admin=session.get("role") == "admin",
    )


# ---------------------------------------------------------------------
# API - Imobilizados
# ---------------------------------------------------------------------

@app.route("/api/imobilizados", methods=["GET"])
@login_required
def api_listar_imobilizados():
    return jsonify(db.listar_imobilizados())


@app.route("/api/imobilizados", methods=["POST"])
@edit_required
def api_criar_imobilizado():
    dados = request.get_json(force=True)
    codigo = (dados.get("codigo") or "").strip()
    if not codigo:
        return jsonify({"erro": "Código do item é obrigatório."}), 400

    novo = {
        "codigo": codigo,
        "descricao": (dados.get("descricao") or "").strip(),
        "qtde": dados.get("qtde", ""),
        "localizacao": (dados.get("localizacao") or "").strip(),
        "nf_entrada": (dados.get("nf_entrada") or "").strip(),
        "data_entrada": dados.get("data_entrada") or datetime.now().strftime("%Y-%m-%d"),
        "nf_saida": (dados.get("nf_saida") or "").strip(),
        "data_saida": (dados.get("data_saida") or "").strip(),
        "vd_loja": (dados.get("vd_loja") or "").strip(),
        "filial_destino": (dados.get("filial_destino") or "").strip(),
        "local": (dados.get("local") or "").strip(),
        "armazenagem": (dados.get("armazenagem") or "").strip(),
        "status": (dados.get("status") or "").strip(),
        "nro_imobilizado": (dados.get("nro_imobilizado") or "").strip(),
        "nro_serie": (dados.get("nro_serie") or "").strip(),
        "nro_patrimonio": (dados.get("nro_patrimonio") or "").strip(),
        "tipo_estoque": (dados.get("tipo_estoque") or "").strip(),
        "pedido": (dados.get("pedido") or "").strip(),
        "val_aquis": (dados.get("val_aquis") or "").strip(),
        "chamado": (dados.get("chamado") or "").strip(),
        "criado_por": session.get("username"),
    }
    try:
        qtde_informada = int(float(dados.get("qtde") or 1))
    except (ValueError, TypeError):
        qtde_informada = 1
    qtde_informada = max(qtde_informada, 1)
    linhas = [dict(novo, qtde="1") for _ in range(qtde_informada)]
    total = db.criar_imobilizados_em_lote(linhas, session.get("username"), observacao="Cadastro manual do imobilizado")
    return jsonify({"ok": True, "criados": total, "codigo": codigo}), 201


@app.route("/api/imobilizados/<int:item_id>", methods=["PUT"])
@edit_required
def api_atualizar_imobilizado(item_id):
    dados = request.get_json(force=True)
    item_antes = db.buscar_imobilizado_por_id(item_id)
    if not item_antes:
        return jsonify({"erro": "Item não encontrado."}), 404

    dados["atualizado_por"] = session.get("username")
    dados["atualizado_em"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    ok = db.atualizar_imobilizado(item_id, dados)
    if not ok:
        return jsonify({"erro": "Item não encontrado."}), 404

    db.registrar_movimentacao(item_id, "edicao", None, session.get("username"),
                               "Dados do imobilizado editados", tabela="imobilizados")
    return jsonify({"ok": True})


@app.route("/api/imobilizados/<int:item_id>", methods=["DELETE"])
@edit_required
def api_excluir_imobilizado(item_id):
    item = db.buscar_imobilizado_por_id(item_id)
    if not item:
        return jsonify({"erro": "Item não encontrado."}), 404
    db.registrar_movimentacao(item_id, "exclusao", item.get("qtde"), session.get("username"),
                               f"Imobilizado {item.get('codigo')} excluído", tabela="imobilizados")
    db.excluir_imobilizado(item_id)
    return jsonify({"ok": True, "item": item})


@app.route("/api/imobilizados/restaurar", methods=["POST"])
@edit_required
def api_restaurar_imobilizado():
    dados = request.get_json(force=True)
    if not dados or not dados.get("id"):
        return jsonify({"erro": "Dados inválidos para restaurar."}), 400
    if db.buscar_imobilizado_por_id(dados["id"]):
        return jsonify({"erro": "Este item já existe (não foi excluído ou já foi restaurado)."}), 400
    db.recriar_imobilizado(dados)
    db.registrar_movimentacao(dados["id"], "restauracao", dados.get("qtde"), session.get("username"),
                               "Exclusão desfeita", tabela="imobilizados")
    return jsonify({"ok": True})


@app.route("/api/imobilizados/excluir-em-lote", methods=["POST"])
@edit_required
def api_excluir_imobilizados_em_lote():
    dados = request.get_json(force=True)
    ids = dados.get("ids") or []
    if not ids:
        return jsonify({"erro": "Nenhum item selecionado."}), 400
    for item_id in ids:
        item = db.buscar_imobilizado_por_id(item_id)
        if item:
            db.registrar_movimentacao(item_id, "exclusao", item.get("qtde"), session.get("username"),
                                       f"Imobilizado {item.get('codigo')} excluído (exclusão em massa)",
                                       tabela="imobilizados")
    total = db.excluir_imobilizados_em_lote(ids)
    return jsonify({"ok": True, "excluidos": total})


@app.route("/api/imobilizados/<int:item_id>/movimentacoes")
@login_required
def api_movimentacoes_imobilizado(item_id):
    return jsonify(db.listar_movimentacoes(item_id, tabela="imobilizados"))


@app.route("/api/imobilizados/<int:item_id>/enviar-estoque", methods=["POST"])
@edit_required
def api_enviar_estoque(item_id):
    total = db.enviar_imobilizado_para_estoque(item_id, session.get("username"))
    if total is None:
        return jsonify({"erro": "Imobilizado não encontrado."}), 404
    return jsonify({"ok": True, "criados_no_estoque": total})


@app.route("/api/imobilizados/enviar-estoque-em-lote", methods=["POST"])
@edit_required
def api_enviar_estoque_em_lote():
    dados = request.get_json(force=True)
    ids = dados.get("ids") or []
    if not ids:
        return jsonify({"erro": "Nenhum item selecionado."}), 400
    total_criados = 0
    total_enviados = 0
    for item_id in ids:
        criados = db.enviar_imobilizado_para_estoque(item_id, session.get("username"))
        if criados is not None:
            total_criados += criados
            total_enviados += 1
    return jsonify({"ok": True, "imobilizados_enviados": total_enviados, "criados_no_estoque": total_criados})


@app.route("/export-imobilizados")
@login_required
def exportar_imobilizados_excel():
    itens = db.listar_imobilizados()
    wb = Workbook()
    ws = wb.active
    ws.title = "Imobilizados"
    colunas = ["ID", "Codigo do item", "Descricao", "Qtde", "Localizacao",
               "NF de entrada", "Data de entrada", "NF de saida",
               "Data de saida", "VD / referencia", "Filial destino", "Local",
               "Armazenagem", "Status", "Nro Imobilizado", "Nro Serie",
               "Nro Patrimonio", "Tipo de Estoque", "Criado por",
               "Ultima alteracao por", "Ultima alteracao em",
               "Pedido", "ValAquis.", "Chamado"]
    ws.append(colunas)
    for it in itens:
        ws.append([
            it["id"], it["codigo"], it["descricao"], it["qtde"], it["localizacao"],
            it["nf_entrada"], it["data_entrada"], it["nf_saida"],
            it["data_saida"], it["vd_loja"], it.get("filial_destino"), it.get("local"),
            it.get("armazenagem"), it.get("status"), it.get("nro_imobilizado"),
            it.get("nro_serie"), it.get("nro_patrimonio"), it.get("tipo_estoque"),
            it.get("criado_por"), it.get("atualizado_por"), it.get("atualizado_em"),
            it.get("pedido"), it.get("val_aquis"), it.get("chamado"),
        ])
    larguras = [8, 18, 32, 8, 18, 18, 16, 18, 16, 18, 22, 12, 14, 12, 16, 16, 16, 18, 14, 16, 16, 14, 12, 14]
    for i, largura in enumerate(larguras, start=1):
        ws.column_dimensions[chr(64 + i)].width = largura

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    nome_arquivo = f"imobilizados_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return send_file(
        buffer,
        as_attachment=True,
        download_name=nome_arquivo,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )




@app.route("/export-relatorio-lojas", methods=["POST"])
@login_required
def exportar_relatorio_lojas_excel():
    """Gera um relatório gerencial em Excel com o mesmo resumo usado no PDF."""
    dados = request.get_json(force=True) or {}
    estoque = dados.get("estoque") or []
    faltantes = dados.get("faltantes") or []
    kit = dados.get("kit") or []

    wb = Workbook()
    ws = wb.active
    ws.title = "Resumo"

    # Paleta e estilos
    cor_escura = "1A2029"
    cor_azul = "2876BE"
    cor_verde = "2B915D"
    cor_laranja = "CD8018"
    cor_clara = "F5F7FA"
    cor_cinza = "66707D"
    borda = Border(
        left=Side(style="thin", color="E1E5EA"),
        right=Side(style="thin", color="E1E5EA"),
        top=Side(style="thin", color="E1E5EA"),
        bottom=Side(style="thin", color="E1E5EA"),
    )

    def titulo_planilha(sheet, titulo, subtitulo=None, col_final=5):
        sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=col_final)
        c = sheet.cell(1, 1, titulo)
        c.font = Font(name="Aptos Display", size=18, bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor=cor_escura)
        c.alignment = Alignment(vertical="center")
        sheet.row_dimensions[1].height = 30
        if subtitulo:
            sheet.merge_cells(start_row=2, start_column=1, end_row=2, end_column=col_final)
            c2 = sheet.cell(2, 1, subtitulo)
            c2.font = Font(name="Aptos", size=9, color=cor_cinza)
            c2.alignment = Alignment(vertical="center")
            sheet.row_dimensions[2].height = 20

    def cabecalho(sheet, row, titulos, fill=cor_escura):
        for col, value in enumerate(titulos, start=1):
            cell = sheet.cell(row, col, value)
            cell.font = Font(name="Aptos", size=10, bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor=fill)
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            cell.border = borda
        sheet.row_dimensions[row].height = 24

    def ajustar_larguras(sheet, larguras):
        for idx, largura in enumerate(larguras, start=1):
            sheet.column_dimensions[get_column_letter(idx)].width = largura

    gerado = dados.get("gerado_em") or datetime.now().isoformat()
    titulo_planilha(ws, "Relatório de Estoque e Capacidade de Lojas", f"Gerado em: {gerado}", 5)
    ajustar_larguras(ws, [26, 28, 25, 25, 36])

    resumo = [
        ("Lojas completas possíveis", dados.get("lojas_possiveis", 0), cor_verde),
        ("Próxima loja", dados.get("proxima_loja", 1), cor_azul),
        ("Unidades em estoque", dados.get("total_unidades", 0), cor_escura),
        ("Produtos no estoque", dados.get("total_produtos", 0), cor_escura),
        ("Categorias com falta", dados.get("categorias_faltantes", 0), cor_laranja),
    ]
    linha = 4
    for rotulo, valor, cor in resumo:
        ws.cell(linha, 1, rotulo).font = Font(name="Aptos", size=10, bold=True, color=cor_cinza)
        ws.cell(linha, 2, valor).font = Font(name="Aptos Display", size=15, bold=True, color=cor)
        ws.cell(linha, 1).fill = PatternFill("solid", fgColor=cor_clara)
        ws.cell(linha, 2).fill = PatternFill("solid", fgColor=cor_clara)
        ws.cell(linha, 1).border = ws.cell(linha, 2).border = borda
        linha += 1

    linha += 1
    ws.cell(linha, 1, "Item(ns) limitante(s)").font = Font(bold=True, color=cor_laranja)
    ws.merge_cells(start_row=linha, start_column=2, end_row=linha, end_column=5)
    ws.cell(linha, 2, dados.get("item_limitante") or "-").alignment = Alignment(wrap_text=True)
    linha += 2
    ws.cell(linha, 1, "Resumo da simulação").font = Font(bold=True, color=cor_escura)
    ws.merge_cells(start_row=linha+1, start_column=1, end_row=linha+3, end_column=5)
    ws.cell(linha+1, 1, dados.get("detalhe") or "-").alignment = Alignment(wrap_text=True, vertical="top")
    ws.cell(linha+1, 1).fill = PatternFill("solid", fgColor=cor_clara)
    ws.cell(linha+1, 1).border = borda
    ws.freeze_panes = "A4"

    # Estoque detalhado
    ws_e = wb.create_sheet("Estoque detalhado")
    titulo_planilha(ws_e, "Estoque detalhado", "Quantidade consolidada por código e descrição", 3)
    cabecalho(ws_e, 4, ["Código", "Descrição do produto", "Qtd. em estoque"], cor_escura)
    for r_idx, item in enumerate(estoque, start=5):
        valores = [item.get("codigo") or "-", item.get("descricao") or "", item.get("quantidade") or 0]
        for c_idx, valor in enumerate(valores, start=1):
            c = ws_e.cell(r_idx, c_idx, valor)
            c.border = borda
            c.alignment = Alignment(vertical="center", wrap_text=(c_idx == 2), horizontal="center" if c_idx == 3 else "left")
            if r_idx % 2 == 0:
                c.fill = PatternFill("solid", fgColor="F8FAFC")
    ajustar_larguras(ws_e, [22, 52, 20])
    ws_e.freeze_panes = "A5"
    ws_e.auto_filter.ref = f"A4:C{max(4, ws_e.max_row)}"

    # Faltantes
    ws_f = wb.create_sheet("Faltantes proxima loja")
    titulo_planilha(ws_f, f"Itens faltantes para abrir a loja nº {dados.get('proxima_loja', 1)}", "Priorize esta aba para preparação da próxima inauguração", 5)
    cabecalho(ws_f, 4, ["Código", "Item", "Em estoque", "Necessário total", "Faltam"], cor_laranja)
    if faltantes:
        for r_idx, item in enumerate(faltantes, start=5):
            valores = [item.get("codigo") or "-", item.get("descricao") or "", item.get("em_estoque") or 0, item.get("necessario_total") or 0, item.get("faltam") or 0]
            for c_idx, valor in enumerate(valores, start=1):
                c = ws_f.cell(r_idx, c_idx, valor)
                c.border = borda
                c.alignment = Alignment(vertical="center", wrap_text=(c_idx == 2), horizontal="center" if c_idx >= 3 else "left")
                if c_idx == 5:
                    c.font = Font(bold=True, color="B43C2D")
                if r_idx % 2 == 0:
                    c.fill = PatternFill("solid", fgColor="FFF8ED")
    else:
        ws_f.merge_cells("A5:E6")
        ws_f["A5"] = "Nenhum item faltante para a próxima loja."
        ws_f["A5"].font = Font(bold=True, color=cor_verde)
        ws_f["A5"].alignment = Alignment(horizontal="center", vertical="center")
    ajustar_larguras(ws_f, [22, 48, 18, 20, 16])
    ws_f.freeze_panes = "A5"
    if faltantes:
        ws_f.auto_filter.ref = f"A4:E{ws_f.max_row}"

    # Kit padrão
    ws_k = wb.create_sheet("Kit padrao por loja")
    titulo_planilha(ws_k, "Kit padrão por loja", "Base utilizada no cálculo da capacidade de inauguração", 5)
    cabecalho(ws_k, 4, ["Código", "Item", "Qtd./loja", "Em estoque", "Lojas suportadas"], cor_azul)
    for r_idx, item in enumerate(kit, start=5):
        valores = [item.get("codigo") or "-", item.get("descricao") or "", item.get("qtd_por_loja") or 0, item.get("em_estoque") or 0, item.get("lojas_suportadas") or 0]
        for c_idx, valor in enumerate(valores, start=1):
            c = ws_k.cell(r_idx, c_idx, valor)
            c.border = borda
            c.alignment = Alignment(vertical="center", wrap_text=(c_idx == 2), horizontal="center" if c_idx >= 3 else "left")
            if c_idx == 5:
                c.font = Font(bold=True, color=cor_azul)
            if r_idx % 2 == 0:
                c.fill = PatternFill("solid", fgColor="F5F9FD")
    ajustar_larguras(ws_k, [22, 48, 16, 18, 20])
    ws_k.freeze_panes = "A5"
    if kit:
        ws_k.auto_filter.ref = f"A4:E{ws_k.max_row}"

    # Configurações de impressão
    for sheet in wb.worksheets:
        sheet.sheet_view.showGridLines = False
        sheet.page_setup.orientation = "landscape"
        sheet.page_setup.fitToWidth = 1
        sheet.page_setup.fitToHeight = 0
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.oddFooter.center.text = "© 2026 · Developed by Alexandre Martins"
        sheet.oddFooter.right.text = "Página &P de &N"

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    nome_arquivo = f"relatorio-estoque-lojas-{datetime.now().strftime('%Y-%m-%d')}.xlsx"
    return send_file(
        buffer,
        as_attachment=True,
        download_name=nome_arquivo,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ---------------------------------------------------------------------
# API - Itens
# ---------------------------------------------------------------------

@app.route("/api/itens", methods=["GET"])
@login_required
def api_listar():
    return jsonify(db.listar_itens())


@app.route("/api/itens", methods=["POST"])
@edit_required
def api_criar():
    dados = request.get_json(force=True)
    codigo = (dados.get("codigo") or "").strip()
    if not codigo:
        return jsonify({"erro": "Código do item é obrigatório."}), 400

    base = {
        "codigo": codigo,
        "descricao": (dados.get("descricao") or "").strip(),
        "qtde": "1",
        "localizacao": (dados.get("localizacao") or "").strip(),
        "nf_entrada": (dados.get("nf_entrada") or "").strip(),
        "data_entrada": dados.get("data_entrada") or datetime.now().strftime("%Y-%m-%d"),
        "nf_saida": (dados.get("nf_saida") or "").strip(),
        "data_saida": (dados.get("data_saida") or "").strip(),
        "vd_loja": (dados.get("vd_loja") or "").strip(),
        "filial_destino": (dados.get("filial_destino") or "").strip(),
        "local": (dados.get("local") or "").strip(),
        "armazenagem": (dados.get("armazenagem") or "").strip(),
        "status": (dados.get("status") or "").strip(),
        "nro_imobilizado": (dados.get("nro_imobilizado") or "").strip(),
        "nro_serie": (dados.get("nro_serie") or "").strip(),
        "nro_patrimonio": (dados.get("nro_patrimonio") or "").strip(),
        "tipo_estoque": (dados.get("tipo_estoque") or "").strip(),
        "pedido": (dados.get("pedido") or "").strip(),
        "val_aquis": (dados.get("val_aquis") or "").strip(),
        "chamado": (dados.get("chamado") or "").strip(),
        "criado_por": session.get("username"),
    }

    # Cada unidade vira uma linha própria no Estoque (ex: qtde 40 = 40 linhas,
    # cada uma com qtde 1) — isso permite dar saída/retirar item por item.
    try:
        qtde_informada = int(float(dados.get("qtde") or 1))
    except (ValueError, TypeError):
        qtde_informada = 1
    qtde_informada = max(qtde_informada, 1)

    linhas = [dict(base) for _ in range(qtde_informada)]
    total = db.criar_itens_em_lote(linhas, session.get("username"), observacao="Cadastro manual do item")

    return jsonify({"ok": True, "criados": total, "codigo": codigo}), 201


@app.route("/api/itens/<int:item_id>", methods=["PUT"])
@edit_required
def api_atualizar(item_id):
    dados = request.get_json(force=True)
    item_antes = db.buscar_item_por_id(item_id)
    if not item_antes:
        return jsonify({"erro": "Item não encontrado."}), 404

    dados["atualizado_por"] = session.get("username")
    dados["atualizado_em"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    ok = db.atualizar_item(item_id, dados)
    if not ok:
        return jsonify({"erro": "Item não encontrado."}), 404

    # Registra a movimentação no histórico, se a quantidade mudou.
    if "qtde" in dados:
        try:
            qtde_antes = float(item_antes.get("qtde") or 0)
            qtde_depois = float(dados["qtde"] or 0)
        except ValueError:
            qtde_antes = qtde_depois = None
        if qtde_antes is not None and qtde_depois < qtde_antes:
            diferenca = qtde_antes - qtde_depois
            if dados.get("nf_saida"):
                obs = f"Saída registrada (NF {dados.get('nf_saida')}, destino: {dados.get('filial_destino') or dados.get('vd_loja') or '-'})"
                tipo_mov = "saida"
            else:
                obs = "Retirada de estoque"
                tipo_mov = "retirada"
            db.registrar_movimentacao(item_id, tipo_mov, str(diferenca), session.get("username"), obs)
        elif qtde_antes is not None and qtde_depois > qtde_antes:
            db.registrar_movimentacao(item_id, "ajuste", str(qtde_depois - qtde_antes),
                                       session.get("username"), "Quantidade aumentada manualmente")
        else:
            db.registrar_movimentacao(item_id, "edicao", None, session.get("username"), "Dados do item editados")
    else:
        db.registrar_movimentacao(item_id, "edicao", None, session.get("username"), "Dados do item editados")

    return jsonify({"ok": True})


@app.route("/api/itens/<int:item_id>", methods=["DELETE"])
@edit_required
def api_excluir(item_id):
    item = db.buscar_item_por_id(item_id)
    if not item:
        return jsonify({"erro": "Item não encontrado."}), 404
    db.registrar_movimentacao(item_id, "exclusao", item.get("qtde"), session.get("username"),
                               f"Item {item.get('codigo')} excluído")
    db.excluir_item(item_id)
    return jsonify({"ok": True, "item": item})


@app.route("/api/itens/restaurar", methods=["POST"])
@edit_required
def api_restaurar():
    dados = request.get_json(force=True)
    if not dados or not dados.get("id"):
        return jsonify({"erro": "Dados inválidos para restaurar."}), 400
    if db.buscar_item_por_id(dados["id"]):
        return jsonify({"erro": "Este item já existe (não foi excluído ou já foi restaurado)."}), 400
    db.recriar_item(dados)
    db.registrar_movimentacao(dados["id"], "restauracao", dados.get("qtde"), session.get("username"),
                               "Exclusão desfeita")
    return jsonify({"ok": True})


@app.route("/api/itens/excluir-em-lote", methods=["POST"])
@edit_required
def api_excluir_em_lote():
    dados = request.get_json(force=True)
    ids = dados.get("ids") or []
    if not ids:
        return jsonify({"erro": "Nenhum item selecionado."}), 400
    for item_id in ids:
        item = db.buscar_item_por_id(item_id)
        if item:
            db.registrar_movimentacao(item_id, "exclusao", item.get("qtde"), session.get("username"),
                                       f"Item {item.get('codigo')} excluído (exclusão em massa)")
    total = db.excluir_itens_em_lote(ids)
    return jsonify({"ok": True, "excluidos": total})


@app.route("/api/itens/<int:item_id>/movimentacoes")
@login_required
def api_movimentacoes(item_id):
    return jsonify(db.listar_movimentacoes(item_id, tabela="itens"))


@app.route("/export")
@login_required
def exportar_excel():
    itens = db.listar_itens()
    wb = Workbook()
    ws = wb.active
    ws.title = "Estoque"
    colunas = ["ID", "Codigo do item", "Descricao", "Qtde", "Localizacao",
               "NF de entrada", "Data de entrada", "NF de saida",
               "Data de saida", "VD / referencia", "Filial destino", "Local",
               "Armazenagem", "Status", "Nro Imobilizado", "Nro Serie",
               "Nro Patrimonio", "Tipo de Estoque", "Criado por",
               "Ultima alteracao por", "Ultima alteracao em",
               "Pedido", "ValAquis.", "Chamado"]
    ws.append(colunas)
    for it in itens:
        ws.append([
            it["id"], it["codigo"], it["descricao"], it["qtde"], it["localizacao"],
            it["nf_entrada"], it["data_entrada"], it["nf_saida"],
            it["data_saida"], it["vd_loja"], it.get("filial_destino"), it.get("local"),
            it.get("armazenagem"), it.get("status"), it.get("nro_imobilizado"),
            it.get("nro_serie"), it.get("nro_patrimonio"), it.get("tipo_estoque"),
            it.get("criado_por"), it.get("atualizado_por"), it.get("atualizado_em"),
            it.get("pedido"), it.get("val_aquis"), it.get("chamado"),
        ])
    larguras = [8, 18, 32, 8, 18, 18, 16, 18, 16, 18, 22, 12, 14, 12, 16, 16, 16, 18, 14, 16, 16, 14, 12, 14]
    for i, largura in enumerate(larguras, start=1):
        ws.column_dimensions[chr(64 + i)].width = largura

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)
    nome_arquivo = f"estoque_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return send_file(
        buffer,
        as_attachment=True,
        download_name=nome_arquivo,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ---------------------------------------------------------------------
# Importação de planilha Excel
# ---------------------------------------------------------------------

def _normalizar(texto):
    """Deixa o texto minúsculo, sem acento e sem espaços/pontuação, para
    comparar nomes de coluna de forma tolerante (ex: 'Nº Patrimônio' == 'nro patrimonio')."""
    if texto is None:
        return ""
    texto = str(texto).strip().lower()
    texto = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    texto = "".join(c for c in texto if c.isalnum())
    return texto


TIPOS_ESTOQUE_CANONICOS = ["Expansão", "Sustentação", "Requalificação", "Ampliação", "Realocação", "Reversa"]


def _canonicalizar_tipo_estoque(valor):
    """Se o valor bater (ignorando acento/maiúscula) com um dos tipos de estoque
    oficiais, devolve a grafia oficial. Caso contrário, devolve o texto original."""
    if not valor:
        return valor
    norm = _normalizar(valor)
    for tipo in TIPOS_ESTOQUE_CANONICOS:
        if _normalizar(tipo) == norm:
            return tipo
    return valor


# Cada campo do sistema aceita várias variações possíveis de nome de coluna
# na planilha (já normalizadas: sem acento, sem espaço, minúsculo).
ALIASES_COLUNAS = {
    "codigo": ["codigo", "codigodoitem"],
    "descricao": ["descricao", "descricaodoequipamento"],
    "qtde": ["qtde", "quantidade", "qtd"],
    "localizacao": ["localizacao"],
    "nf_entrada": ["nfdeentrada", "nfentrada", "notafiscaldeentrada", "nf"],
    "data_entrada": ["datadeentrada", "dataentrada"],
    "nf_saida": ["nfdesaida", "nfsaida", "notafiscaldesaida"],
    "data_saida": ["datadesaida", "datasaida"],
    "vd_loja": ["vddalojadestino", "vddaloja", "vdloja", "vd", "lojadestino"],
    "filial_destino": ["filialdestino", "filial", "codigofilial", "lojafilial", "destinofilial"],
    "local": ["local"],
    "armazenagem": ["armazenagem", "localarmazenagem"],
    "status": ["status"],
    "nro_imobilizado": ["nroimobilizado", "numeroimobilizado", "imobilizado"],
    "nro_serie": ["nroserie", "numerodeserie", "nserie", "serie"],
    "nro_patrimonio": ["nropatrimonio", "numeropatrimonio", "patrimonio"],
    "tipo_estoque": ["tipodeestoque", "tipoestoque"],
    "pedido": ["pedido"],
    "val_aquis": ["valaquis", "valoraquisicao", "valordeaquisicao"],
    "chamado": ["chamado"],
}


def _mapear_colunas(linha_cabecalho):
    """Recebe a primeira linha da planilha (os títulos das colunas) e devolve
    um dicionário {indice_da_coluna: campo_do_sistema}."""
    mapa = {}
    for indice, titulo in enumerate(linha_cabecalho):
        normalizado = _normalizar(titulo)
        for campo, apelidos in ALIASES_COLUNAS.items():
            if normalizado in apelidos:
                mapa[indice] = campo
                break
    return mapa


def _valor_para_texto(valor):
    """Converte o valor de uma célula do Excel (que pode vir como data,
    número, etc.) para texto simples, do jeito que o sistema espera."""
    if valor is None:
        return ""
    if isinstance(valor, datetime):
        return valor.strftime("%Y-%m-%d")
    return str(valor).strip()


@app.route("/api/itens/importar", methods=["POST"])
@edit_required
def api_importar():
    senha = request.form.get("senha", "")
    usuario_atual = db.buscar_usuario_por_id(session["user_id"])
    if not usuario_atual or not check_password_hash(usuario_atual["password_hash"], senha):
        return jsonify({"erro": "Senha incorreta."}), 403

    tabela_destino = request.form.get("tabela", "estoque")
    if tabela_destino not in ("estoque", "imobilizados"):
        tabela_destino = "estoque"

    arquivo = request.files.get("arquivo")
    if not arquivo or not arquivo.filename:
        return jsonify({"erro": "Nenhum arquivo enviado."}), 400
    if not arquivo.filename.lower().endswith((".xlsx", ".xlsm")):
        return jsonify({"erro": "Envie um arquivo Excel (.xlsx)."}), 400

    try:
        try:
            wb = load_workbook(arquivo, read_only=True, data_only=True)
            ws = wb.active
        except Exception:
            return jsonify({"erro": "Não consegui abrir esse arquivo. Confirme se é um .xlsx válido."}), 400

        if ws is None:
            return jsonify({"erro": "A planilha não tem nenhuma aba com dados."}), 400

        linhas = ws.iter_rows(values_only=True)
        try:
            cabecalho = next(linhas)
        except StopIteration:
            return jsonify({"erro": "A planilha está vazia."}), 400

        mapa_colunas = _mapear_colunas(cabecalho)
        if "codigo" not in mapa_colunas.values():
            return jsonify({"erro": "Não encontrei uma coluna de 'Código do item' na planilha. "
                                     "Verifique se a primeira linha tem os títulos das colunas."}), 400

        usuario = session.get("username")
        novos_itens = []
        ignoradas = 0

        for linha in linhas:
            if linha is None or all(v is None for v in linha):
                continue
            dados = {}
            for indice, campo in mapa_colunas.items():
                if indice < len(linha):
                    dados[campo] = _valor_para_texto(linha[indice])
            if not dados.get("codigo"):
                ignoradas += 1
                continue
            if dados.get("tipo_estoque"):
                dados["tipo_estoque"] = _canonicalizar_tipo_estoque(dados["tipo_estoque"])
            dados["criado_por"] = usuario
            if not dados.get("data_entrada"):
                dados["data_entrada"] = datetime.now().strftime("%Y-%m-%d")

            if tabela_destino == "estoque":
                # Cada unidade da planilha vira uma linha própria no Estoque
                # (ex: qtde 40 numa linha da planilha = 40 linhas no sistema).
                try:
                    qtde_linha = int(float(dados.get("qtde") or 1))
                except (ValueError, TypeError):
                    qtde_linha = 1
                qtde_linha = max(qtde_linha, 1)
                base = dict(dados)
                base["qtde"] = "1"
                novos_itens.extend(dict(base) for _ in range(qtde_linha))
            else:
                # Imobilizado: mantém a quantidade exatamente como veio na planilha,
                # numa única linha (não é dividido).
                novos_itens.append(dados)

        if not novos_itens:
            return jsonify({"erro": "Nenhuma linha válida encontrada (confira se a coluna 'Código do item' está preenchida)."}), 400

        if tabela_destino == "estoque":
            total = db.criar_itens_em_lote(
                novos_itens, usuario,
                observacao=f"Importado via planilha ({arquivo.filename})"
            )
        else:
            total = db.criar_imobilizados_em_lote(
                novos_itens, usuario,
                observacao=f"Importado via planilha ({arquivo.filename})"
            )

        return jsonify({"ok": True, "importados": total, "ignoradas": ignoradas, "tabela": tabela_destino})

    except Exception as e:
        print(f"[erro] Falha ao importar planilha: {e}")
        return jsonify({"erro": f"Erro ao processar a planilha: {e}"}), 500


# ---------------------------------------------------------------------
# API - Usuários (só admin)
# ---------------------------------------------------------------------

@app.route("/api/usuarios", methods=["POST"])
@admin_required
def api_criar_usuario():
    dados = request.get_json(force=True)
    username = (dados.get("username") or "").strip()
    password = dados.get("password") or ""
    role = dados.get("role") if dados.get("role") in ("admin", "gestor", "operador", "consulta", "user") else "operador"

    if not username or not password:
        return jsonify({"erro": "Usuário e senha são obrigatórios."}), 400
    if len(password) < 6:
        return jsonify({"erro": "A senha precisa ter pelo menos 6 caracteres."}), 400
    if db.buscar_usuario_por_username(username):
        return jsonify({"erro": "Já existe um usuário com esse nome."}), 400

    db.criar_usuario(username, password, role)
    return jsonify({"ok": True}), 201


@app.route("/api/usuarios/<int:user_id>/forcar-troca-senha", methods=["POST"])
@admin_required
def api_forcar_troca_senha(user_id):
    alvo = db.buscar_usuario_por_id(user_id)
    if not alvo:
        return jsonify({"erro": "Usuário não encontrado."}), 404
    db.forcar_troca_senha(user_id)
    return jsonify({"ok": True})


@app.route("/api/usuarios/<int:user_id>", methods=["DELETE"])
@admin_required
def api_excluir_usuario(user_id):
    if user_id == session.get("user_id"):
        return jsonify({"erro": "Você não pode excluir o próprio usuário enquanto está logado com ele."}), 400

    alvo = db.buscar_usuario_por_id(user_id)
    if alvo and alvo["role"] == "admin" and db.contar_admins() <= 1:
        return jsonify({"erro": "Precisa existir pelo menos um administrador."}), 400

    ok = db.excluir_usuario(user_id)
    if not ok:
        return jsonify({"erro": "Usuário não encontrado."}), 404
    return jsonify({"ok": True})


# ---------------------------------------------------------------------
# Inicialização / execução
# ---------------------------------------------------------------------

db.init_db()


def descobrir_ip_local():
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "SEU-IP-LOCAL"


if __name__ == "__main__":
    ip = descobrir_ip_local()
    print("=" * 60)
    print("Acesse neste computador em:  http://localhost:5000")
    print(f"Outras pessoas na mesma rede acessam em:  http://{ip}:5000")
    print("=" * 60)

    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=5000)
    except ImportError:
        print("\n[Aviso] 'waitress' não instalado — rodando com o servidor")
        print("de desenvolvimento do Flask.\n")
        app.run(host="0.0.0.0", port=5000, debug=False)
