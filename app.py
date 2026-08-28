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
from datetime import datetime
from functools import wraps

from flask import (
    Flask, request, jsonify, render_template, redirect,
    url_for, session, send_file, flash,
)
from werkzeug.security import check_password_hash
from openpyxl import Workbook, load_workbook

import db

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "troque-esta-chave-em-producao")


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


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html", erro=None)

    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    usuario = db.buscar_usuario_por_username(username)

    if not usuario or not check_password_hash(usuario["password_hash"], password):
        return render_template("login.html", erro="Usuário ou senha inválidos.")

    session["user_id"] = usuario["id"]
    session["username"] = usuario["username"]
    session["role"] = usuario["role"]
    session["precisa_trocar_senha"] = usuario.get("precisa_trocar_senha") == "1"

    if session["precisa_trocar_senha"]:
        return redirect(url_for("trocar_senha"))
    proximo = request.args.get("proximo") or url_for("index")
    return redirect(proximo)


@app.route("/trocar-senha", methods=["GET", "POST"])
@login_required
def trocar_senha():
    if not session.get("precisa_trocar_senha"):
        return redirect(url_for("index"))

    if request.method == "GET":
        return render_template("trocar_senha.html", username=session.get("username"), erro=None)

    nova = request.form.get("nova_senha", "")
    confirmar = request.form.get("confirmar_senha", "")

    if len(nova) < 6:
        return render_template("trocar_senha.html", username=session.get("username"),
                                erro="A senha precisa ter pelo menos 6 caracteres.")
    if nova != confirmar:
        return render_template("trocar_senha.html", username=session.get("username"),
                                erro="As senhas não conferem.")

    db.trocar_senha(session["user_id"], nova)
    session["precisa_trocar_senha"] = False
    return redirect(url_for("index"))


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
        is_admin=session.get("role") == "admin",
    )


@app.route("/usuarios")
@admin_required
def pagina_usuarios():
    return render_template(
        "usuarios.html",
        username=session.get("username"),
        usuarios=db.listar_usuarios(),
    )


@app.route("/cadastro-itens")
@login_required
def pagina_cadastro_itens():
    return render_template(
        "cadastro_itens.html",
        username=session.get("username"),
        is_admin=session.get("role") == "admin",
    )


# ---------------------------------------------------------------------
# API - Cadastro mestre de itens
# ---------------------------------------------------------------------

@app.route("/api/cadastro-itens", methods=["GET"])
@login_required
def api_listar_cadastro_itens():
    # O cadastro mestre é único: todo item cadastrado fica disponível
    # tanto no Estoque quanto nos Imobilizados.
    return jsonify(db.listar_cadastro_itens())


@app.route("/api/cadastro-itens", methods=["POST"])
@login_required
def api_criar_cadastro_item():
    dados = request.get_json(force=True)
    codigo = (dados.get("codigo") or "").strip()
    descricao = (dados.get("descricao") or "").strip()
    unidade = (dados.get("unidade") or "UN").strip()
    if not codigo:
        return jsonify({"erro": "Código do item é obrigatório."}), 400
    if not descricao:
        return jsonify({"erro": "Descrição do item é obrigatória."}), 400
    if db.buscar_cadastro_item_por_codigo(codigo):
        return jsonify({"erro": "Já existe um item cadastrado com este código."}), 400

    novo = db.criar_cadastro_item({
        "codigo": codigo,
        "descricao": descricao,
        "unidade": unidade,
        "tipo": "estoque",
        "criado_por": session.get("username"),
    })
    return jsonify(novo), 201


@app.route("/api/cadastro-itens/<int:item_id>", methods=["PUT"])
@login_required
def api_atualizar_cadastro_item(item_id):
    dados = request.get_json(force=True)
    codigo = (dados.get("codigo") or "").strip()
    descricao = (dados.get("descricao") or "").strip()
    unidade = (dados.get("unidade") or "UN").strip()
    codigo_anterior = db.buscar_cadastro_item_por_id(item_id)
    if not codigo or not descricao:
        return jsonify({"erro": "Código e descrição são obrigatórios."}), 400
    existente = db.buscar_cadastro_item_por_codigo(codigo)
    if existente and existente["id"] != item_id:
        return jsonify({"erro": "Já existe outro item com este código."}), 400
    ok = db.atualizar_cadastro_item(item_id, {
        "codigo": codigo, "descricao": descricao, "unidade": unidade,
        "codigo_anterior": (codigo_anterior or {}).get("codigo", codigo),
        "atualizado_por": session.get("username"),
        "atualizado_em": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    if not ok:
        return jsonify({"erro": "Item de cadastro não encontrado."}), 404
    return jsonify({"ok": True})


@app.route("/api/cadastro-itens/<int:item_id>", methods=["DELETE"])
@login_required
def api_excluir_cadastro_item(item_id):
    item = db.buscar_cadastro_item_por_id(item_id)
    if not item:
        return jsonify({"erro": "Item de cadastro não encontrado."}), 404
    # A exclusão remove o cadastro mestre, mas preserva as linhas já lançadas
    # no Estoque/Imobilizados para não apagar histórico operacional.
    db.excluir_cadastro_item(item_id)
    return jsonify({"ok": True})


@app.route("/imobilizados")
@login_required
def pagina_imobilizados():
    return render_template(
        "imobilizados.html",
        username=session.get("username"),
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
@login_required
def api_criar_imobilizado():
    dados = request.get_json(force=True)
    codigo = (dados.get("codigo") or "").strip()
    if not codigo:
        return jsonify({"erro": "Código do item é obrigatório."}), 400
    cadastro = db.buscar_cadastro_item_por_codigo(codigo)
    if not cadastro:
        return jsonify({"erro": "Cadastre o código do item antes de lançar no sistema."}), 400
    novo = {
        "codigo": codigo,
        "descricao": cadastro.get("descricao", ""),
        "qtde": dados.get("qtde", ""),
        "localizacao": (dados.get("localizacao") or "").strip(),
        "nf_entrada": (dados.get("nf_entrada") or "").strip(),
        "data_entrada": dados.get("data_entrada") or datetime.now().strftime("%Y-%m-%d"),
        "nf_saida": (dados.get("nf_saida") or "").strip(),
        "data_saida": (dados.get("data_saida") or "").strip(),
        "vd_loja": (dados.get("vd_loja") or "").strip(),
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

    # Cada unidade do Imobilizado também ocupa uma linha própria.
    # Ex.: quantidade 2 => duas linhas, ambas com qtde=1.
    linhas = [dict(novo, qtde="1") for _ in range(qtde_informada)]
    total = db.criar_imobilizados_em_lote(
        linhas, session.get("username"), observacao="Cadastro manual do imobilizado"
    )
    return jsonify({"ok": True, "criados": total, "codigo": codigo}), 201


@app.route("/api/imobilizados/<int:item_id>", methods=["PUT"])
@login_required
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
@login_required
def api_excluir_imobilizado(item_id):
    item = db.buscar_imobilizado_por_id(item_id)
    if not item:
        return jsonify({"erro": "Item não encontrado."}), 404
    db.registrar_movimentacao(item_id, "exclusao", item.get("qtde"), session.get("username"),
                               f"Imobilizado {item.get('codigo')} excluído", tabela="imobilizados")
    db.excluir_imobilizado(item_id)
    return jsonify({"ok": True, "item": item})


@app.route("/api/imobilizados/restaurar", methods=["POST"])
@login_required
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
@login_required
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
@login_required
def api_enviar_estoque(item_id):
    total = db.enviar_imobilizado_para_estoque(item_id, session.get("username"))
    if total is None:
        return jsonify({"erro": "Imobilizado não encontrado."}), 404
    return jsonify({"ok": True, "criados_no_estoque": total})


@app.route("/api/imobilizados/enviar-estoque-em-lote", methods=["POST"])
@login_required
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
               "Data de saida", "VD da loja (destino)", "Local",
               "Armazenagem", "Status", "Nro Imobilizado", "Nro Serie",
               "Nro Patrimonio", "Tipo de Estoque", "Criado por",
               "Ultima alteracao por", "Ultima alteracao em",
               "Pedido", "ValAquis.", "Chamado"]
    ws.append(colunas)
    for it in itens:
        ws.append([
            it["id"], it["codigo"], it["descricao"], it["qtde"], it["localizacao"],
            it["nf_entrada"], it["data_entrada"], it["nf_saida"],
            it["data_saida"], it["vd_loja"], it.get("local"),
            it.get("armazenagem"), it.get("status"), it.get("nro_imobilizado"),
            it.get("nro_serie"), it.get("nro_patrimonio"), it.get("tipo_estoque"),
            it.get("criado_por"), it.get("atualizado_por"), it.get("atualizado_em"),
            it.get("pedido"), it.get("val_aquis"), it.get("chamado"),
        ])
    larguras = [8, 18, 32, 8, 18, 18, 16, 18, 16, 22, 12, 14, 12, 16, 16, 16, 18, 14, 16, 16, 14, 12, 14]
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


# ---------------------------------------------------------------------
# API - Itens
# ---------------------------------------------------------------------

@app.route("/api/itens", methods=["GET"])
@login_required
def api_listar():
    return jsonify(db.listar_itens())


@app.route("/api/itens", methods=["POST"])
@login_required
def api_criar():
    dados = request.get_json(force=True)
    codigo = (dados.get("codigo") or "").strip()
    if not codigo:
        return jsonify({"erro": "Código do item é obrigatório."}), 400
    cadastro = db.buscar_cadastro_item_por_codigo(codigo)
    if not cadastro:
        return jsonify({"erro": "Cadastre o código do item antes de lançar no sistema."}), 400
    base = {
        "codigo": codigo,
        "descricao": cadastro.get("descricao", ""),
        "qtde": "1",
        "localizacao": (dados.get("localizacao") or "").strip(),
        "nf_entrada": (dados.get("nf_entrada") or "").strip(),
        "data_entrada": dados.get("data_entrada") or datetime.now().strftime("%Y-%m-%d"),
        "nf_saida": (dados.get("nf_saida") or "").strip(),
        "data_saida": (dados.get("data_saida") or "").strip(),
        "vd_loja": (dados.get("vd_loja") or "").strip(),
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
@login_required
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
                obs = f"Saída registrada (NF {dados.get('nf_saida')}, destino: {dados.get('vd_loja') or '-'})"
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
@login_required
def api_excluir(item_id):
    item = db.buscar_item_por_id(item_id)
    if not item:
        return jsonify({"erro": "Item não encontrado."}), 404
    db.registrar_movimentacao(item_id, "exclusao", item.get("qtde"), session.get("username"),
                               f"Item {item.get('codigo')} excluído")
    db.excluir_item(item_id)
    return jsonify({"ok": True, "item": item})


@app.route("/api/itens/restaurar", methods=["POST"])
@login_required
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
@login_required
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
               "Data de saida", "VD da loja (destino)", "Local",
               "Armazenagem", "Status", "Nro Imobilizado", "Nro Serie",
               "Nro Patrimonio", "Tipo de Estoque", "Criado por",
               "Ultima alteracao por", "Ultima alteracao em",
               "Pedido", "ValAquis.", "Chamado"]
    ws.append(colunas)
    for it in itens:
        ws.append([
            it["id"], it["codigo"], it["descricao"], it["qtde"], it["localizacao"],
            it["nf_entrada"], it["data_entrada"], it["nf_saida"],
            it["data_saida"], it["vd_loja"], it.get("local"),
            it.get("armazenagem"), it.get("status"), it.get("nro_imobilizado"),
            it.get("nro_serie"), it.get("nro_patrimonio"), it.get("tipo_estoque"),
            it.get("criado_por"), it.get("atualizado_por"), it.get("atualizado_em"),
            it.get("pedido"), it.get("val_aquis"), it.get("chamado"),
        ])
    larguras = [8, 18, 32, 8, 18, 18, 16, 18, 16, 22, 12, 14, 12, 16, 16, 16, 18, 14, 16, 16, 14, 12, 14]
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
@login_required
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
                # Imobilizado: cada unidade também vira uma linha própria.
                try:
                    qtde_linha = int(float(dados.get("qtde") or 1))
                except (ValueError, TypeError):
                    qtde_linha = 1
                qtde_linha = max(qtde_linha, 1)
                base = dict(dados)
                base["qtde"] = "1"
                novos_itens.extend(dict(base) for _ in range(qtde_linha))

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
    role = dados.get("role") if dados.get("role") in ("admin", "user") else "user"

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
