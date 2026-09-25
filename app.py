"""
Controle de Estoque - Entrada e Saída de Equipamentos
------------------------------------------------------
Programa em Python (Flask) com interface web (HTTP).

- Localmente: guarda os dados num banco SQLite (estoque.db), sem
  precisar configurar nada.
- Em produção: usa PostgreSQL externo através da variável DATABASE_URL.
  A configuração recomendada é Koyeb para a aplicação + Neon para o banco.

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
import re
import unicodedata
import zipfile
import secrets
import hmac
import time
import base64
import hashlib
import struct
import json
import urllib.request as urlrequest
import urllib.error as urlerror
import smtplib
import ssl
import threading
from email.message import EmailMessage
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps

from flask import (
    Flask, request, jsonify, render_template, redirect,
    url_for, session, send_file, flash,
)
from werkzeug.security import check_password_hash
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.chart import BarChart, Reference
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
from reportlab.graphics.shapes import Drawing, String
from reportlab.graphics.charts.barcharts import VerticalBarChart
from cryptography.fernet import Fernet, InvalidToken
import qrcode

import db

app = Flask(__name__)
APP_BUILD = "2026-09-25-relatorio-executivo-grafico-v121"
_DASHBOARD_CACHE = {"expira": 0.0, "dados": None}
app.secret_key = os.environ.get("SECRET_KEY", "troque-esta-chave-em-producao")
_RUNNING_HTTPS_HOSTED = bool(
    os.environ.get("KOYEB_PUBLIC_DOMAIN")
    or os.environ.get("RENDER_EXTERNAL_HOSTNAME")
)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Koyeb publica o serviço em HTTPS. Localmente permanece False por padrão.
    SESSION_COOKIE_SECURE=os.environ.get(
        "SESSION_COOKIE_SECURE", "1" if _RUNNING_HTTPS_HOSTED else "0"
    ) == "1",
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
    recebido = request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", "")
    esperado = session.get("_csrf_token", "")
    return bool(recebido and esperado and hmac.compare_digest(recebido, esperado))

@app.context_processor
def _inject_security_helpers():
    return {"csrf_token": _csrf_token()}


# ---------------------------------------------------------------------
# MFA / TOTP — Microsoft Authenticator e Google Authenticator
# ---------------------------------------------------------------------

MFA_ISSUER = os.environ.get("MFA_ISSUER", "Controle de Estoque")
MFA_ATTEMPTS = {}
MFA_MAX_FAILURES = 5
MFA_WINDOW_SECONDS = 5 * 60


def _mfa_cipher():
    material = os.environ.get("MFA_ENCRYPTION_KEY") or app.secret_key
    chave = base64.urlsafe_b64encode(hashlib.sha256(material.encode("utf-8")).digest())
    return Fernet(chave)


def _protect_mfa_secret(secret):
    return _mfa_cipher().encrypt(secret.encode("utf-8")).decode("ascii")


def _unprotect_mfa_secret(token):
    if not token:
        return None
    try:
        return _mfa_cipher().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError):
        return None


def _base32_secret():
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def _totp_code(secret, timestamp=None, interval=30, digits=6):
    timestamp = time.time() if timestamp is None else timestamp
    contador = int(timestamp // interval)
    padded = secret + "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode(padded.upper(), casefold=True)
    digest = hmac.new(key, struct.pack(">Q", contador), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    valor = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(valor % (10 ** digits)).zfill(digits)


def _verify_totp(secret, codigo, window=1):
    codigo = "".join(ch for ch in str(codigo or "") if ch.isdigit())
    if len(codigo) != 6 or not secret:
        return False
    agora = time.time()
    return any(hmac.compare_digest(_totp_code(secret, agora + passo * 30), codigo) for passo in range(-window, window + 1))


def _mfa_uri(username, secret):
    from urllib.parse import quote
    label = quote(f"{MFA_ISSUER}:{username}")
    issuer = quote(MFA_ISSUER)
    return f"otpauth://totp/{label}?secret={secret}&issuer={issuer}&algorithm=SHA1&digits=6&period=30"


def _qr_data_uri(texto):
    qr = qrcode.QRCode(version=None, error_correction=qrcode.constants.ERROR_CORRECT_M, box_size=7, border=3)
    qr.add_data(texto)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _gerar_codigos_recuperacao(qtd=8):
    alfabeto = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    codigos = []
    for _ in range(qtd):
        bruto = "".join(secrets.choice(alfabeto) for _ in range(10))
        codigos.append(bruto[:5] + "-" + bruto[5:])
    return codigos


def _hash_recovery_code(codigo):
    normalizado = "".join(ch for ch in str(codigo or "").upper() if ch.isalnum())
    return hashlib.sha256(normalizado.encode("utf-8")).hexdigest()


def _consume_recovery_code(usuario, codigo):
    if not usuario:
        return False
    try:
        hashes = json.loads(usuario.get("mfa_recovery_codes") or "[]")
    except Exception:
        hashes = []
    alvo = _hash_recovery_code(codigo)
    if alvo not in hashes:
        return False
    hashes.remove(alvo)
    db.atualizar_codigos_recuperacao_mfa(usuario["id"], json.dumps(hashes))
    return True


def _mfa_key(username):
    return f"{_client_ip()}|{(username or '').strip().lower()}"


def _mfa_prune(key):
    agora = time.time()
    tentativas = [t for t in MFA_ATTEMPTS.get(key, []) if agora - t < MFA_WINDOW_SECONDS]
    if tentativas:
        MFA_ATTEMPTS[key] = tentativas
    else:
        MFA_ATTEMPTS.pop(key, None)
    return tentativas


def _mfa_wait_seconds(username):
    tentativas = _mfa_prune(_mfa_key(username))
    if len(tentativas) < MFA_MAX_FAILURES:
        return 0
    return max(1, int(MFA_WINDOW_SECONDS - (time.time() - tentativas[0])))


def _mfa_register_failure(username):
    key = _mfa_key(username)
    tentativas = _mfa_prune(key)
    tentativas.append(time.time())
    MFA_ATTEMPTS[key] = tentativas


def _mfa_clear_failures(username):
    MFA_ATTEMPTS.pop(_mfa_key(username), None)


def _set_pending_login(usuario, proximo):
    csrf = session.get("_csrf_token") or secrets.token_urlsafe(32)
    session.clear()
    session["_csrf_token"] = csrf
    session["pending_user_id"] = usuario["id"]
    session["pending_username"] = usuario["username"]
    session["pending_role"] = usuario["role"]
    session["pending_precisa_trocar_senha"] = usuario.get("precisa_trocar_senha") == "1"
    session["pending_next"] = proximo or url_for("dashboard")


def _start_password_change_before_mfa(usuario, proximo):
    """Cria uma sessão restrita apenas à troca da senha no primeiro acesso.

    A senha já foi validada, mas o login ainda não é considerado concluído
    enquanto o usuário não criar a senha pessoal e finalizar o MFA.
    """
    csrf = session.get("_csrf_token") or secrets.token_urlsafe(32)
    session.clear()
    session["_csrf_token"] = csrf
    session["user_id"] = usuario["id"]
    session["username"] = usuario["username"]
    session["role"] = usuario["role"]
    session["precisa_trocar_senha"] = True
    session["primeiro_acesso_mfa_pendente"] = True
    session["primeiro_acesso_next"] = proximo or url_for("dashboard")


def _finalize_login(usuario=None):
    if usuario is None:
        user_id = session.get("pending_user_id")
        usuario = db.buscar_usuario_por_id(user_id) if user_id else None
    if not usuario:
        session.clear()
        return redirect(url_for("login"))
    precisa_trocar = usuario.get("precisa_trocar_senha") == "1"
    proximo = session.get("pending_next") or url_for("dashboard")
    session.clear()
    session["_csrf_token"] = secrets.token_urlsafe(32)
    session["user_id"] = usuario["id"]
    session["username"] = usuario["username"]
    session["role"] = usuario["role"]
    session["precisa_trocar_senha"] = precisa_trocar
    db.registrar_evento_login(usuario["username"], _client_ip(), "sucesso", "login concluído com MFA" if usuario.get("mfa_enabled") == "1" else "login realizado")
    if precisa_trocar:
        return redirect(url_for("trocar_senha"))
    return redirect(proximo)


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


def admin_page_required(view):
    """Proteção para páginas administrativas com retorno amigável ao usuário."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login", proximo=request.path))
        if session.get("precisa_trocar_senha"):
            return redirect(url_for("trocar_senha"))
        if session.get("role") != "admin":
            return redirect(url_for("dashboard", acesso_admin="1"))
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


def page_role_required(*roles):
    permitidos=set(roles)
    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not session.get("user_id"):
                return redirect(url_for("login", proximo=request.path))
            if session.get("precisa_trocar_senha"):
                return redirect(url_for("trocar_senha"))
            role=session.get("role") or "user"
            if role == "user":
                role = "operador"
            if role not in permitidos:
                return redirect(url_for("dashboard", acesso_negado="1"))
            return view(*args, **kwargs)
        return wrapped
    return decorator


def edit_required(view):
    return role_required("admin", "gestor", "operador")(view)


def manager_required(view):
    # Gestor e Operador possuem as mesmas permissões de edição/inclusão
    # nas áreas operacionais. O perfil Consulta permanece somente leitura.
    return role_required("admin", "gestor", "operador")(view)


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
    proximo = request.args.get("proximo") or url_for("dashboard")

    # Primeiro acesso:
    # senha temporária -> criação da senha pessoal -> configuração/validação do MFA -> sistema.
    if usuario.get("precisa_trocar_senha") == "1":
        _start_password_change_before_mfa(usuario, proximo)
        db.registrar_evento_login(
            usuario["username"],
            _client_ip(),
            "primeiro_acesso_senha_pendente",
            "senha temporária validada; aguardando criação da senha pessoal antes do MFA",
        )
        return redirect(url_for("trocar_senha"))

    # Demais acessos: MFA é obrigatório para todos os perfis.
    _set_pending_login(usuario, proximo)
    if usuario.get("mfa_enabled") == "1":
        db.registrar_evento_login(usuario["username"], _client_ip(), "mfa_pendente", "senha validada; aguardando segundo fator")
        return redirect(url_for("mfa_verificar"))
    db.registrar_evento_login(usuario["username"], _client_ip(), "mfa_configuracao_exigida", "usuário deve ativar MFA obrigatório")
    return redirect(url_for("mfa_configurar"))


@app.route("/mfa/verificar", methods=["GET", "POST"])
def mfa_verificar():
    user_id = session.get("pending_user_id")
    if not user_id:
        return redirect(url_for("login"))
    usuario = db.buscar_usuario_por_id(user_id)
    if not usuario:
        session.clear()
        return redirect(url_for("login"))
    if usuario.get("mfa_enabled") != "1":
        return redirect(url_for("mfa_configurar"))

    erro = None
    if request.method == "POST":
        if not _csrf_ok():
            erro = "A sessão de segurança expirou. Atualize a página e tente novamente."
            return render_template("mfa_verificar.html", username=usuario["username"], erro=erro), 400

        espera = _mfa_wait_seconds(usuario["username"])
        if espera > 0:
            minutos = max(1, (espera + 59) // 60)
            db.registrar_evento_login(usuario["username"], _client_ip(), "mfa_bloqueado", "limite de tentativas MFA excedido")
            erro = f"Muitas tentativas de MFA. Aguarde aproximadamente {minutos} minuto(s)."
            return render_template("mfa_verificar.html", username=usuario["username"], erro=erro), 429

        codigo = (request.form.get("codigo") or "").strip()
        secret = _unprotect_mfa_secret(usuario.get("mfa_secret"))
        totp_ok = _verify_totp(secret, codigo) if secret else False
        recovery_ok = False
        if not totp_ok and len("".join(ch for ch in codigo if ch.isalnum())) >= 8:
            recovery_ok = _consume_recovery_code(usuario, codigo)

        if not (totp_ok or recovery_ok):
            _mfa_register_failure(usuario["username"])
            db.registrar_evento_login(usuario["username"], _client_ip(), "mfa_falha", "código MFA inválido")
            erro = "Código inválido. Informe o código de 6 dígitos do Authenticator ou um código de recuperação."
            return render_template("mfa_verificar.html", username=usuario["username"], erro=erro), 401

        _mfa_clear_failures(usuario["username"])
        db.registrar_evento_login(usuario["username"], _client_ip(), "mfa_validado", "código de recuperação utilizado" if recovery_ok else "código TOTP validado")
        return _finalize_login(usuario)

    return render_template("mfa_verificar.html", username=usuario["username"], erro=erro)


@app.route("/mfa/configurar", methods=["GET", "POST"])
def mfa_configurar():
    pending = bool(session.get("pending_user_id"))
    user_id = session.get("pending_user_id") or session.get("user_id")
    if not user_id:
        return redirect(url_for("login"))
    usuario = db.buscar_usuario_por_id(user_id)
    if not usuario:
        session.clear()
        return redirect(url_for("login"))

    # Todos os perfis entram por aqui obrigatoriamente no primeiro acesso sem MFA.
    obrigatorio = pending
    if usuario.get("mfa_enabled") == "1":
        return redirect(url_for("mfa_verificar") if pending else url_for("pagina_seguranca"))

    setup_key = f"mfa_setup_secret_{user_id}"
    secret = session.get(setup_key)
    if not secret:
        secret = _base32_secret()
        session[setup_key] = secret
    qr_uri = _mfa_uri(usuario["username"], secret)
    qr_data = _qr_data_uri(qr_uri)
    erro = None

    if request.method == "POST":
        if not _csrf_ok():
            erro = "A sessão de segurança expirou. Atualize a página e tente novamente."
            return render_template("mfa_configurar.html", username=usuario["username"], secret=secret, qr_data=qr_data, erro=erro, obrigatorio=obrigatorio), 400
        codigo = request.form.get("codigo", "")
        if not _verify_totp(secret, codigo):
            db.registrar_evento_login(usuario["username"], _client_ip(), "mfa_config_falha", "código de confirmação inválido")
            erro = "O código não confere. Aguarde um novo código no Authenticator e tente novamente."
            return render_template("mfa_configurar.html", username=usuario["username"], secret=secret, qr_data=qr_data, erro=erro, obrigatorio=obrigatorio), 400

        codigos = _gerar_codigos_recuperacao()
        hashes = [_hash_recovery_code(c) for c in codigos]
        db.salvar_mfa_usuario(user_id, _protect_mfa_secret(secret), json.dumps(hashes))
        session.pop(setup_key, None)
        session["mfa_recovery_codes_once"] = codigos
        session["mfa_recovery_username"] = usuario["username"]
        session["mfa_setup_pending"] = pending
        db.registrar_evento_login(usuario["username"], _client_ip(), "mfa_ativado", "MFA TOTP ativado")
        return redirect(url_for("mfa_codigos_recuperacao"))

    return render_template("mfa_configurar.html", username=usuario["username"], secret=secret, qr_data=qr_data, erro=erro, obrigatorio=obrigatorio)


@app.route("/mfa/codigos-recuperacao")
def mfa_codigos_recuperacao():
    codigos = session.get("mfa_recovery_codes_once")
    if not codigos:
        if session.get("pending_user_id"):
            return redirect(url_for("mfa_verificar"))
        if session.get("user_id"):
            return redirect(url_for("pagina_seguranca"))
        return redirect(url_for("login"))
    return render_template(
        "mfa_recuperacao.html",
        username=session.get("mfa_recovery_username") or session.get("pending_username") or session.get("username"),
        codigos=codigos,
        pending=bool(session.get("mfa_setup_pending")),
    )


@app.route("/mfa/concluir", methods=["POST"])
def mfa_concluir():
    if not _csrf_ok():
        return redirect(url_for("mfa_codigos_recuperacao"))
    session.pop("mfa_recovery_codes_once", None)
    session.pop("mfa_recovery_username", None)
    pending = bool(session.pop("mfa_setup_pending", False))
    if pending and session.get("pending_user_id"):
        usuario = db.buscar_usuario_por_id(session.get("pending_user_id"))
        return _finalize_login(usuario)
    return redirect(url_for("pagina_seguranca") if session.get("user_id") else url_for("login"))


@app.route("/seguranca")
@login_required
def pagina_seguranca():
    usuario = db.buscar_usuario_por_id(session.get("user_id"))
    return render_template(
        "seguranca.html",
        username=session.get("username"),
        role=session.get("role") or "user",
        is_admin=session.get("role") == "admin",
        usuario=usuario,
    )


@app.route("/mfa/desativar", methods=["POST"])
@login_required
def mfa_desativar():
    usuario = db.buscar_usuario_por_id(session.get("user_id"))
    if not usuario or usuario.get("mfa_enabled") != "1":
        return redirect(url_for("pagina_seguranca"))
    flash("O MFA é obrigatório para todos os perfis e não pode ser desativado. Em caso de troca ou perda do aparelho, solicite a um Administrador o reset do MFA.", "erro")
    return redirect(url_for("pagina_seguranca"))


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

    user_id = session["user_id"]
    username = session.get("username")
    primeiro_acesso_mfa = bool(session.get("primeiro_acesso_mfa_pendente"))
    proximo = session.get("primeiro_acesso_next") or url_for("dashboard")

    db.trocar_senha(user_id, nova)
    db.registrar_evento_login(
        username,
        _client_ip(),
        "senha_alterada",
        "senha pessoal criada no primeiro acesso; MFA será exigido em seguida" if primeiro_acesso_mfa else "senha atualizada pelo usuário",
    )

    if primeiro_acesso_mfa:
        usuario = db.buscar_usuario_por_id(user_id)
        if not usuario:
            session.clear()
            return redirect(url_for("login"))

        # A sessão de troca de senha é encerrada e volta a ser uma sessão
        # pendente de MFA. Assim não existe caminho para o Dashboard antes
        # da conclusão do segundo fator.
        _set_pending_login(usuario, proximo)
        if usuario.get("mfa_enabled") == "1":
            db.registrar_evento_login(
                usuario["username"],
                _client_ip(),
                "mfa_pendente",
                "senha pessoal criada; aguardando validação do MFA",
            )
            return redirect(url_for("mfa_verificar"))

        db.registrar_evento_login(
            usuario["username"],
            _client_ip(),
            "mfa_configuracao_exigida",
            "senha pessoal criada; aguardando configuração do MFA obrigatório",
        )
        return redirect(url_for("mfa_configurar"))

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
def pagina_inicial():
    return redirect(url_for("dashboard"))


@app.route("/estoque")
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


@app.route("/agente-ia")
@login_required
def pagina_agente_ia():
    role = session.get("role") or "user"
    if role == "user":
        role = "operador"
    return render_template(
        "agente_ia.html",
        username=session.get("username"),
        role=role,
        is_admin=role == "admin",
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
@page_role_required("admin", "gestor", "operador")
def pagina_relatorios():
    return render_template(
        "relatorios.html",
        username=session.get("username"),
        role=session.get("role") or "user",
        is_admin=session.get("role") == "admin",
    )




def _formatar_moeda_br(valor):
    """Formata Decimal/float como moeda brasileira para relatórios."""
    try:
        numero = Decimal(str(valor or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except (InvalidOperation, ValueError, TypeError):
        numero = Decimal("0.00")
    texto = f"{numero:,.2f}"
    return "R$ " + texto.replace(",", "X").replace(".", ",").replace("X", ".")


def _calcular_relatorio_executivo_estoque_kit():
    """Visão executiva priorizando capacidade de abertura, gargalos e cenários.

    Regra principal: para cada item do Kit Padrão, calcula Estoque de Expansão
    // quantidade exigida por loja. O menor resultado determina quantas lojas
    completas podem ser abertas imediatamente. Os cenários mostram o complemento
    necessário para a próxima loja, para o pipeline atual e para a meta salva.
    """
    def qtd_num(valor):
        try:
            return max(0, int(float(valor or 0)))
        except (TypeError, ValueError):
            return 0

    itens = (db.obter_dashboard_compacto(1) or {}).get("itens") or []
    kit = db.listar_kit_padrao_loja() or []
    produtos = db.listar_produtos() or []
    meta_lojas = int(db.obter_meta_lojas_expansao() or 10)
    pipeline = _pipeline_acompanhamento_expansao()
    lojas_pendentes = len((pipeline or {}).get("pendentes") or [])

    expansao = [
        x for x in itens
        if _normalizar_exec(x.get("tipo_estoque")) == "expansao" and qtd_num(x.get("qtde")) > 0
    ]
    produtos_codigo = {
        str(p.get("codigo") or "").strip().lower(): p
        for p in produtos if str(p.get("codigo") or "").strip()
    }

    def descricoes_compativeis(a, b):
        a = _normalizar_exec(a)
        b = _normalizar_exec(b)
        if not a or not b:
            return False
        if a == b:
            return True
        if ("scanner com fio" in a and "scanner sem fio" in b) or ("scanner sem fio" in a and "scanner com fio" in b):
            return False
        return len(a) >= 6 and len(b) >= 6 and (a in b or b in a)

    def produto_para_kit(k):
        codigo = str(k.get("codigo") or "").strip().lower()
        if codigo and codigo in produtos_codigo:
            return produtos_codigo[codigo]
        desc = _normalizar_exec(k.get("descricao"))
        candidatos = []
        for prod in produtos:
            pd = _normalizar_exec(prod.get("descricao"))
            if descricoes_compativeis(desc, pd):
                candidatos.append((0 if desc == pd else abs(len(desc) - len(pd)), prod))
        return sorted(candidatos, key=lambda x: x[0])[0][1] if candidatos else None

    def estoque_disponivel(prod, k):
        codigo_kit = str(k.get("codigo") or "").strip().lower()
        descricoes = [k.get("descricao"), (prod or {}).get("descricao")]
        total = 0
        for item in expansao:
            codigo_i = str(item.get("codigo") or "").strip().lower()
            if codigo_kit:
                combina = codigo_i == codigo_kit
            else:
                combina = any(descricoes_compativeis(item.get("descricao"), d) for d in descricoes if d)
            if combina:
                total += qtd_num(item.get("qtde"))
        return total

    linhas = []
    total_unidades_expansao = sum(qtd_num(x.get("qtde")) for x in expansao)
    total_unidades_kit_estoque = 0
    valor_estoque_kit = Decimal("0.00")
    valor_kit_loja = Decimal("0.00")
    itens_sem_custo = 0

    for k in kit:
        prod = produto_para_kit(k)
        qtd_por_loja = max(1, qtd_num(k.get("quantidade")) or qtd_num((prod or {}).get("qtde_por_loja")) or 1)
        disponivel = estoque_disponivel(prod, k)
        lojas_suportadas = disponivel // qtd_por_loja

        custo = Decimal("0.00")
        custo_informado = False
        if prod:
            try:
                custo = _decimal_moeda(prod.get("custo"), "0.00")
                custo_informado = custo > 0
            except ValueError:
                custo = Decimal("0.00")
        if not custo_informado:
            itens_sem_custo += 1

        valor_estoque = (custo * disponivel).quantize(Decimal("0.01")) if custo_informado else Decimal("0.00")
        valor_kit_item = (custo * qtd_por_loja).quantize(Decimal("0.01")) if custo_informado else Decimal("0.00")
        total_unidades_kit_estoque += disponivel
        valor_estoque_kit += valor_estoque
        valor_kit_loja += valor_kit_item

        linhas.append({
            "codigo": str((prod or {}).get("codigo") or k.get("codigo") or "").strip(),
            "descricao": str((prod or {}).get("descricao") or k.get("descricao") or "").strip(),
            "estoque": disponivel,
            "qtd_por_loja": qtd_por_loja,
            "lojas_suportadas": lojas_suportadas,
            "custo": custo,
            "custo_informado": custo_informado,
            "valor_estoque": valor_estoque,
            "valor_kit_loja": valor_kit_item,
        })

    if linhas:
        capacidade = min(x["lojas_suportadas"] for x in linhas)
        limitantes = [x for x in linhas if x["lojas_suportadas"] == capacidade]
    else:
        capacidade = 0
        limitantes = []

    def calcular_alvo(alvo):
        alvo = max(0, int(alvo or 0))
        unidades_faltantes = 0
        valor_faltante = Decimal("0.00")
        faltas = []
        sem_custo_falta = 0
        for item in linhas:
            necessario = item["qtd_por_loja"] * alvo
            falta = max(0, necessario - item["estoque"])
            if falta:
                unidades_faltantes += falta
                if item["custo_informado"]:
                    valor_faltante += item["custo"] * falta
                else:
                    sem_custo_falta += 1
                faltas.append({
                    **item,
                    "necessario": necessario,
                    "falta": falta,
                    "valor_falta": (item["custo"] * falta).quantize(Decimal("0.01")) if item["custo_informado"] else Decimal("0.00"),
                })
        faltas.sort(key=lambda x: (x["lojas_suportadas"], -x["falta"], x["descricao"].lower()))
        return {
            "alvo": alvo,
            "unidades_faltantes": unidades_faltantes,
            "valor_faltante": valor_faltante.quantize(Decimal("0.01")),
            "itens_com_falta": len(faltas),
            "faltas": faltas,
            "sem_custo_falta": sem_custo_falta,
            "atendido": unidades_faltantes == 0,
        }

    proxima = calcular_alvo(capacidade + 1)
    pipeline_alvo = lojas_pendentes if lojas_pendentes > 0 else meta_lojas
    pipeline_cenario = calcular_alvo(pipeline_alvo)
    meta_cenario = calcular_alvo(meta_lojas)

    def resumo_criticos(faltas, limite=3):
        nomes = []
        for x in faltas[:limite]:
            nomes.append(x.get("codigo") or x.get("descricao") or "-")
        return ", ".join(nomes) if nomes else "Nenhum"

    def _nome_item_exec(item):
        return item.get("codigo") or item.get("descricao") or "-"

    def resumo_limitantes(lista, limite=3):
        nomes = [_nome_item_exec(x) for x in (lista or []) if _nome_item_exec(x)]
        return ", ".join(nomes[:limite]) if nomes else "-"

    def limitante_do_cenario(tipo, faltas=None):
        faltas = faltas or []
        if tipo == "atual":
            return resumo_limitantes(limitantes)
        if faltas:
            ordenadas = sorted(
                faltas,
                key=lambda x: (-int(x.get("falta") or 0), int(x.get("lojas_suportadas") or 0), (x.get("descricao") or "").lower())
            )
            principais = [_nome_item_exec(x) for x in ordenadas[:3] if _nome_item_exec(x)]
            if principais:
                return ", ".join(principais)
        return resumo_limitantes(limitantes)

    cenarios = [
        {
            "nome": "Estoque atual",
            "descricao": "Sem compra adicional",
            "lojas": capacidade,
            "unidades_faltantes": 0,
            "itens_com_falta": 0,
            "valor_faltante": Decimal("0.00"),
            "situacao": "Disponível agora" if capacidade > 0 else "Sem loja completa",
            "criticos": ", ".join((x.get("codigo") or x.get("descricao") or "-") for x in limitantes[:3]) or "-",
            "item_limitante": limitante_do_cenario("atual"),
        },
        {
            "nome": "Próxima loja",
            "descricao": "O que falta para abrir mais 1 loja",
            "lojas": capacidade + 1,
            "unidades_faltantes": proxima["unidades_faltantes"],
            "itens_com_falta": proxima["itens_com_falta"],
            "valor_faltante": proxima["valor_faltante"],
            "situacao": "Atendido" if proxima["atendido"] else "Requer complemento",
            "criticos": resumo_criticos(proxima["faltas"]),
            "item_limitante": limitante_do_cenario("proxima", proxima["faltas"]),
        },
        {
            "nome": "Pipeline atual",
            "descricao": "Cobertura das lojas pendentes",
            "lojas": pipeline_alvo,
            "unidades_faltantes": pipeline_cenario["unidades_faltantes"],
            "itens_com_falta": pipeline_cenario["itens_com_falta"],
            "valor_faltante": pipeline_cenario["valor_faltante"],
            "situacao": "Atendido" if pipeline_cenario["atendido"] else "Requer complemento",
            "criticos": resumo_criticos(pipeline_cenario["faltas"]),
            "item_limitante": limitante_do_cenario("pipeline", pipeline_cenario["faltas"]),
        },
        {
            "nome": "Meta salva",
            "descricao": "Cobertura da meta definida no sistema",
            "lojas": meta_lojas,
            "unidades_faltantes": meta_cenario["unidades_faltantes"],
            "itens_com_falta": meta_cenario["itens_com_falta"],
            "valor_faltante": meta_cenario["valor_faltante"],
            "situacao": "Atendido" if meta_cenario["atendido"] else "Requer complemento",
            "criticos": resumo_criticos(meta_cenario["faltas"]),
            "item_limitante": limitante_do_cenario("meta", meta_cenario["faltas"]),
        },
    ]

    for item in linhas:
        item["necessario_meta"] = item["qtd_por_loja"] * meta_lojas
        item["saldo_meta"] = item["estoque"] - item["necessario_meta"]
        item["faltam_meta"] = max(0, -item["saldo_meta"])
        item["situacao_meta"] = "Atende" if item["saldo_meta"] >= 0 else "Falta"
        item["valor_meta"] = (item["custo"] * item["necessario_meta"]).quantize(Decimal("0.01")) if item["custo_informado"] else Decimal("0.00")
        item["valor_faltante_meta"] = (item["custo"] * item["faltam_meta"]).quantize(Decimal("0.01")) if item["custo_informado"] else Decimal("0.00")

    cobertura_meta = (Decimal(capacidade) / Decimal(meta_lojas) * Decimal("100")) if meta_lojas else Decimal("0")
    linhas.sort(key=lambda x: (x["lojas_suportadas"], x["codigo"], x["descricao"].lower()))
    return {
        "gerado_em": datetime.now(),
        "meta_lojas": meta_lojas,
        "lojas_pendentes": lojas_pendentes,
        "capacidade_lojas": capacidade,
        "cobertura_meta": cobertura_meta.quantize(Decimal("0.1")),
        "total_unidades_expansao": total_unidades_expansao,
        "total_unidades_kit_estoque": total_unidades_kit_estoque,
        "valor_estoque_kit": valor_estoque_kit.quantize(Decimal("0.01")),
        "valor_kit_loja": valor_kit_loja.quantize(Decimal("0.01")),
        "valor_faltante_meta": meta_cenario["valor_faltante"],
        "itens_sem_custo": itens_sem_custo,
        "limitantes": limitantes,
        "ranking_gargalos": linhas[:5],
        "proxima_loja": proxima,
        "pipeline_cenario": pipeline_cenario,
        "meta_cenario": meta_cenario,
        "cenarios": cenarios,
        "linhas": linhas,
    }


@app.route("/relatorio-executivo-estoque-kit.xlsx")
@role_required("admin", "gestor", "operador")
def exportar_relatorio_executivo_estoque_kit_excel():
    dados = _calcular_relatorio_executivo_estoque_kit()
    wb = Workbook()
    ws = wb.active
    ws.title = "Executivo"

    escuro = "1A2029"; azul = "2876BE"; verde = "2B915D"; laranja = "CD8018"; vermelho = "B43C2D"; cinza = "66707D"; claro = "F5F7FA"
    borda = Border(left=Side(style="thin", color="E1E5EA"), right=Side(style="thin", color="E1E5EA"), top=Side(style="thin", color="E1E5EA"), bottom=Side(style="thin", color="E1E5EA"))

    ws.merge_cells("A1:M1")
    ws["A1"] = "Relatório Executivo · Capacidade de Abertura de Lojas"
    ws["A1"].font = Font(name="Aptos Display", size=18, bold=True, color="FFFFFF")
    ws["A1"].fill = PatternFill("solid", fgColor=escuro)
    ws["A1"].alignment = Alignment(vertical="center")
    ws.row_dimensions[1].height = 30
    ws.merge_cells("A2:M2")
    ws["A2"] = f"Estoque de Expansão x Kit Padrão · Gerado em {dados['gerado_em'].strftime('%d/%m/%Y %H:%M')}"
    ws["A2"].font = Font(size=9, color=cinza)

    limitante_txt = ", ".join((x.get("codigo") or x.get("descricao") or "-") for x in dados["limitantes"][:3]) or "-"
    proxima_falta = dados["proxima_loja"]["unidades_faltantes"]
    cards = [
        ("LOJAS POSSÍVEIS AGORA", dados["capacidade_lojas"], verde),
        ("ITEM LIMITANTE", limitante_txt, laranja),
        ("PRÓXIMA LOJA · UNIDADES FALTANTES", proxima_falta, laranja if proxima_falta else verde),
        ("LOJAS PENDENTES NO PIPELINE", dados["lojas_pendentes"], azul),
    ]
    for i, (rotulo, valor, cor) in enumerate(cards):
        col = 1 + (i % 2) * 7
        row = 4 + (i // 2) * 3
        ws.merge_cells(start_row=row, start_column=col, end_row=row, end_column=col+4)
        ws.cell(row, col, rotulo).font = Font(size=9, bold=True, color=cinza)
        ws.merge_cells(start_row=row+1, start_column=col, end_row=row+1, end_column=col+4)
        c = ws.cell(row+1, col, valor)
        c.font = Font(size=16 if i != 1 else 11, bold=True, color=cor)
        c.fill = PatternFill("solid", fgColor=claro)
        c.alignment = Alignment(vertical="center", wrap_text=True)
        for cc in range(col, col+5):
            ws.cell(row+1, cc).border = borda
        ws.row_dimensions[row+1].height = 30

    row = 11
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=13)
    ws.cell(row, 1, "PROJEÇÃO POR CENÁRIO").font = Font(size=11, bold=True, color="FFFFFF")
    ws.cell(row, 1).fill = PatternFill("solid", fgColor=azul)
    headers = ["Cenário", "Lojas", "Situação", "Item limitante", "Itens com falta", "Unidades faltantes", "Compra estimada", "Itens críticos"]
    row += 1
    spans = [(1,2),(3,3),(4,5),(6,8),(9,9),(10,10),(11,11),(12,13)]
    for h,(c1,c2) in zip(headers, spans):
        if c1 != c2: ws.merge_cells(start_row=row,start_column=c1,end_row=row,end_column=c2)
        cell=ws.cell(row,c1,h); cell.font=Font(bold=True,color="FFFFFF",size=9); cell.fill=PatternFill("solid",fgColor=escuro); cell.alignment=Alignment(horizontal="center",vertical="center")
    for cen in dados["cenarios"]:
        row += 1
        vals=[cen["nome"],cen["lojas"],cen["situacao"],cen.get("item_limitante") or "-",cen["itens_com_falta"],cen["unidades_faltantes"],float(cen["valor_faltante"]),cen["criticos"]]
        for val,(c1,c2),idx in zip(vals,spans,range(len(vals))):
            if c1 != c2: ws.merge_cells(start_row=row,start_column=c1,end_row=row,end_column=c2)
            cell=ws.cell(row,c1,val); cell.border=borda; cell.alignment=Alignment(horizontal="center" if idx not in (0,3,7) else "left",vertical="center",wrap_text=True)
            for cc in range(c1,c2+1): ws.cell(row,cc).border=borda
            if idx==6: cell.number_format='R$ #,##0.00'
        ws.cell(row,4).font=Font(bold=True,color=verde if cen["situacao"] in ("Disponível agora","Atendido") else laranja)
        for cc in range(6,9):
            ws.cell(row,cc).fill=PatternFill("solid",fgColor="FFF4E5")
        ws.cell(row,6).font=Font(bold=True,color=laranja)

    row += 2
    ws.merge_cells(start_row=row,start_column=1,end_row=row,end_column=13)
    ws.cell(row,1,"RANKING DE GARGALOS · ITENS QUE LIMITAM PRIMEIRO A ABERTURA").font=Font(size=11,bold=True,color="FFFFFF")
    ws.cell(row,1).fill=PatternFill("solid",fgColor=laranja)
    row += 1
    garg_headers=["Posição","Código","Item","Estoque","Qtd./loja","Cobertura (lojas)","Falta p/ próxima loja"]
    garg_spans=[(1,1),(2,3),(4,7),(8,8),(9,9),(10,11),(12,13)]
    for h,(c1,c2) in zip(garg_headers,garg_spans):
        if c1!=c2: ws.merge_cells(start_row=row,start_column=c1,end_row=row,end_column=c2)
        cell=ws.cell(row,c1,h); cell.font=Font(bold=True,color="FFFFFF",size=9); cell.fill=PatternFill("solid",fgColor=escuro); cell.alignment=Alignment(horizontal="center")
    for pos,item in enumerate(dados["ranking_gargalos"],1):
        row += 1
        falta_proxima=max(0,(dados["capacidade_lojas"]+1)*item["qtd_por_loja"]-item["estoque"])
        vals=[pos,item["codigo"] or "-",item["descricao"],item["estoque"],item["qtd_por_loja"],item["lojas_suportadas"],falta_proxima]
        for val,(c1,c2),idx in zip(vals,garg_spans,range(len(vals))):
            if c1!=c2: ws.merge_cells(start_row=row,start_column=c1,end_row=row,end_column=c2)
            cell=ws.cell(row,c1,val); cell.border=borda; cell.alignment=Alignment(horizontal="center" if idx not in (1,2) else "left",vertical="center",wrap_text=True)
            for cc in range(c1,c2+1): ws.cell(row,cc).border=borda
        if item["lojas_suportadas"]==dados["capacidade_lojas"]:
            for cc in range(1,14): ws.cell(row,cc).fill=PatternFill("solid",fgColor="FFF4E5")

    row += 2
    ws.merge_cells(start_row=row,start_column=1,end_row=row,end_column=13)
    ws.cell(row,1,"DETALHAMENTO · ESTOQUE x KIT PADRÃO").font=Font(size=11,bold=True,color="FFFFFF")
    ws.cell(row,1).fill=PatternFill("solid",fgColor=escuro)
    row += 1
    header_row=row
    headers=["Código","Item","Estoque","Qtd./loja","Lojas suportadas",f"Nec. meta {dados['meta_lojas']}","Saldo meta","Situação","Custo unit.","Valor estoque","Kit/loja","Valor meta","Compra meta"]
    for col,h in enumerate(headers,1):
        c=ws.cell(row,col,h); c.font=Font(bold=True,color="FFFFFF",size=8); c.fill=PatternFill("solid",fgColor=escuro); c.alignment=Alignment(horizontal="center",vertical="center",wrap_text=True); c.border=borda
    for item in dados["linhas"]:
        row += 1
        vals=[item["codigo"] or "-",item["descricao"],item["estoque"],item["qtd_por_loja"],item["lojas_suportadas"],item["necessario_meta"],item["saldo_meta"],item["situacao_meta"],float(item["custo"]) if item["custo_informado"] else None,float(item["valor_estoque"]) if item["custo_informado"] else None,float(item["valor_kit_loja"]) if item["custo_informado"] else None,float(item["valor_meta"]) if item["custo_informado"] else None,float(item["valor_faltante_meta"]) if item["custo_informado"] else None]
        for col,val in enumerate(vals,1):
            c=ws.cell(row,col,val); c.border=borda; c.alignment=Alignment(vertical="center",wrap_text=(col==2),horizontal="center" if col not in (1,2) else "left")
            if row%2==0: c.fill=PatternFill("solid",fgColor="F8FAFC")
            if col in (9,10,11,12,13) and val is not None: c.number_format='R$ #,##0.00'
        ws.cell(row,8).font=Font(bold=True,color=verde if item["situacao_meta"]=="Atende" else vermelho)
        if item["lojas_suportadas"]==dados["capacidade_lojas"]:
            for cc in range(1,14): ws.cell(row,cc).fill=PatternFill("solid",fgColor="FFF8ED")

    larguras=[16,38,15,12,17,17,15,13,15,18,16,18,18]
    for idx,largura in enumerate(larguras,1): ws.column_dimensions[get_column_letter(idx)].width=largura
    ws.freeze_panes=f"A{header_row+1}"; ws.sheet_view.showGridLines=False
    ws.page_setup.orientation="landscape"; ws.page_setup.fitToWidth=1; ws.page_setup.fitToHeight=0; ws.sheet_properties.pageSetUpPr.fitToPage=True
    ws.oddFooter.center.text="© 2026 · Developed by ALM - Expansão de TI"; ws.oddFooter.right.text="Página &P de &N"

    cen_ws=wb.create_sheet("Cenários")
    cen_ws.append(["Cenário","Lojas objetivo","Situação","Item limitante","Itens com falta","Unidades faltantes","Compra estimada","Itens críticos"])
    for cen in dados["cenarios"]:
        cen_ws.append([cen["nome"],cen["lojas"],cen["situacao"],cen.get("item_limitante") or "-",cen["itens_com_falta"],cen["unidades_faltantes"],float(cen["valor_faltante"]),cen["criticos"]])
    for c in cen_ws[1]: c.font=Font(bold=True,color="FFFFFF"); c.fill=PatternFill("solid",fgColor=escuro); c.alignment=Alignment(horizontal="center")
    for r in range(2,cen_ws.max_row+1):
        cen_ws.cell(r,7).number_format='R$ #,##0.00'
        cen_ws.cell(r,4).fill=PatternFill("solid",fgColor="FFF4E5")
        cen_ws.cell(r,4).font=Font(bold=True,color=laranja)
    for i,w in enumerate([22,16,22,34,16,18,20,45],1): cen_ws.column_dimensions[get_column_letter(i)].width=w
    cen_ws.freeze_panes="A2"; cen_ws.sheet_view.showGridLines=False

    graf_ws = wb.create_sheet("Gráfico Cenários")
    graf_ws.append(["Cenário","Lojas","Item limitante"])
    for cen in dados["cenarios"]:
        graf_ws.append([cen["nome"], cen["lojas"], cen.get("item_limitante") or "-"])
    for c in graf_ws[1]:
        c.font=Font(bold=True,color="FFFFFF")
        c.fill=PatternFill("solid",fgColor=escuro)
        c.alignment=Alignment(horizontal="center")
    for r in range(2, graf_ws.max_row+1):
        graf_ws.cell(r,3).fill = PatternFill("solid", fgColor="FFF4E5")
        graf_ws.cell(r,3).font = Font(bold=True, color=laranja)
    for i,w in enumerate([24,12,42],1):
        graf_ws.column_dimensions[get_column_letter(i)].width=w
    chart = BarChart()
    chart.type = "bar"
    chart.style = 10
    chart.title = "Capacidade de abertura por cenário"
    chart.y_axis.title = "Cenário"
    chart.x_axis.title = "Quantidade de lojas"
    chart.height = 8
    chart.width = 18
    data = Reference(graf_ws, min_col=2, min_row=1, max_row=1+len(dados["cenarios"]))
    cats = Reference(graf_ws, min_col=1, min_row=2, max_row=1+len(dados["cenarios"]))
    chart.add_data(data, titles_from_data=True)
    chart.set_categories(cats)
    chart.legend = None
    if chart.series:
        chart.series[0].graphicalProperties.solidFill = azul
        chart.series[0].graphicalProperties.line.solidFill = azul
    graf_ws.add_chart(chart, "E2")
    graf_ws.sheet_view.showGridLines=False

    notas=wb.create_sheet("Premissas")
    notas.append(["Premissa","Regra"])
    notas.append(["Lojas possíveis agora","Menor cobertura entre todos os itens do Kit: Estoque de Expansão dividido pela quantidade exigida por loja."])
    notas.append(["Item limitante","Item ou itens com a menor quantidade de lojas suportadas; eles determinam a capacidade real de abertura."])
    notas.append(["Próxima loja","Quantidade complementar necessária para elevar a capacidade atual em exatamente uma loja."])
    notas.append(["Pipeline atual","Compara o estoque com a quantidade de lojas pendentes identificadas no acompanhamento de expansão."])
    notas.append(["Meta salva","Compara o estoque com a meta de lojas definida no sistema."])
    notas.append(["Financeiro","Valores usam o custo do Cadastro de Produtos; itens sem custo continuam no cálculo físico."])
    notas.append(["Itens sem custo",dados["itens_sem_custo"]])
    notas.column_dimensions["A"].width=32; notas.column_dimensions["B"].width=110
    for c in notas[1]: c.font=Font(bold=True,color="FFFFFF"); c.fill=PatternFill("solid",fgColor=escuro)
    notas.sheet_view.showGridLines=False

    buf=io.BytesIO(); wb.save(buf); buf.seek(0)
    return send_file(buf,as_attachment=True,download_name=f"relatorio_executivo_capacidade_lojas_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/relatorio-executivo-estoque-kit.pdf")
@role_required("admin", "gestor", "operador")
def exportar_relatorio_executivo_estoque_kit_pdf():
    dados=_calcular_relatorio_executivo_estoque_kit()
    buf=io.BytesIO()
    doc=SimpleDocTemplate(buf,pagesize=landscape(A4),rightMargin=10*mm,leftMargin=10*mm,topMargin=10*mm,bottomMargin=12*mm,title="Relatório Executivo - Capacidade de Abertura",author="Expansão de TI")
    styles=getSampleStyleSheet()
    styles.add(ParagraphStyle(name="ExecTituloV120",parent=styles["Title"],fontSize=17,leading=20,textColor=colors.HexColor("#1A2029"),spaceAfter=3))
    styles.add(ParagraphStyle(name="ExecSubV120",parent=styles["Normal"],fontSize=8.3,leading=10.5,textColor=colors.HexColor("#66707D"),spaceAfter=6))
    styles.add(ParagraphStyle(name="ExecCellV120",parent=styles["Normal"],fontSize=6.5,leading=7.8,textColor=colors.HexColor("#20242B")))
    styles.add(ParagraphStyle(name="ExecHeroV120",parent=styles["Normal"],fontSize=10,leading=12,textColor=colors.HexColor("#1A2029")))
    story=[Paragraph("Relatório Executivo · Capacidade de Abertura de Lojas",styles["ExecTituloV120"]),Paragraph(f"Estoque de Expansão x Kit Padrão · Gerado em {dados['gerado_em'].strftime('%d/%m/%Y %H:%M')}. Prioridade: lojas possíveis, gargalos e projeção por cenário.",styles["ExecSubV120"])]

    limitante_txt=", ".join((x.get("codigo") or x.get("descricao") or "-") for x in dados["limitantes"][:3]) or "-"
    hero=[
        ["LOJAS POSSÍVEIS AGORA",str(dados["capacidade_lojas"]),"ITEM LIMITANTE",limitante_txt],
        ["PRÓXIMA LOJA · UNIDADES FALTANTES",str(dados["proxima_loja"]["unidades_faltantes"]),"LOJAS PENDENTES NO PIPELINE",str(dados["lojas_pendentes"])],
    ]
    ht=Table(hero,colWidths=[55*mm,27*mm,55*mm,45*mm],hAlign="LEFT")
    ht.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,-1),colors.HexColor("#F5F7FA")),("BOX",(0,0),(-1,-1),0.45,colors.HexColor("#DDE3EA")),("INNERGRID",(0,0),(-1,-1),0.3,colors.HexColor("#E1E5EA")),("FONTNAME",(0,0),(0,-1),"Helvetica-Bold"),("FONTNAME",(2,0),(2,-1),"Helvetica-Bold"),("TEXTCOLOR",(0,0),(0,-1),colors.HexColor("#66707D")),("TEXTCOLOR",(2,0),(2,-1),colors.HexColor("#66707D")),("FONTNAME",(1,0),(1,-1),"Helvetica-Bold"),("FONTNAME",(3,0),(3,-1),"Helvetica-Bold"),("FONTSIZE",(1,0),(1,-1),14),("FONTSIZE",(3,0),(3,-1),9),("VALIGN",(0,0),(-1,-1),"MIDDLE"),("LEFTPADDING",(0,0),(-1,-1),5),("RIGHTPADDING",(0,0),(-1,-1),5),("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6)]))
    story.extend([ht,Spacer(1,4*mm),Paragraph("<b>Capacidade de abertura por cenário</b>",styles["ExecHeroV120"])])

    valores_cenario = [int(c.get("lojas") or 0) for c in dados["cenarios"]]
    max_val = max(valores_cenario) if valores_cenario else 0
    desenho = Drawing(520, 165)
    desenho.add(String(5, 150, "Gráfico · quantas lojas conseguimos abrir em cada cenário", fontName="Helvetica-Bold", fontSize=9, fillColor=colors.HexColor("#1A2029")))
    graf = VerticalBarChart()
    graf.x = 30
    graf.y = 35
    graf.height = 90
    graf.width = 450
    graf.data = [valores_cenario]
    graf.categoryAxis.categoryNames = [c.get("nome") for c in dados["cenarios"]]
    graf.categoryAxis.labels.boxAnchor = "ne"
    graf.categoryAxis.labels.angle = 25
    graf.categoryAxis.labels.fontName = "Helvetica"
    graf.categoryAxis.labels.fontSize = 7
    graf.valueAxis.valueMin = 0
    graf.valueAxis.valueMax = max(max_val + 1, 1)
    graf.valueAxis.valueStep = max(1, int((max_val + 4) / 5)) if max_val else 1
    graf.valueAxis.labels.fontSize = 7
    graf.bars[0].fillColor = colors.HexColor("#2876BE")
    graf.bars[0].strokeColor = colors.HexColor("#2876BE")
    desenho.add(graf)
    desenho.add(String(30, 12, "Os itens limitantes de cada cenário estão destacados na tabela abaixo.", fontName="Helvetica", fontSize=7, fillColor=colors.HexColor("#66707D")))
    story.extend([desenho, Spacer(1, 3*mm), Paragraph("<b>Projeção por cenário</b>",styles["ExecHeroV120"])])

    scen=[["Cenário","Lojas","Situação","Item limitante","Itens c/ falta","Unid. faltantes","Compra estimada","Itens críticos"]]
    for c in dados["cenarios"]:
        scen.append([c["nome"],str(c["lojas"]),c["situacao"],Paragraph(c.get("item_limitante") or "-",styles["ExecCellV120"]),str(c["itens_com_falta"]),str(c["unidades_faltantes"]),_formatar_moeda_br(c["valor_faltante"]),Paragraph(c["criticos"],styles["ExecCellV120"])])
    st=Table(scen,repeatRows=1,colWidths=[23*mm,13*mm,25*mm,46*mm,17*mm,20*mm,24*mm,42*mm])
    st_style=[("BACKGROUND",(0,0),(-1,0),colors.HexColor("#2876BE")),("TEXTCOLOR",(0,0),(-1,0),colors.white),("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),("FONTSIZE",(0,0),(-1,0),7),("FONTSIZE",(0,1),(-1,-1),6.8),("GRID",(0,0),(-1,-1),0.35,colors.HexColor("#D9E0E7")),("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#F8FAFC")]),("ALIGN",(1,1),(1,-1),"CENTER"),("ALIGN",(4,1),(6,-1),"CENTER"),("VALIGN",(0,0),(-1,-1),"MIDDLE"),("LEFTPADDING",(0,0),(-1,-1),3),("RIGHTPADDING",(0,0),(-1,-1),3),("TOPPADDING",(0,0),(-1,-1),4),("BOTTOMPADDING",(0,0),(-1,-1),4),("BACKGROUND",(3,1),(3,-1),colors.HexColor("#FFF4E5")),("TEXTCOLOR",(3,1),(3,-1),colors.HexColor("#B86A06")),("FONTNAME",(3,1),(3,-1),"Helvetica-Bold")]
    st.setStyle(TableStyle(st_style))
    story.extend([st,Spacer(1,4*mm),Paragraph("<b>Ranking de gargalos</b> · os primeiros itens abaixo são os que reduzem primeiro a quantidade de lojas que podem ser abertas.",styles["ExecSubV120"])])

    garg=[["#","Código","Item","Estoque","Qtd./loja","Cobertura","Falta p/ próxima"]]
    for pos,item in enumerate(dados["ranking_gargalos"],1):
        falta=max(0,(dados["capacidade_lojas"]+1)*item["qtd_por_loja"]-item["estoque"])
        garg.append([str(pos),item["codigo"] or "-",Paragraph(item["descricao"] or "-",styles["ExecCellV120"]),str(item["estoque"]),str(item["qtd_por_loja"]),str(item["lojas_suportadas"]),str(falta)])
    gt=Table(garg,repeatRows=1,colWidths=[9*mm,22*mm,72*mm,20*mm,20*mm,22*mm,28*mm])
    gst=[("BACKGROUND",(0,0),(-1,0),colors.HexColor("#CD8018")),("TEXTCOLOR",(0,0),(-1,0),colors.white),("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),("FONTSIZE",(0,0),(-1,-1),7),("GRID",(0,0),(-1,-1),0.35,colors.HexColor("#D9E0E7")),("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#F8FAFC")]),("ALIGN",(0,1),(1,-1),"CENTER"),("ALIGN",(3,1),(-1,-1),"CENTER"),("VALIGN",(0,0),(-1,-1),"MIDDLE")]
    for i,item in enumerate(dados["ranking_gargalos"],1):
        if item["lojas_suportadas"]==dados["capacidade_lojas"]: gst.append(("BACKGROUND",(0,i),(-1,i),colors.HexColor("#FFF4E5")))
    gt.setStyle(TableStyle(gst)); story.extend([gt,PageBreak()])

    story.extend([Paragraph("Detalhamento · Estoque de Expansão x Kit Padrão",styles["ExecTituloV120"]),Paragraph("A tabela detalha a cobertura individual de cada item. A menor cobertura determina a capacidade real de abertura.",styles["ExecSubV120"])])
    cab=["Código","Item","Estoque","Qtd./loja","Lojas","Nec. meta","Saldo","Custo unit.","Valor estoque","Compra meta"]
    tab=[cab]
    for item in dados["linhas"]:
        tab.append([item["codigo"] or "-",Paragraph(item["descricao"] or "-",styles["ExecCellV120"]),str(item["estoque"]),str(item["qtd_por_loja"]),str(item["lojas_suportadas"]),str(item["necessario_meta"]),str(item["saldo_meta"]),_formatar_moeda_br(item["custo"]) if item["custo_informado"] else "Sem custo",_formatar_moeda_br(item["valor_estoque"]) if item["custo_informado"] else "-",_formatar_moeda_br(item["valor_faltante_meta"]) if item["custo_informado"] else "-"])
    dt=Table(tab,repeatRows=1,colWidths=[17*mm,55*mm,18*mm,18*mm,16*mm,20*mm,18*mm,24*mm,27*mm,27*mm])
    estilo=[("BACKGROUND",(0,0),(-1,0),colors.HexColor("#1A2029")),("TEXTCOLOR",(0,0),(-1,0),colors.white),("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),("FONTSIZE",(0,0),(-1,-1),6.6),("ALIGN",(2,1),(-1,-1),"CENTER"),("VALIGN",(0,0),(-1,-1),"MIDDLE"),("GRID",(0,0),(-1,-1),0.35,colors.HexColor("#D9E0E7")),("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.white,colors.HexColor("#F8FAFC")]),("LEFTPADDING",(0,0),(-1,-1),2.5),("RIGHTPADDING",(0,0),(-1,-1),2.5),("TOPPADDING",(0,0),(-1,-1),3.2),("BOTTOMPADDING",(0,0),(-1,-1),3.2)]
    for i,item in enumerate(dados["linhas"],1):
        if item["saldo_meta"]<0: estilo.extend([("TEXTCOLOR",(6,i),(6,i),colors.HexColor("#B43C2D")),("FONTNAME",(6,i),(6,i),"Helvetica-Bold")])
        if item["lojas_suportadas"]==dados["capacidade_lojas"]: estilo.append(("BACKGROUND",(0,i),(-1,i),colors.HexColor("#FFF8ED")))
    dt.setStyle(TableStyle(estilo)); story.extend([dt,Spacer(1,4*mm)])
    story.append(Paragraph(f"Leitura executiva: hoje o estoque permite abrir <b>{dados['capacidade_lojas']} loja(s) completa(s)</b>. Para a próxima loja faltam <b>{dados['proxima_loja']['unidades_faltantes']} unidade(s)</b> distribuídas em <b>{dados['proxima_loja']['itens_com_falta']} item(ns)</b>. O financeiro é complementar e não altera o cálculo físico de capacidade.",styles["ExecSubV120"]))

    def rodape(canvas_pdf,doc_pdf):
        canvas_pdf.saveState(); canvas_pdf.setFont("Helvetica",7); canvas_pdf.setFillColor(colors.HexColor("#7B8794")); canvas_pdf.drawString(10*mm,6*mm,"Developed by ALM - Expansão de TI"); canvas_pdf.drawRightString(landscape(A4)[0]-10*mm,6*mm,f"Página {doc_pdf.page}"); canvas_pdf.restoreState()
    doc.build(story,onFirstPage=rodape,onLaterPages=rodape); buf.seek(0)
    return send_file(buf,as_attachment=True,download_name=f"relatorio_executivo_capacidade_lojas_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf",mimetype="application/pdf")

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
    inicio = (request.args.get("inicio") or "").strip()
    fim = (request.args.get("fim") or "").strip()
    try:
        limite=max(1,min(int(request.args.get("limite", 80)),50000 if (inicio or fim) else 500))
    except (TypeError,ValueError):
        limite=80
    movs=(db.listar_movimentacoes_periodo(inicio, fim, limite)
          if (inicio or fim) else db.listar_movimentacoes_recentes(limite))
    itens={str(x.get("id")):x for x in db.listar_itens()}
    imobs={str(x.get("id")):x for x in db.listar_imobilizados()}
    for m in movs:
        tabela=m.get("tabela") or "itens"
        if tabela == "sistema":
            m["codigo"] = "META LOJAS"
            m["descricao"] = "Meta do lote de inauguração"
        else:
            ref=(imobs if tabela=="imobilizados" else itens).get(str(m.get("item_id")), {})
            m["codigo"]=ref.get("codigo","")
            m["descricao"]=ref.get("descricao","")
    return jsonify(movs)


@app.route("/api/movimentacoes/excluir-lote", methods=["POST"])
@admin_required
def api_excluir_movimentacoes_lote():
    dados = request.get_json(silent=True) or {}
    ids_brutos = dados.get("ids") or []
    if not isinstance(ids_brutos, list):
        return jsonify({"erro": "Lista de movimentações inválida."}), 400
    try:
        ids = sorted({int(x) for x in ids_brutos if int(x) > 0})
    except (TypeError, ValueError):
        return jsonify({"erro": "Um ou mais identificadores são inválidos."}), 400
    if not ids:
        return jsonify({"erro": "Selecione ao menos uma movimentação."}), 400
    if len(ids) > 50000:
        return jsonify({"erro": "O limite por exclusão é de 50.000 movimentações."}), 400
    total = db.excluir_movimentacoes_por_ids(ids)
    usuario = session.get("username") or "Administrador"
    db.salvar_configuracao(
        "ultima_exclusao_historico",
        f"{datetime.now().strftime('%d/%m/%Y %H:%M:%S')} · {usuario} · {total} registro(s)",
        usuario,
    )
    return jsonify({"ok": True, "excluidas": total})


@app.route("/api/movimentacoes/excluir-tudo", methods=["POST"])
@admin_required
def api_excluir_todas_movimentacoes():
    dados = request.get_json(silent=True) or {}
    if dados.get("confirmacao") != "APAGAR TUDO":
        return jsonify({"erro": "Confirmação inválida. Digite APAGAR TUDO."}), 400
    movimentacoes = db.listar_todas_movimentacoes()
    if not movimentacoes:
        return jsonify({"erro": "O histórico de movimentações já está vazio."}), 400

    # Gera o arquivo completo antes de remover qualquer registro.
    wb = Workbook()
    ws = wb.active
    ws.title = "Histórico removido"
    _preencher_planilha_dict(ws, movimentacoes)
    info = wb.create_sheet("Informações")
    agora = datetime.now()
    usuario = session.get("username") or "Administrador"
    info.append(["Backup anterior à exclusão total do histórico"])
    info.append(["Gerado em", agora.strftime("%d/%m/%Y %H:%M:%S")])
    info.append(["Executado por", usuario])
    info.append(["Registros arquivados", len(movimentacoes)])

    ids = [m["id"] for m in movimentacoes]
    total = db.excluir_movimentacoes_por_ids(ids)
    db.salvar_configuracao(
        "ultima_exclusao_total_historico",
        f"{agora.strftime('%d/%m/%Y %H:%M:%S')} · {usuario} · {total} registro(s)",
        usuario,
    )

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    nome = f"backup_historico_completo_{agora.strftime('%Y%m%d_%H%M%S')}.xlsx"
    resposta = send_file(
        buf,
        as_attachment=True,
        download_name=nome,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resposta.headers["X-Registros-Excluidos"] = str(total)
    return resposta


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


def _decimal_moeda(valor, padrao="0.00"):
    """Converte moeda em formato BR/US para Decimal com 2 casas."""
    if valor is None or str(valor).strip() == "":
        valor = padrao
    texto = str(valor).strip().replace("R$", "").replace(" ", "")
    if "," in texto:
        texto = texto.replace(".", "").replace(",", ".")
    try:
        numero = Decimal(texto)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Valor monetário inválido.")
    return numero.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def _moeda_canonica(valor, padrao="0.00"):
    return format(_decimal_moeda(valor, padrao), ".2f")


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
    for filial in db.listar_filiais(incluir_inativas=True):
        alvo=f"{filial.get('codigo','')} {filial.get('nome','')} {filial.get('cidade','')} {filial.get('uf','')}".lower()
        alvo=unicodedata.normalize("NFD",alvo); alvo="".join(c for c in alvo if unicodedata.category(c)!="Mn")
        if termo in alvo:
            resultados.append({"origem":"Filiais","id":filial.get("id"),"codigo":filial.get("codigo","") or "","descricao":filial.get("nome","") or "","quantidade":"","tipo_estoque":"","status":_status_filial_normalizado(filial.get("ativo")),"localizacao":f"{filial.get('cidade','')} / {filial.get('uf','')}","nro_serie":"","nro_patrimonio":"","filial_destino":""})
        if len(resultados)>=100: break
    for prod in db.listar_produtos():
        alvo=f"{prod.get('codigo','')} {prod.get('descricao','')}".lower()
        alvo=unicodedata.normalize("NFD",alvo)
        alvo="".join(c for c in alvo if unicodedata.category(c)!="Mn")
        if termo in alvo:
            resultados.append({"origem":"Cadastro de Produtos","id":prod.get("id"),"codigo":prod.get("codigo","") or "","descricao":prod.get("descricao","") or "","quantidade":"","tipo_estoque":"","status":"","localizacao":"","nro_serie":"","nro_patrimonio":"","filial_destino":""})
        if len(resultados)>=100: break
    return jsonify(resultados)


def _normalizar_exec(valor):
    texto=unicodedata.normalize("NFD",str(valor or "").strip().lower())
    return "".join(c for c in texto if unicodedata.category(c)!="Mn")


CAMPOS_OBRIGATORIOS_BAIXA_ENVIO = [
    ("nf_saida", "NF de saída"),
    ("data_saida", "Data de saída"),
    ("filial_destino", "Filial / destino"),
    ("nro_imobilizado", "Nº imobilizado"),
    ("nro_serie", "Nº série"),
    ("nro_patrimonio", "Nº patrimônio"),
]

def _preparar_baixa_envio_item(item_antes, dados):
    """Valida e normaliza uma baixa definitiva do Estoque para uma filial.

    Cada linha do Estoque representa uma unidade. Ao salvar Status=Enviado,
    a unidade sai do saldo (qtde=0), mas permanece cadastrada para auditoria
    e para compor a ficha da filial de destino.
    """
    item_antes = item_antes or {}
    dados = dict(dados or {})
    combinado = dict(item_antes)
    combinado.update(dados)
    if _normalizar_exec(combinado.get("status")) != "enviado":
        return dados, None

    faltantes = [rotulo for campo, rotulo in CAMPOS_OBRIGATORIOS_BAIXA_ENVIO if not str(combinado.get(campo) or "").strip()]
    if faltantes:
        return dados, (
            "Para gravar o item como Enviado e dar baixa no estoque, preencha: "
            + ", ".join(faltantes) + "."
        )

    codigo_filial = str(combinado.get("filial_destino") or "").strip()
    if not db.buscar_filial_por_codigo(codigo_filial):
        return dados, "A filial / destino informada não foi encontrada no cadastro de Filiais."

    dados["status"] = "Enviado"
    dados["qtde"] = "0"
    for campo, _rotulo in CAMPOS_OBRIGATORIOS_BAIXA_ENVIO:
        dados[campo] = str(combinado.get(campo) or "").strip()
    return dados, None


def _equipamento_combina_kit(item, item_kit):
    codigo_k = str((item_kit or {}).get("codigo") or "").strip()
    codigo_i = str((item or {}).get("codigo") or "").strip()
    if codigo_k:
        return codigo_i == codigo_k
    desc_k = _normalizar_exec((item_kit or {}).get("descricao"))
    desc_i = _normalizar_exec((item or {}).get("descricao"))
    return bool(desc_k and desc_i and (desc_k == desc_i or desc_k in desc_i or desc_i in desc_k))


def _qtd_registro_parque(item):
    try:
        qtd = int(float((item or {}).get("qtde") or 0))
    except (TypeError, ValueError):
        qtd = 0
    return max(1, qtd)


def _chave_fisica_equipamento(item, origem):
    filial = str((item or {}).get("filial_destino") or "").strip()
    codigo = str((item or {}).get("codigo") or "").strip()
    for campo in ("nro_imobilizado", "nro_serie", "nro_patrimonio"):
        valor = str((item or {}).get(campo) or "").strip()
        if valor:
            return (filial, codigo, campo, valor)
    return (filial, codigo, origem, int((item or {}).get("id") or 0))


def _parque_real_filiais():
    """Retorna somente equipamentos realmente vinculados às filiais.

    O parque real usa os registros existentes em Estoque e Imobilizados e
    deduplica ativos identificados por imobilizado, série ou patrimônio.
    Nenhuma quantidade virtual/legada é criada aqui.
    """
    resumo = {}
    vistos = set()
    for origem, registros in (("estoque", db.listar_itens()), ("imobilizados", db.listar_imobilizados())):
        for item in registros or []:
            filial = str(item.get("filial_destino") or "").strip()
            if not filial:
                continue
            chave = _chave_fisica_equipamento(item, origem)
            if chave in vistos:
                continue
            vistos.add(chave)
            qtd = _qtd_registro_parque(item)
            codigo = str(item.get("codigo") or "").strip() or "SEM-CODIGO"
            desc = str(item.get("descricao") or "Item sem descrição").strip()
            f = resumo.setdefault(filial, {"unidades": 0, "tipos": set(), "por_item": {}})
            f["unidades"] += qtd
            f["tipos"].add(codigo)
            g = f["por_item"].setdefault(codigo, {
                "codigo": codigo, "descricao": desc, "unidades": 0,
                "reais": 0, "legado": 0,
            })
            g["unidades"] += qtd
            g["reais"] += qtd
    for f in resumo.values():
        f["tipos_total"] = len(f["tipos"])
        f["por_item_lista"] = sorted(f["por_item"].values(), key=lambda x: (x.get("codigo") or "", x.get("descricao") or ""))
    return resumo


def _resumo_parque_filiais(incluir_legado_ativas=True, somente_ativas=False):
    """Visão do parque por filial.

    Regra v117:
    - lojas ATIVAS antigas recebem, para fins de parque, no mínimo o Kit padrão
      completo, mesmo quando não existe rastreabilidade histórica no Estoque;
    - a diferença é classificada como ``legado`` e NÃO movimenta o estoque;
    - lojas A INAUGURAR/PENDENTES nunca recebem preenchimento legado; nelas só
      contam unidades realmente baixadas/vinculadas;
    - ``somente_ativas=True`` é usado na visão geral do parque operacional.
    """
    real = _parque_real_filiais()
    filiais = db.listar_filiais(incluir_inativas=True) or []
    kit = db.listar_kit_padrao_loja() or []
    saida = {}

    for filial in filiais:
        codigo_filial = str(filial.get("codigo") or "").strip()
        if not codigo_filial:
            continue
        ativa = str(filial.get("ativo") or "").strip() == "1"
        if somente_ativas and not ativa:
            continue

        base = real.get(codigo_filial) or {"unidades": 0, "tipos": set(), "por_item": {}}
        por_item = {}
        for codigo_item, item in (base.get("por_item") or {}).items():
            por_item[codigo_item] = dict(item)

        if ativa and incluir_legado_ativas:
            for k in kit:
                codigo_k = str(k.get("codigo") or "").strip() or "SEM-CODIGO"
                desc_k = str(k.get("descricao") or "Item sem descrição").strip()
                try:
                    necessario = max(1, int(float(k.get("quantidade") or 1)))
                except Exception:
                    necessario = 1
                g = por_item.setdefault(codigo_k, {
                    "codigo": codigo_k, "descricao": desc_k,
                    "unidades": 0, "reais": 0, "legado": 0,
                })
                reais = int(g.get("reais") or 0)
                faltam_legado = max(0, necessario - reais)
                if faltam_legado:
                    g["legado"] = int(g.get("legado") or 0) + faltam_legado
                    g["unidades"] = int(g.get("unidades") or 0) + faltam_legado
                if not g.get("descricao"):
                    g["descricao"] = desc_k

        itens_lista = sorted(por_item.values(), key=lambda x: (x.get("codigo") or "", x.get("descricao") or ""))
        tipos = {str(x.get("codigo") or "SEM-CODIGO") for x in itens_lista if int(x.get("unidades") or 0) > 0}
        saida[codigo_filial] = {
            "unidades": sum(int(x.get("unidades") or 0) for x in itens_lista),
            "reais": sum(int(x.get("reais") or 0) for x in itens_lista),
            "legado": sum(int(x.get("legado") or 0) for x in itens_lista),
            "tipos": tipos,
            "tipos_total": len(tipos),
            "por_item": por_item,
            "por_item_lista": itens_lista,
            "filial": dict(filial),
        }
    return saida


def _faltantes_kit_real_filial(codigo_filial, itens_estoque=None, kit=None):
    """Calcula o que ainda falta baixar REALMENTE do Estoque para completar o Kit."""
    codigo_filial = str(codigo_filial or "").strip()
    itens_estoque = itens_estoque if itens_estoque is not None else (db.listar_itens() or [])
    kit = kit if kit is not None else (db.listar_kit_padrao_loja() or [])
    enviados = [
        x for x in itens_estoque
        if str(x.get("filial_destino") or "").strip() == codigo_filial
        and _normalizar_exec(x.get("status")) == "enviado"
    ]
    usados = set()
    faltantes = []
    for k in kit:
        try:
            necessario = max(1, int(float(k.get("quantidade") or 1)))
        except Exception:
            necessario = 1
        candidatos = [x for x in enviados if x.get("id") not in usados and _equipamento_combina_kit(x, k)]
        candidatos.sort(key=lambda x: int(x.get("id") or 0))
        for x in candidatos[:necessario]:
            usados.add(x.get("id"))
        falta = max(0, necessario - min(necessario, len(candidatos)))
        if falta:
            faltantes.append({
                "codigo": str(k.get("codigo") or "").strip(),
                "descricao": str(k.get("descricao") or "Item sem descrição").strip(),
                "faltam": falta,
            })
    return faltantes


def _kit_real_completo_filial(codigo_filial):
    kit = db.listar_kit_padrao_loja() or []
    return bool(kit) and not _faltantes_kit_real_filial(codigo_filial, kit=kit)


def _kit_vinculado_filial(filial):
    """Monta apenas o Kit padrão efetivamente vinculado à loja.

    Para lojas ativas antigas, completa a diferença como legado operacional,
    sem criar linhas fictícias no Estoque. Para lojas ainda não ativas, mostra
    somente o que já recebeu baixa real.
    """
    codigo_filial = str((filial or {}).get("codigo") or "").strip()
    ativa = str((filial or {}).get("ativo") or "").strip() == "1"
    kit = db.listar_kit_padrao_loja() or []
    itens = db.listar_itens() or []
    enviados = [x for x in itens if str(x.get("filial_destino") or "").strip() == codigo_filial and _normalizar_exec(x.get("status")) == "enviado"]
    usados = set()
    linhas = []
    for k in kit:
        try:
            necessario = max(1, int(float(k.get("quantidade") or 1)))
        except Exception:
            necessario = 1
        candidatos = [x for x in enviados if x.get("id") not in usados and _equipamento_combina_kit(x, k)]
        candidatos.sort(key=lambda x: int(x.get("id") or 0))
        usados_agora = candidatos[:necessario]
        for x in usados_agora:
            usados.add(x.get("id"))
        reais = min(necessario, len(usados_agora))
        legado = max(0, necessario - reais) if ativa else 0
        vinculado = reais + legado
        if vinculado <= 0:
            continue
        if legado and reais:
            situacao = "Rastreado + legado"
        elif legado:
            situacao = "Legado"
        else:
            situacao = "Rastreado"
        linhas.append({
            "codigo": str(k.get("codigo") or "").strip(),
            "descricao": str(k.get("descricao") or "Item sem descrição").strip(),
            "padrao": necessario,
            "vinculado": vinculado,
            "reais": reais,
            "legado": legado,
            "situacao": situacao,
        })
    return linhas


def _ativar_filial_se_kit_real_completo(codigo_filial, usuario=None):
    """Promove loja A inaugurar/Pendente para Ativa após a última baixa do Kit.

    A promoção usa exclusivamente unidades reais com Status=Enviado. O legado
    só existe para lojas que já eram ativas antes desta regra.
    """
    codigo_filial = str(codigo_filial or "").strip()
    if not codigo_filial:
        return {"ativada": False, "motivo": "sem_filial"}
    filial = db.buscar_filial_por_codigo(codigo_filial)
    if not filial:
        return {"ativada": False, "motivo": "filial_nao_encontrada"}
    status_atual = str(filial.get("ativo") or "").strip()
    if status_atual == "1":
        return {"ativada": False, "motivo": "ja_ativa"}
    if status_atual not in ("inaugurar", "pendente"):
        return {"ativada": False, "motivo": "status_nao_elegivel"}
    faltantes = _faltantes_kit_real_filial(codigo_filial)
    if faltantes:
        return {"ativada": False, "motivo": "kit_incompleto", "faltantes": faltantes}

    db.atualizar_filial(
        int(filial["id"]), str(filial.get("codigo") or ""), str(filial.get("nome") or ""),
        str(filial.get("cidade") or ""), str(filial.get("uf") or ""), "1",
        bandeira=filial.get("bandeira"), previsao_abertura=filial.get("previsao_abertura"),
    )

    # Mantém o Acompanhamento coerente: Kit completo por baixa = loja inaugurada/ativa.
    try:
        for acomp in db.listar_acompanhamento_expansao() or []:
            if str(acomp.get("filial") or "").strip() != codigo_filial:
                continue
            if _normalizar_exec(acomp.get("status_filial")) == "inaugurada":
                break
            payload = dict(acomp)
            payload["status_filial"] = "INAUGURADA"
            db.atualizar_acompanhamento_expansao(int(acomp["id"]), payload, usuario or "sistema")
            break
    except Exception:
        app.logger.exception("Falha ao sincronizar inauguração automática da filial %s", codigo_filial)

    try:
        db.registrar_movimentacao(
            0, "ativacao_filial_kit_completo", "1", usuario or "sistema",
            f"Filial {codigo_filial} promovida automaticamente para Ativa após completar o Kit padrão com baixas reais do Estoque.",
            tabela="sistema",
        )
    except Exception:
        app.logger.exception("Falha ao auditar ativação automática da filial %s", codigo_filial)
    return {"ativada": True, "motivo": "kit_completo"}


def _dados_equipamentos_parque():
    """Consolida o parque operacional geral das lojas ATIVAS."""
    parque = _resumo_parque_filiais(incluir_legado_ativas=True, somente_ativas=True)
    agregado = {}
    total_unidades = total_reais = total_legado = 0
    filiais_com_parque = 0
    for codigo_filial, dados in parque.items():
        if int(dados.get("unidades") or 0) > 0:
            filiais_com_parque += 1
        total_unidades += int(dados.get("unidades") or 0)
        total_reais += int(dados.get("reais") or 0)
        total_legado += int(dados.get("legado") or 0)
        for item in dados.get("por_item_lista") or []:
            codigo = str(item.get("codigo") or "SEM-CODIGO")
            g = agregado.setdefault(codigo, {
                "codigo": codigo,
                "descricao": str(item.get("descricao") or "Item sem descrição"),
                "unidades": 0, "reais": 0, "legado": 0, "filiais": 0,
            })
            qtd = int(item.get("unidades") or 0)
            if qtd > 0:
                g["filiais"] += 1
            g["unidades"] += qtd
            g["reais"] += int(item.get("reais") or 0)
            g["legado"] += int(item.get("legado") or 0)

    filiais_ativas = sum(1 for f in (db.listar_filiais(incluir_inativas=True) or []) if str(f.get("ativo") or "") == "1")
    itens = sorted(agregado.values(), key=lambda x: (x.get("codigo") or "", x.get("descricao") or ""))
    return {
        "total_unidades": total_unidades,
        "total_reais": total_reais,
        "total_legado": total_legado,
        "tipos_total": len([x for x in itens if int(x.get("unidades") or 0) > 0]),
        "filiais_ativas": filiais_ativas,
        "filiais_com_parque": filiais_com_parque,
        "itens": itens,
    }

def _status_kit_filial_resumo(filial, itens_estoque=None, kit=None):
    if str((filial or {}).get("ativo") or "").strip() == "0":
        return "OK"
    codigo_filial = str((filial or {}).get("codigo") or "").strip()
    enviados = [x for x in (itens_estoque if itens_estoque is not None else db.listar_itens())
               if str(x.get("filial_destino") or "").strip() == codigo_filial
               and _normalizar_exec(x.get("status")) == "enviado"]
    kit = kit if kit is not None else db.listar_kit_padrao_loja()
    if not kit:
        return "Sem kit"
    usados = set(); teve_algum = False; completo = True
    for k in kit:
        try:
            necessario = max(1, int(float(k.get("quantidade") or 1)))
        except Exception:
            necessario = 1
        candidatos = [x for x in enviados if x.get("id") not in usados and _equipamento_combina_kit(x, k)]
        for x in candidatos:
            usados.add(x.get("id"))
        teve_algum = teve_algum or bool(candidatos)
        if len(candidatos) < necessario:
            completo = False
    if completo:
        return "Completo"
    return "Parcial" if teve_algum else "Pendente"


def _data_acompanhamento_iso(valor):
    """Converte datas do Acompanhamento para ISO quando possível."""
    texto = str(valor or "").strip()
    if not texto or _normalizar_exec(texto) in {"a definir", "pendente", "sem data", "-", "n/t", "nt"}:
        return ""
    texto = texto[:10]
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y"):
        try:
            return datetime.strptime(texto, fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    return ""

def _data_acompanhamento_legivel(valor):
    iso = _data_acompanhamento_iso(valor)
    if iso:
        try:
            return datetime.strptime(iso, "%Y-%m-%d").strftime("%d/%m/%Y")
        except Exception:
            pass
    texto = str(valor or "").strip()
    return texto if texto else "A DEFINIR"


def _qtd_disponivel_num(valor):
    try:
        return float(valor or 0)
    except (TypeError, ValueError):
        return 0.0


def _pipeline_acompanhamento_expansao():
    """Fonte única do pipeline: status PENDENTE/INAUGURADA do Acompanhamento de Expansão."""
    linhas = db.listar_acompanhamento_expansao()
    pendentes = []
    inauguradas = 0
    for item in linhas:
        status = _normalizar_exec(item.get("status_filial"))
        if status == "inaugurada":
            inauguradas += 1
            continue
        if status != "pendente":
            continue
        pendentes.append({
            "id": item.get("id"),
            "codigo": str(item.get("filial") or "").strip(),
            "nome": str(item.get("descricao_filial") or "").strip(),
            "uf": str(item.get("uf") or "").strip().upper(),
            "previsao_abertura": _data_acompanhamento_iso(item.get("inauguracao")),
            "inauguracao": str(item.get("inauguracao") or "").strip(),
            "entrada_ti": str(item.get("entrada_ti") or "").strip(),
            "entrada_ti_iso": _data_acompanhamento_iso(item.get("entrada_ti")),
            "term_obra": str(item.get("term_obra") or "").strip(),
            "status_filial": str(item.get("status_filial") or "").strip().upper(),
        })
    pendentes.sort(key=lambda f: (str(f.get("previsao_abertura") or "9999-99-99"), str(f.get("codigo") or "")))
    return {
        "pendentes": pendentes,
        "pendentes_total": len(pendentes),
        "inauguradas_total": inauguradas,
        "total_acompanhado": len(linhas),
    }

def _calcular_visao_executiva(itens=None, kit=None, filiais=None, meta=None):
    def _qtd_num(valor):
        try:return int(float(valor or 0))
        except Exception:return 0
    if itens is None:
        itens=db.obter_dashboard_compacto(1).get("itens", [])
    if kit is None:
        kit=db.listar_kit_padrao_loja()
    # Filiais permanece como visão cadastral geral, mas o pipeline de inauguração
    # vem exclusivamente do Acompanhamento de Expansão.
    if filiais is None:
        filiais=db.listar_filiais(incluir_inativas=True)
    if meta is None:
        meta=db.obter_meta_lojas_expansao()
    expansao=[x for x in itens if _normalizar_exec(x.get("tipo_estoque"))=="expansao" and _qtd_num(x.get("qtde"))>0]
    req=[]
    for k in kit:
        necessario=max(1,int(k.get("quantidade") or 1))
        codigo_k=str(k.get("codigo") or "").strip()
        desc_k=_normalizar_exec(k.get("descricao"))
        disponivel=0
        for item in expansao:
            codigo_i=str(item.get("codigo") or "").strip()
            desc_i=_normalizar_exec(item.get("descricao"))
            combina=(codigo_k and codigo_i==codigo_k) or (desc_k and desc_i and (desc_k in desc_i or desc_i in desc_k))
            if combina:
                disponivel += _qtd_num(item.get("qtde"))
        lojas=disponivel//necessario
        req.append({"codigo":codigo_k,"descricao":k.get("descricao") or "","necessario":necessario,"disponivel":disponivel,"lojas":lojas})
    capacidade=min([x["lojas"] for x in req],default=0)
    pipeline = _pipeline_acompanhamento_expansao()
    planejadas = pipeline["pendentes"]
    qtd_planejada=len(planejadas)
    atendiveis=min(capacidade,qtd_planejada)
    risco=max(0,qtd_planejada-capacidade)
    pct=100.0 if qtd_planejada==0 else min(100.0,(capacidade/qtd_planejada)*100.0)
    hoje=datetime.now().date()
    horizontes={}
    for dias in (30,60,90):
        limite=hoje+timedelta(days=dias)
        dentro=[]
        for f in planejadas:
            txt=str(f.get("previsao_abertura") or "").strip()
            try: dt=datetime.strptime(txt[:10],"%Y-%m-%d").date()
            except Exception: continue
            if hoje <= dt <= limite: dentro.append(f)
        qtd=len(dentro)
        horizontes[str(dias)]={"lojas":qtd,"atendiveis":min(capacidade,qtd),"risco":max(0,qtd-capacidade)}
    sem_data=sum(1 for f in planejadas if not str(f.get("previsao_abertura") or "").strip())
    # Marcos operacionais do Acompanhamento de Expansão: Entrada de TI e Inauguração.
    # O Dashboard exibe somente datas efetivamente programadas, sem a lista de TI pendente.
    entrada_ti_programada=[]
    inauguracao_programada=[]
    for f in planejadas:
        registro={
            "id":f.get("id"),
            "codigo":f.get("codigo"),
            "nome":f.get("nome"),
            "uf":f.get("uf"),
            "entrada_ti":_data_acompanhamento_legivel(f.get("entrada_ti")),
            "entrada_ti_iso":f.get("entrada_ti_iso") or "",
            "inauguracao":_data_acompanhamento_legivel(f.get("inauguracao")),
            "inauguracao_iso":f.get("previsao_abertura") or "",
            "previsao_abertura":f.get("previsao_abertura") or "",
        }
        if f.get("entrada_ti_iso"):
            entrada_ti_programada.append(registro)
        if f.get("previsao_abertura"):
            inauguracao_programada.append(registro)
    entrada_ti_programada.sort(key=lambda x:(x.get("entrada_ti_iso") or "9999-99-99", str(x.get("codigo") or "")))
    inauguracao_programada.sort(key=lambda x:(x.get("inauguracao_iso") or "9999-99-99", str(x.get("codigo") or "")))
    deficits=[]
    for x in req:
        alvo=x["necessario"]*max(1,qtd_planejada or int(meta or 1))
        falta=max(0,alvo-x["disponivel"])
        if falta:
            deficits.append({**x,"falta":falta,"alvo":alvo})
    deficits.sort(key=lambda x:-x["falta"])
    return {
        "meta_lojas":int(meta or 10),"capacidade_lojas":capacidade,"lojas_a_inaugurar":qtd_planejada,
        "lojas_atendiveis":atendiveis,"lojas_em_risco":risco,"percentual_atendimento":round(pct,1),
        "itens_criticos":len(deficits),"estoque_expansao":sum(_qtd_num(x.get("qtde")) for x in expansao),
        "horizontes":horizontes,"sem_data":sem_data,"deficits":deficits[:8],
        "inauguradas_acompanhamento":pipeline["inauguradas_total"],
        "pendentes_inauguracao":pipeline["pendentes_total"],
        "fonte_pipeline":"acompanhamento_expansao",
        "entrada_ti_programada":entrada_ti_programada,
        "inauguracao_programada":inauguracao_programada,
        "entrada_ti_programada_total":len(entrada_ti_programada),
        "inauguracao_programada_total":len(inauguracao_programada),
        "planejadas":[{"id":f.get("id"),"codigo":f.get("codigo"),"nome":f.get("nome"),"uf":f.get("uf"),"previsao_abertura":f.get("previsao_abertura"),"situacao":"ATENDIDA" if i<capacidade else "RISCO"} for i,f in enumerate(planejadas)]
    }

def _obter_ultimo_backup_info():
    """Retorna o último backup persistido no banco para todos os usuários."""
    try:
        usuario = db.obter_configuracao("ultimo_backup_usuario")
        data_hora = db.obter_configuracao("ultimo_backup_datahora")
        arquivo = db.obter_configuracao("ultimo_backup_arquivo")
        if data_hora:
            return {"usuario": usuario or "-", "data_hora": data_hora, "arquivo": arquivo or ""}
    except Exception:
        pass
    return None

@app.route("/api/status-sistema")
@login_required
def api_status_sistema():
    try:
        saude=db.obter_saude_sistema()
    except Exception as e:
        saude={"database":"indisponível","database_ok":False,"erro":str(e),"contagens":{},"inconsistencias":{"total":0}}
    saude.update({
        "ultimo_backup":_obter_ultimo_backup_info(),
        "perfil":session.get("role") or "user",
        "usuario":session.get("username"),
        "build":APP_BUILD,
    })
    return jsonify(saude)

@app.route("/api/visao-executiva")
@login_required
def api_visao_executiva():
    return jsonify(_calcular_visao_executiva())

@app.route("/api/dashboard-resumo")
@login_required
def api_dashboard_resumo():
    """Carga compacta do Dashboard em uma única chamada HTTP."""
    agora_mono = time.monotonic()
    if request.args.get("refresh") != "1" and _DASHBOARD_CACHE["dados"] is not None and agora_mono < _DASHBOARD_CACHE["expira"]:
        resposta = jsonify(_DASHBOARD_CACHE["dados"])
        resposta.headers["Cache-Control"] = "private, max-age=10"
        resposta.headers["X-Dashboard-Cache"] = "HIT"
        return resposta
    base=db.obter_dashboard_compacto(20)
    visao=_calcular_visao_executiva(
        itens=base.get("itens") or [],
        kit=base.get("kit") or [],
        filiais=base.get("filiais") or [],
        meta=base.get("meta_lojas") or 10,
    )
    status={
        "database":"PostgreSQL" if db.IS_PG else "SQLite",
        "database_ok":True,
        "ultimo_backup":base.get("ultimo_backup"),
        "build":APP_BUILD,
    }
    dados={
        "itens":base.get("itens") or [],
        "estoque_total":base.get("estoque_total") or 0,
        "imobilizados_total":base.get("imobilizados_total") or 0,
        "produtos":base.get("produtos") or [],
        "produtos_total":base.get("produtos_total") or 0,
        "kit":base.get("kit") or [],
        "filiais_ativas":base.get("filiais_ativas") or 0,
        "filiais_pendentes_inauguracao":visao.get("pendentes_inauguracao") or 0,
        "filiais_inauguradas_acompanhamento":visao.get("inauguradas_acompanhamento") or 0,
        "meta_lojas":base.get("meta_lojas") or 10,
        "movimentacoes":base.get("movimentacoes") or [],
        "status":status,
        "visao":visao,
    }
    _DASHBOARD_CACHE["dados"] = dados
    _DASHBOARD_CACHE["expira"] = time.monotonic() + 15
    resposta=jsonify(dados)
    resposta.headers["Cache-Control"]="private, max-age=10"
    resposta.headers["X-Dashboard-Cache"]="MISS"
    return resposta

@app.route("/api/importacoes-recentes")
@admin_required
def api_importacoes_recentes():
    return jsonify(db.listar_importacoes_recentes(30))

@app.route("/gestao-dados")
@admin_page_required
def pagina_gestao_dados():
    return render_template(
        "gestao_dados.html",
        username=session.get("username"),
        role="admin",
        is_admin=True,
        pode_gerenciar_dados=True,
    )


@app.route("/api/expurgo-movimentacoes/status")
@admin_required
def api_status_expurgo_movimentacoes():
    dias = db.obter_retencao_movimentacoes()
    limite = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%d")
    antigos = db.listar_movimentacoes_anteriores(limite)
    return jsonify({
        "retencao_dias": dias,
        "data_limite": limite,
        "registros_elegiveis": len(antigos),
        "ultimo_expurgo": db.obter_configuracao("ultimo_expurgo_movimentacoes", ""),
    })


@app.route("/api/expurgo-movimentacoes/config", methods=["POST"])
@admin_required
def api_config_expurgo_movimentacoes():
    dados = request.get_json(silent=True) or request.form
    try:
        dias = int(dados.get("dias", 60))
    except (TypeError, ValueError):
        return jsonify({"erro": "Informe uma quantidade válida de dias."}), 400
    if dias < 30 or dias > 3650:
        return jsonify({"erro": "A retenção deve ficar entre 30 e 3650 dias."}), 400
    dias = db.salvar_retencao_movimentacoes(dias, session.get("username"))
    return jsonify({"ok": True, "retencao_dias": dias})


@app.route("/expurgo-movimentacoes", methods=["POST"])
@admin_required
def executar_expurgo_movimentacoes():
    dias = db.obter_retencao_movimentacoes()
    limite = (datetime.now() - timedelta(days=dias)).strftime("%Y-%m-%d")
    antigos = db.listar_movimentacoes_anteriores(limite)
    if not antigos:
        return jsonify({"erro": "Não existem movimentações anteriores ao limite configurado."}), 400

    # O Excel é concluído em memória antes da exclusão.
    wb = Workbook()
    ws = wb.active
    ws.title = "Movimentações expurgadas"
    _preencher_planilha_dict(ws, antigos)
    info = wb.create_sheet("Informações")
    info.append(["Expurgo do histórico de movimentações"])
    info.append(["Gerado em", datetime.now().strftime("%d/%m/%Y %H:%M:%S")])
    info.append(["Executado por", session.get("username") or "Administrador"])
    info.append(["Retenção", f"{dias} dias"])
    info.append(["Data limite", limite])
    info.append(["Registros arquivados", len(antigos)])

    ids = [m["id"] for m in antigos]
    excluidos = db.excluir_movimentacoes_por_ids(ids)
    agora = datetime.now().strftime("%d/%m/%Y %H:%M:%S")
    usuario = session.get("username") or "Administrador"
    db.salvar_configuracao(
        "ultimo_expurgo_movimentacoes",
        f"{agora} · {usuario} · {excluidos} registro(s) · retenção {dias} dias",
        usuario,
    )

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    nome = f"arquivo_movimentacoes_expurgadas_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    resposta = send_file(
        buf,
        as_attachment=True,
        download_name=nome,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    resposta.headers["X-Registros-Expurgados"] = str(excluidos)
    return resposta


UF_NOMES = {
    "AC":"Acre","AL":"Alagoas","AP":"Amapá","AM":"Amazonas","BA":"Bahia",
    "CE":"Ceará","DF":"Distrito Federal","ES":"Espírito Santo","GO":"Goiás",
    "MA":"Maranhão","MT":"Mato Grosso","MS":"Mato Grosso do Sul","MG":"Minas Gerais",
    "PA":"Pará","PB":"Paraíba","PR":"Paraná","PE":"Pernambuco","PI":"Piauí",
    "RJ":"Rio de Janeiro","RN":"Rio Grande do Norte","RS":"Rio Grande do Sul",
    "RO":"Rondônia","RR":"Roraima","SC":"Santa Catarina","SP":"São Paulo",
    "SE":"Sergipe","TO":"Tocantins",
}

MAPA_UF_POS = {
    "RR": (160, 75), "AP": (305, 85), "AM": (120, 165), "PA": (270, 170),
    "AC": (45, 230), "RO": (92, 220), "TO": (252, 225), "MA": (280, 175),
    "PI": (303, 194), "CE": (330, 190), "RN": (355, 195), "PB": (348, 205),
    "PE": (334, 219), "AL": (345, 233), "SE": (341, 248), "BA": (300, 255),
    "MT": (190, 233), "GO": (235, 262), "DF": (256, 256), "MS": (180, 296),
    "MG": (275, 282), "ES": (318, 288), "RJ": (307, 318), "SP": (257, 310),
    "PR": (241, 337), "SC": (248, 356), "RS": (224, 372),
}

STATUS_FILIAL_ROTULOS = {
    "ativa": "Ativa",
    "inaugurar": "Inaugurar",
    "pendente": "Pendente",
    "inativa": "Inativa",
}


def _cor_estado_pdf(estado):
    total = int(estado.get("total") or 0)
    dsp = int(estado.get("dsp") or 0)
    dpa = int(estado.get("dpa") or 0)
    if total <= 0:
        return colors.HexColor("#252D38"), None
    conhecidos = dsp + dpa
    if conhecidos <= 0:
        return colors.HexColor("#657083"), None
    if dsp > 0 and dpa == 0:
        return colors.HexColor("#3EA6FF"), None
    if dpa > 0 and dsp == 0:
        return colors.HexColor("#EF5260"), None
    return colors.HexColor("#3EA6FF"), colors.HexColor("#EF5260")


def _draw_round_label(pdf, x, y, w, h, title, value, fill="#1A2029", value_color="#FFFFFF"):
    pdf.setFillColor(colors.HexColor(fill))
    pdf.setStrokeColor(colors.HexColor("#2B3444"))
    pdf.roundRect(x, y, w, h, 10, stroke=1, fill=1)
    pdf.setFillColor(colors.HexColor("#8B96A8"))
    pdf.setFont("Helvetica", 8)
    pdf.drawString(x + 10, y + h - 14, title.upper())
    pdf.setFillColor(colors.HexColor(value_color))
    pdf.setFont("Helvetica-Bold", 18)
    pdf.drawString(x + 10, y + 12, str(value))


def _draw_store_icon(pdf, x, y, scale=1.0, accent="#3EA6FF"):
    aw = 34 * scale
    ah = 22 * scale
    pdf.setStrokeColor(colors.HexColor("#203040"))
    pdf.setLineWidth(1)
    pdf.setFillColor(colors.HexColor("#EAF4FF"))
    pdf.roundRect(x, y, aw, ah, 4 * scale, stroke=1, fill=1)
    pdf.setFillColor(colors.HexColor(accent))
    pdf.rect(x - 1 * scale, y + ah - 8 * scale, aw + 2 * scale, 8 * scale, stroke=0, fill=1)
    pdf.setFillColor(colors.white)
    stripe_w = (aw + 2 * scale) / 5.0
    for i in range(5):
        if i % 2 == 0:
            pdf.rect(x - 1 * scale + i * stripe_w, y + ah - 8 * scale, stripe_w, 8 * scale, stroke=0, fill=1)
    pdf.setFillColor(colors.HexColor("#D9E9F7"))
    pdf.rect(x + 4 * scale, y + 4 * scale, 8 * scale, 9 * scale, stroke=0, fill=1)
    pdf.setFillColor(colors.HexColor("#C8DDF0"))
    pdf.rect(x + 17 * scale, y + 4 * scale, 12 * scale, 12 * scale, stroke=0, fill=1)


def _draw_state_tile_pdf(pdf, cx, cy, estado, scale=1.0):
    w = 30 * scale
    h = 22 * scale
    x = cx - (w / 2.0)
    y = cy - (h / 2.0)
    c1, c2 = _cor_estado_pdf(estado)
    pdf.saveState()
    path = pdf.beginPath()
    path.roundRect(x, y, w, h, 4)
    pdf.clipPath(path, stroke=0, fill=0)
    if c2 is None:
        pdf.setFillColor(c1)
        pdf.rect(x, y, w, h, stroke=0, fill=1)
    else:
        conhecidos = max(1, int(estado.get("dsp") or 0) + int(estado.get("dpa") or 0))
        split = w * (int(estado.get("dsp") or 0) / conhecidos)
        pdf.setFillColor(c1)
        pdf.rect(x, y, split, h, stroke=0, fill=1)
        pdf.setFillColor(c2)
        pdf.rect(x + split, y, w - split, h, stroke=0, fill=1)
    pdf.restoreState()
    pdf.setStrokeColor(colors.HexColor("#E7F3FF"))
    pdf.setLineWidth(0.7)
    pdf.roundRect(x, y, w, h, 4, stroke=1, fill=0)
    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", max(6, 7.4 * scale))
    pdf.drawCentredString(cx, y + h - (7.8 * scale), str(estado.get("uf") or ""))
    pdf.setFont("Helvetica-Bold", max(4.5, 5.4 * scale))
    pdf.setFillColor(colors.HexColor("#E8FFF3"))
    pdf.drawRightString(cx - 1.5 * scale, y + 4.3 * scale, str(int(estado.get('ativa') or 0)))
    pdf.setFillColor(colors.HexColor("#FFFFFF"))
    pdf.drawCentredString(cx, y + 4.2 * scale, "|")
    pdf.setFillColor(colors.HexColor("#E0F1FF"))
    pdf.drawString(cx + 1.5 * scale, y + 4.3 * scale, str(int(estado.get('inaugurar') or 0)))


def _gerar_pdf_projecao_lojas(dados):
    estados = dados.get("estados") or []
    totais = dados.get("totais") or {}
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=landscape(A4))
    larg, alt = landscape(A4)
    margem = 14 * mm
    area_w = larg - 2 * margem

    estados_validos = [e for e in estados if int(e.get("total") or 0) > 0]
    estados_ordenados = sorted(estados_validos, key=lambda e: (-int(e.get("total") or 0), e.get("estado") or ""))
    total_base = max(1, int(totais.get("total_geral") or 0) or sum(int(e.get("total") or 0) for e in estados_validos) or 1)
    top_estado = estados_ordenados[0] if estados_ordenados else None
    top5_total = sum(int(e.get("total") or 0) for e in estados_ordenados[:5])
    cobertura = len(estados_validos)
    taxa_ativas = (int(totais.get("ativa") or 0) / total_base) * 100.0

    def _footer(page_no):
        pdf.setStrokeColor(colors.HexColor("#253241"))
        pdf.line(margem, 10 * mm, larg - margem, 10 * mm)
        pdf.setFillColor(colors.HexColor("#8EA1B4"))
        pdf.setFont("Helvetica", 7.5)
        pdf.drawString(margem, 6.5 * mm, "© 2026 · Developed by ALM - Expansão de TI · Relatório executivo de projeção de abertura e lojas")
        pdf.drawRightString(larg - margem, 6.5 * mm, f"Página {page_no}")

    def _panel(x, y, w, h, title=None, subtitle=None, radius=12):
        pdf.setFillColor(colors.HexColor("#151D27"))
        pdf.setStrokeColor(colors.HexColor("#2A3645"))
        pdf.roundRect(x, y, w, h, radius, stroke=1, fill=1)
        if title:
            pdf.setFillColor(colors.white)
            pdf.setFont("Helvetica-Bold", 12)
            pdf.drawString(x + 12, y + h - 20, title)
        if subtitle:
            pdf.setFillColor(colors.HexColor("#91A7BD"))
            pdf.setFont("Helvetica", 8.2)
            pdf.drawString(x + 12, y + h - 33, subtitle)

    def _kpi_card(x, y, w, h, titulo, valor, cor, detalhe):
        pdf.setFillColor(colors.HexColor("#182230"))
        pdf.setStrokeColor(colors.HexColor("#314255"))
        pdf.roundRect(x, y, w, h, 11, stroke=1, fill=1)
        pdf.setFillColor(colors.HexColor("#9AB0C5"))
        pdf.setFont("Helvetica", 7.4)
        pdf.drawString(x + 10, y + h - 13, titulo.upper())
        pdf.setFillColor(colors.HexColor(cor))
        pdf.setFont("Helvetica-Bold", 17)
        pdf.drawString(x + 10, y + 19, str(valor))
        pdf.setFillColor(colors.HexColor("#7F95AA"))
        pdf.setFont("Helvetica", 6.8)
        pdf.drawString(x + 10, y + 8, detalhe)

    def _summary_row(x, y, w, label, value, color="#FFFFFF"):
        pdf.setFillColor(colors.HexColor("#1B2531"))
        pdf.roundRect(x, y, w, 22, 7, stroke=0, fill=1)
        pdf.setFillColor(colors.HexColor("#A0B5C9"))
        pdf.setFont("Helvetica", 8.2)
        pdf.drawString(x + 10, y + 7.5, label)
        pdf.setFillColor(colors.HexColor(color))
        pdf.setFont("Helvetica-Bold", 10.5)
        pdf.drawRightString(x + w - 10, y + 7.5, str(value))

    def _bullet_line(x, y, label, value, color="#DCE8F5"):
        pdf.setFillColor(colors.HexColor(color))
        pdf.circle(x + 3, y + 2.5, 2, stroke=0, fill=1)
        pdf.setFillColor(colors.HexColor("#DDE7F1"))
        pdf.setFont("Helvetica", 8)
        pdf.drawString(x + 10, y, label)
        pdf.setFont("Helvetica-Bold", 8.2)
        pdf.drawRightString(x + 198, y, str(value))

    def _state_bar_row(x, y, w, nome, total, ativa, inaugurar, pct, idx):
        h = 26
        fill = "#16212E" if idx % 2 == 0 else "#141D29"
        pdf.setFillColor(colors.HexColor(fill))
        pdf.roundRect(x, y, w, h, 7, stroke=0, fill=1)
        pdf.setFillColor(colors.white)
        pdf.setFont("Helvetica-Bold", 8.6)
        nome_curto = nome if len(nome) <= 28 else nome[:25] + "..."
        pdf.drawString(x + 8, y + 16, nome_curto)
        pdf.setFillColor(colors.HexColor("#8CA2B7"))
        pdf.setFont("Helvetica", 7)
        pdf.drawString(x + 8, y + 7, f"Total {total} · Ativas {ativa} · Inaugurar {inaugurar}")
        track_x = x + 150
        track_w = w - 215
        pdf.setFillColor(colors.HexColor("#0D1721"))
        pdf.roundRect(track_x, y + 8, track_w, 10, 5, stroke=0, fill=1)
        pdf.setFillColor(colors.HexColor("#4FB2FF"))
        pdf.roundRect(track_x, y + 8, max(6, track_w * max(0, min(1, pct / 100.0))), 10, 5, stroke=0, fill=1)
        pdf.setFillColor(colors.white)
        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawRightString(x + w - 8, y + 11, f"{pct:.1f}%")

    def _mini_brand_card(x, y, w, h, titulo, valor, pct, color_hex):
        pdf.setFillColor(colors.HexColor("#1B2531"))
        pdf.roundRect(x, y, w, h, 8, stroke=0, fill=1)
        titulo_exib = titulo if len(str(titulo)) <= 10 else "Sem band."
        title_font = 7.0 if len(titulo_exib) > 8 else 7.8
        pdf.setFillColor(colors.HexColor("#8EA2B6"))
        pdf.setFont("Helvetica-Bold", title_font)
        pdf.drawCentredString(x + (w/2), y + h - 10, titulo_exib)
        pdf.setFillColor(colors.HexColor(color_hex))
        pdf.setFont("Helvetica-Bold", 10.8)
        pdf.drawString(x + 8, y + 14, str(valor))
        pdf.setFillColor(colors.HexColor("#D9E4EF"))
        pdf.setFont("Helvetica-Bold", 7.0)
        pdf.drawRightString(x + w - 8, y + 14, f"{pct:.1f}%")
        pdf.setFillColor(colors.HexColor("#0D1721"))
        pdf.roundRect(x + 8, y + 5, w - 16, 4, 2, stroke=0, fill=1)
        barra = (w - 16) * max(0, min(1, pct / 100.0))
        if barra > 0:
            pdf.setFillColor(colors.HexColor(color_hex))
            pdf.roundRect(x + 8, y + 5, max(6, barra), 4, 2, stroke=0, fill=1)

    def _pill(x, y, text, accent="#FFB648"):
        tw = pdf.stringWidth(text, "Helvetica-Bold", 7.2)
        w = tw + 18
        pdf.setFillColor(colors.HexColor("#101923"))
        pdf.setStrokeColor(colors.HexColor("#314255"))
        pdf.roundRect(x, y, w, 16, 8, stroke=1, fill=1)
        pdf.setFillColor(colors.HexColor(accent))
        pdf.setFont("Helvetica-Bold", 7.2)
        pdf.drawString(x + 9, y + 5, text)
        return w

    def _draw_page_bg():
        pdf.setFillColor(colors.HexColor("#0F1620"))
        pdf.rect(0, 0, larg, alt, stroke=0, fill=1)

    # ============================
    # Página 1 — Visão executiva
    # ============================
    _draw_page_bg()
    pdf.setTitle("Projecao de abertura e lojas")
    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 21)
    pdf.drawString(margem, alt - margem, "Projeção de abertura e lojas")
    pdf.setFillColor(colors.HexColor("#9DB3C8"))
    pdf.setFont("Helvetica", 9)
    pdf.drawString(margem, alt - margem - 14, "Relatório executivo com visão consolidada das lojas cadastradas na aba Filiais.")
    pdf.drawRightString(larg - margem, alt - margem - 14, datetime.now().strftime("Gerado em %d/%m/%Y às %H:%M"))

    kpi_y = alt - margem - 66
    gap = 8
    card_w = (area_w - gap * 5) / 6.0
    card_h = 50
    kpis = [
        ("Total geral", totais.get("total_geral", 0), "#FFFFFF", "Base operacional"),
        ("Lojas ativas", totais.get("ativa", 0), "#52D69A", "Em operação"),
        ("A inaugurar", totais.get("inaugurar", 0), "#5AB4FF", "Planejadas"),
        ("Pendentes", totais.get("pendente", 0), "#FFBE55", "Aguardando definição"),
        ("DSP", totais.get("dsp", 0), "#63BAFF", "Bandeira azul"),
        ("DPA", totais.get("dpa", 0), "#FF7E88", "Bandeira vermelha"),
    ]
    for i, (titulo, valor, cor, detalhe) in enumerate(kpis):
        _kpi_card(margem + i * (card_w + gap), kpi_y, card_w, card_h, titulo, valor, cor, detalhe)

    content_y = 52
    content_h = kpi_y - 18 - content_y
    left_w = 510
    right_gap = 12
    right_x = margem + left_w + right_gap
    right_w = larg - margem - right_x

    # Painel esquerdo principal
    _panel(margem, content_y, left_w, content_h, "Participação percentual por estado", "Leitura dos estados com maior concentração de lojas na projeção.")
    pdf.setFillColor(colors.HexColor("#0F1924"))
    pdf.roundRect(margem + left_w - 126, content_y + content_h - 28, 112, 17, 7, stroke=0, fill=1)
    pdf.setFillColor(colors.HexColor("#E2ECF7"))
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawCentredString(margem + left_w - 70, content_y + content_h - 22, f"Base total: {total_base} loja(s)")

    pdf.setFillColor(colors.HexColor("#A5B8CA"))
    pdf.setFont("Helvetica", 8)
    pdf.drawString(margem + 12, content_y + content_h - 48, "Top 10 estados por participação no total de lojas.")
    row_y = content_y + content_h - 82
    row_h = 30
    for idx, e in enumerate(estados_ordenados[:10]):
        pct = (int(e.get("total") or 0) / total_base) * 100.0
        _state_bar_row(margem + 12, row_y - idx * row_h, left_w - 24, f"{e.get('estado')} ({e.get('uf')})", int(e.get("total") or 0), int(e.get("ativa") or 0), int(e.get("inaugurar") or 0), pct, idx)

    # Insights executivos no rodapé do painel esquerdo
    insight_y = content_y + 18
    insight_w = (left_w - 24 - 3 * 8) / 4.0
    top_inauguracao = max(estados_ordenados, key=lambda e: int(e.get("inaugurar") or 0), default=None)
    insights = [
        ("Cobertura nacional", f"{cobertura}/27", f"{(cobertura/27)*100:.1f}% das UFs com lojas", "#63BAFF"),
        ("Operação ativa", f"{taxa_ativas:.1f}%", f"{totais.get('ativa',0)} lojas ativas", "#52D69A"),
        ("Maior presença", f"{top_estado.get('uf') if top_estado else '-'} · {top_estado.get('total') if top_estado else 0}", f"{top_estado.get('estado') if top_estado else 'Sem dados'} lidera a base", "#FFBE55"),
        ("Maior inauguração", f"{top_inauguracao.get('uf') if top_inauguracao else '-'} · {top_inauguracao.get('inaugurar') if top_inauguracao else 0}", f"{top_inauguracao.get('estado') if top_inauguracao else 'Sem dados'} possui mais inaugurações", "#B197FC"),
    ]
    for i, (titulo, valor, detalhe, cor) in enumerate(insights):
        x = margem + 12 + i * (insight_w + 8)
        pdf.setFillColor(colors.HexColor("#182230"))
        pdf.roundRect(x, insight_y, insight_w, 58, 9, stroke=0, fill=1)
        pdf.setFillColor(colors.HexColor("#8FA3B8"))
        pdf.setFont("Helvetica", 7.2)
        pdf.drawString(x + 9, insight_y + 43, titulo.upper())
        pdf.setFillColor(colors.HexColor(cor))
        pdf.setFont("Helvetica-Bold", 13)
        pdf.drawString(x + 9, insight_y + 25, str(valor))
        pdf.setFillColor(colors.HexColor("#D7E2EE"))
        pdf.setFont("Helvetica", 7.2)
        pdf.drawString(x + 9, insight_y + 10, detalhe[:36])

    # Painel direito
    _panel(right_x, content_y, right_w, content_h)
    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 13)
    pdf.drawString(right_x + 12, content_y + content_h - 20, "Resumo executivo")

    cursor = content_y + content_h - 50
    for rot, val, cor in [
        ("Estados com lojas", cobertura, "#FFFFFF"),
        ("Ativas + inaugurar", int(totais.get("ativa", 0)) + int(totais.get("inaugurar", 0)), "#52D69A"),
        ("Pendentes", totais.get("pendente", 0), "#FFBE55"),
        ("Sem bandeira", totais.get("sem_bandeira", 0), "#A7B5C4"),
    ]:
        _summary_row(right_x + 12, cursor, right_w - 24, rot, val, cor)
        cursor -= 27

    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(right_x + 12, cursor - 4, "Mensagem executiva")
    cursor -= 18
    _bullet_line(right_x + 12, cursor, "Base considerada", totais.get("total_geral", 0))
    cursor -= 14
    _bullet_line(right_x + 12, cursor, f"Estado líder: {top_estado.get('uf') if top_estado else '-'}", f"{(int(top_estado.get('total') or 0)/total_base*100):.1f}%" if top_estado else "0%", "#63BAFF")
    cursor -= 14
    _bullet_line(right_x + 12, cursor, "Cobertura nacional", f"{cobertura} UF(s)", "#52D69A")
    cursor -= 14
    _bullet_line(right_x + 12, cursor, "Taxa de operação ativa", f"{taxa_ativas:.1f}%", "#FFBE55")
    cursor -= 24

    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(right_x + 12, cursor, "Top 5 estados")
    cursor -= 16
    for i, e in enumerate(estados_ordenados[:5], start=1):
        pct = (int(e.get("total") or 0) / total_base) * 100.0
        pdf.setFillColor(colors.HexColor("#1B2531"))
        pdf.roundRect(right_x + 12, cursor - 11, right_w - 24, 18, 6, stroke=0, fill=1)
        pdf.setFillColor(colors.HexColor("#DCE7F2"))
        pdf.setFont("Helvetica", 8)
        nome = f"{i}. {e.get('estado')} ({e.get('uf')})"
        pdf.drawString(right_x + 18, cursor, nome[:31])
        pdf.drawRightString(right_x + right_w - 18, cursor, f"{pct:.1f}% · {int(e.get('total') or 0)}")
        cursor -= 20

    cursor -= 2
    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 11)
    pdf.drawString(right_x + 12, cursor, "Composição por bandeira")
    cursor -= 42
    total_marcas = max(1, int(totais.get("dsp", 0)) + int(totais.get("dpa", 0)) + int(totais.get("sem_bandeira", 0)))
    brand_w = (right_w - 24 - 2 * 8) / 3.0
    brands = [
        ("DSP", totais.get("dsp", 0), (int(totais.get("dsp", 0)) / total_marcas) * 100.0, "#63BAFF"),
        ("DPA", totais.get("dpa", 0), (int(totais.get("dpa", 0)) / total_marcas) * 100.0, "#FF7E88"),
        ("Sem bandeira", totais.get("sem_bandeira", 0), (int(totais.get("sem_bandeira", 0)) / total_marcas) * 100.0, "#9AAABA"),
    ]
    for i, (titulo, valor, pct, cor) in enumerate(brands):
        _mini_brand_card(right_x + 12 + i * (brand_w + 8), cursor, brand_w, 34, titulo, valor, pct, cor)

    _footer(1)

    # ============================
    # Página 2 — Detalhamento executivo
    # ============================
    pdf.showPage()
    _draw_page_bg()
    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 18)
    pdf.drawString(margem, alt - margem, "Projeção detalhada por estado")
    pdf.setFillColor(colors.HexColor("#9DB3C8"))
    pdf.setFont("Helvetica", 9)
    pdf.drawString(margem, alt - margem - 14, "Tabela consolidada com percentual de participação, status operacional e distribuição por bandeira.")
    pdf.drawRightString(larg - margem, alt - margem - 14, datetime.now().strftime("Atualizado em %d/%m/%Y às %H:%M"))

    # Faixa de leitura executiva
    band_y = alt - margem - 64
    band_h = 48
    _panel(margem, band_y, area_w, band_h, radius=10)
    exec_msg = f"A projeção atual contempla {totais.get('total_geral',0)} loja(s), com {totais.get('ativa',0)} ativa(s), {totais.get('inaugurar',0)} a inaugurar e {totais.get('pendente',0)} pendente(s)."
    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 11.5)
    pdf.drawString(margem + 12, band_y + 28, "Leitura executiva")
    pdf.setFillColor(colors.HexColor("#C7D5E3"))
    pdf.setFont("Helvetica", 8.6)
    pdf.drawString(margem + 12, band_y + 14, exec_msg)
    if top_estado:
        pdf.drawString(margem + 12, band_y + 4, f"Maior presença: {top_estado.get('estado')} ({top_estado.get('uf')}) com {top_estado.get('total')} loja(s), representando {(int(top_estado.get('total') or 0)/total_base)*100:.1f}% da base.")

    cols = [("UF", 24), ("Estado", 112), ("%", 36), ("Total", 40), ("Ativas", 44), ("Inaug.", 48), ("Pend.", 46), ("DSP", 36), ("DPA", 36), ("Sem", 40)]
    table_x = margem
    table_y = band_y - 26
    row_h = 18
    table_w = sum(w for _, w in cols)

    def _draw_table_header(ypos):
        pdf.setFillColor(colors.HexColor("#234C74"))
        pdf.roundRect(table_x, ypos, table_w, row_h, 5, stroke=0, fill=1)
        pdf.setFillColor(colors.white)
        pdf.setFont("Helvetica-Bold", 8)
        cx = table_x
        for title, w in cols:
            pdf.drawString(cx + 4, ypos + 6, title)
            cx += w

    _draw_table_header(table_y)
    y = table_y - row_h - 2
    alterna = False
    page_no = 2
    for e in estados_ordenados:
        if y < 18 * mm:
            _footer(page_no)
            pdf.showPage()
            page_no += 1
            _draw_page_bg()
            pdf.setFillColor(colors.white)
            pdf.setFont("Helvetica-Bold", 15)
            pdf.drawString(margem, alt - margem, "Projeção detalhada por estado · continuação")
            pdf.setFillColor(colors.HexColor("#9DB3C8"))
            pdf.setFont("Helvetica", 8.5)
            pdf.drawString(margem, alt - margem - 14, "Continuação da tabela consolidada por estado.")
            table_y = alt - margem - 40
            _draw_table_header(table_y)
            y = table_y - row_h - 2
            alterna = False
        fill = "#19222D" if alterna else "#141D28"
        alterna = not alterna
        pdf.setFillColor(colors.HexColor(fill))
        pdf.roundRect(table_x, y, table_w, row_h, 3, stroke=0, fill=1)
        pct = (int(e.get("total") or 0) / total_base) * 100.0
        vals = [
            e.get("uf"), e.get("estado"), f"{pct:.1f}%", e.get("total"), e.get("ativa"), e.get("inaugurar"),
            e.get("pendente"), e.get("dsp"), e.get("dpa"), e.get("sem_bandeira")
        ]
        cx = table_x
        for idx, ((_, w), val) in enumerate(zip(cols, vals)):
            if idx == 2:
                pdf.setFillColor(colors.HexColor("#74BFFF"))
            elif idx == 4:
                pdf.setFillColor(colors.HexColor("#52D69A"))
            elif idx == 5:
                pdf.setFillColor(colors.HexColor("#63BAFF"))
            elif idx == 6:
                pdf.setFillColor(colors.HexColor("#FFBE55"))
            elif idx == 8:
                pdf.setFillColor(colors.HexColor("#FF7E88"))
            else:
                pdf.setFillColor(colors.HexColor("#DCE7F2"))
            pdf.setFont("Helvetica", 7.7)
            if idx >= 2:
                pdf.drawRightString(cx + w - 4, y + 6, str(val))
            else:
                label = str(val)
                if idx == 1 and len(label) > 23:
                    label = label[:20] + "..."
                pdf.drawString(cx + 4, y + 6, label)
            cx += w
        y -= row_h + 2

    _footer(page_no)
    pdf.save()
    buf.seek(0)
    return buf


def _status_filial_normalizado(valor):
    valor = str(valor or "").strip().lower()
    if valor in ("1", "ativa", "ativo", "true"):
        return "ativa"
    if valor == "inaugurar":
        return "inaugurar"
    if valor == "pendente":
        return "pendente"
    return "inativa"

def _dados_projecao_lojas():
    filiais = db.listar_filiais(incluir_inativas=True)
    por_uf = {}
    totais = {
        "ativa": 0, "inaugurar": 0, "pendente": 0, "inativa": 0,
        "dsp": 0, "dpa": 0, "sem_bandeira": 0, "total_geral": 0,
    }
    for f in filiais:
        status = _status_filial_normalizado(f.get("ativo"))
        bandeira = str(f.get("bandeira") or "").strip().upper()
        uf = str(f.get("uf") or "").strip().upper()[:2]
        totais[status] += 1
        if status != "inativa":
            totais["total_geral"] += 1
            if bandeira == "DSP":
                totais["dsp"] += 1
            elif bandeira == "DPA":
                totais["dpa"] += 1
            else:
                totais["sem_bandeira"] += 1
        if uf not in UF_NOMES:
            continue
        linha = por_uf.setdefault(uf, {
            "uf": uf, "estado": UF_NOMES[uf],
            "ativa": 0, "inaugurar": 0, "pendente": 0, "inativa": 0,
            "dsp": 0, "dpa": 0, "sem_bandeira": 0, "total": 0,
        })
        linha[status] += 1
        if status != "inativa":
            linha["total"] += 1
            if bandeira == "DSP":
                linha["dsp"] += 1
            elif bandeira == "DPA":
                linha["dpa"] += 1
            else:
                linha["sem_bandeira"] += 1
    estados = []
    for uf, nome in UF_NOMES.items():
        estados.append(por_uf.get(uf, {
            "uf": uf, "estado": nome,
            "ativa": 0, "inaugurar": 0, "pendente": 0, "inativa": 0,
            "dsp": 0, "dpa": 0, "sem_bandeira": 0, "total": 0,
        }))
    estados.sort(key=lambda x: (-x["total"], x["estado"]))
    return {"totais": totais, "estados": estados, "filiais": filiais}

@app.route("/api/projecao-lojas")
@login_required
def api_projecao_lojas():
    dados = _dados_projecao_lojas()
    return jsonify({"totais": dados["totais"], "estados": dados["estados"]})


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
    projecao = _dados_projecao_lojas()
    fontes=[
        ("Estoque",db.listar_itens()),
        ("Imobilizados",db.listar_imobilizados()),
        ("Produtos",db.listar_produtos()),
        ("Filiais",projecao["filiais"]),
        ("Acomp. Expansão",db.listar_acompanhamento_expansao()),
        ("Projeção por UF",projecao["estados"]),
        ("Kit padrão",db.listar_kit_padrao_loja()),
        ("Movimentações",db.listar_todas_movimentacoes()),
    ]
    for nome,dados in fontes:
        ws=wb.create_sheet(nome[:31])
        _preencher_planilha_dict(ws,dados)
    ws_resumo=wb.create_sheet("Resumo Lojas")
    totais=projecao["totais"]
    _preencher_planilha_dict(ws_resumo,[{
        "Lojas ativas":totais["ativa"],
        "A inaugurar":totais["inaugurar"],
        "Pendentes":totais["pendente"],
        "Inativas":totais["inativa"],
        "Total geral":totais["total_geral"],
        "DSP":totais["dsp"],
        "DPA":totais["dpa"],
        "Sem bandeira":totais["sem_bandeira"],
    }])
    return wb


@app.route("/export-consolidado")
@role_required("admin", "gestor", "operador")
def exportar_consolidado():
    wb=_workbook_consolidado()
    buf=io.BytesIO(); wb.save(buf); buf.seek(0)
    return send_file(buf,as_attachment=True,download_name=f"relatorio_consolidado_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/export-projecao-lojas")
@role_required("admin", "gestor", "operador")
def exportar_projecao_lojas():
    dados = _dados_projecao_lojas()
    wb = Workbook()
    ws = wb.active
    ws.title = "Resumo Geral"
    t = dados["totais"]
    resumo = [
        ("Indicador", "Quantidade"),
        ("Lojas ativas", t["ativa"]),
        ("Lojas a inaugurar", t["inaugurar"]),
        ("Lojas pendentes", t["pendente"]),
        ("Lojas inativas", t["inativa"]),
        ("Total geral", t["total_geral"]),
        ("DSP", t["dsp"]),
        ("DPA", t["dpa"]),
        ("Sem bandeira definida", t["sem_bandeira"]),
    ]
    for row in resumo:
        ws.append(row)
    for c in ws[1]:
        c.font=Font(bold=True,color="FFFFFF"); c.fill=PatternFill("solid",fgColor="1F4E78")
    ws.column_dimensions["A"].width=30; ws.column_dimensions["B"].width=18

    ws_uf = wb.create_sheet("Por UF")
    _preencher_planilha_dict(ws_uf, dados["estados"], "Projeção de abertura e lojas por estado")

    ws_filiais = wb.create_sheet("Filiais")
    linhas=[]
    rotulos={"1":"Ativa","0":"Inativa","inaugurar":"Inaugurar","pendente":"Pendente"}
    for f in dados["filiais"]:
        x=dict(f)
        x["status"] = rotulos.get(str(f.get("ativo") or ""), str(f.get("ativo") or ""))
        linhas.append(x)
    _preencher_planilha_dict(ws_filiais, linhas, "Cadastro de filiais usado na projeção")

    buf=io.BytesIO(); wb.save(buf); buf.seek(0)
    return send_file(
        buf, as_attachment=True,
        download_name=f"projecao_abertura_lojas_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/export-projecao-lojas-pdf")
@role_required("admin", "gestor", "operador")
def exportar_projecao_lojas_pdf():
    dados = _dados_projecao_lojas()
    pdf_buffer = _gerar_pdf_projecao_lojas(dados)
    return send_file(
        pdf_buffer,
        as_attachment=True,
        download_name=f"projecao_abertura_lojas_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf",
        mimetype="application/pdf",
    )


@app.route("/export-movimentacoes")
@role_required("admin", "gestor", "operador")
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
        tabela=m.get("tabela") or "itens"
        if tabela == "sistema":
            m["codigo"]="META LOJAS"
            m["descricao"]="Meta do lote de inauguração"
        else:
            ref=(imobs if tabela=="imobilizados" else itens).get(str(m.get("item_id")),{})
            m["codigo"]=ref.get("codigo","")
            m["descricao"]=ref.get("descricao","")
    wb=Workbook(); ws=wb.active; ws.title="Movimentações"; _preencher_planilha_dict(ws,movs)
    buf=io.BytesIO(); wb.save(buf); buf.seek(0)
    faixa=f"_{inicio or 'inicio'}_{fim or 'hoje'}"
    return send_file(buf,as_attachment=True,download_name=f"movimentacoes{faixa}.xlsx",mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/backup")
@role_required("admin", "gestor", "operador")
def gerar_backup():
    agora = datetime.now()
    usuario = session.get("username") or "Usuário"
    data_hora = agora.strftime("%d/%m/%Y %H:%M")
    nome_arquivo = f"backup_controle_estoque_{agora.strftime('%Y%m%d_%H%M')}.zip"
    mem=io.BytesIO()
    with zipfile.ZipFile(mem,"w",zipfile.ZIP_DEFLATED) as z:
        wb=_workbook_consolidado()
        x=io.BytesIO(); wb.save(x); x.seek(0)
        z.writestr("estoque_backup.xlsx",x.read())
        if not db.IS_PG and os.path.exists(db.SQLITE_PATH):
            z.write(db.SQLITE_PATH,arcname="estoque.db")
        z.writestr("LEIA-ME.txt",f"Backup gerado em {data_hora} por {usuario}.\nContém Estoque, Imobilizados, Produtos, Filiais, Projeção por UF, Kit padrão e Histórico de movimentações.\n")

    # Registro persistente: permanece disponível após logout, novo login ou reinício da aplicação.
    db.salvar_configuracao("ultimo_backup_usuario", usuario, usuario)
    db.salvar_configuracao("ultimo_backup_datahora", data_hora, usuario)
    db.salvar_configuracao("ultimo_backup_arquivo", nome_arquivo, usuario)
    session["ultimo_backup"] = data_hora  # compatibilidade com versões anteriores

    mem.seek(0)
    resposta = send_file(mem,as_attachment=True,download_name=nome_arquivo,mimetype="application/zip")
    resposta.headers["X-Backup-Usuario"] = usuario
    resposta.headers["X-Backup-DataHora"] = data_hora
    return resposta


@app.route("/produtos")
@login_required
def pagina_produtos():
    return render_template("produtos.html", username=session.get("username"), role=session.get("role") or "user", is_admin=session.get("role") == "admin")


@app.route("/orcamento")
@page_role_required("admin", "gestor", "operador")
def pagina_orcamento():
    role = session.get("role") or "user"
    if role == "user":
        role = "operador"
    return render_template(
        "orcamento.html",
        username=session.get("username"),
        role=role,
        is_admin=role == "admin",
        pode_gerenciar_orcamento=role in ("admin", "gestor", "operador"),
    )


@app.route("/filiais")
@login_required
def pagina_filiais():
    return render_template("filiais.html", username=session.get("username"), role=session.get("role") or "user", is_admin=session.get("role") == "admin")


@app.route("/filiais/<int:filial_id>")
@login_required
def pagina_filial_detalhe(filial_id):
    filial = db.buscar_filial_por_id(filial_id)
    if not filial:
        return redirect(url_for("pagina_filiais"))

    kit_vinculado = _kit_vinculado_filial(filial)
    total_vinculado = sum(int(x.get("vinculado") or 0) for x in kit_vinculado)
    total_rastreado = sum(int(x.get("reais") or 0) for x in kit_vinculado)
    total_legado = sum(int(x.get("legado") or 0) for x in kit_vinculado)
    return render_template(
        "filial_detalhe.html",
        filial=filial,
        kit_vinculado=kit_vinculado,
        total_vinculado=total_vinculado,
        total_rastreado=total_rastreado,
        total_legado=total_legado,
        username=session.get("username"), role=session.get("role") or "user",
        is_admin=session.get("role") == "admin",
    )


@app.route("/equipamentos-parque")
@login_required
def pagina_equipamentos_parque():
    return render_template(
        "equipamentos_parque.html",
        dados=_dados_equipamentos_parque(),
        username=session.get("username"), role=session.get("role") or "user",
        is_admin=session.get("role") == "admin",
    )


@app.route("/export-equipamentos-parque")
@login_required
def exportar_equipamentos_parque_excel():
    dados = _dados_equipamentos_parque()
    wb = Workbook()
    ws = wb.active
    ws.title = "Equipamentos no Parque"
    ws.append(["EQUIPAMENTOS NO PARQUE - VISÃO GERAL"])
    ws.append(["Total no parque", dados["total_unidades"]])
    ws.append(["Tipos de equipamento", dados["tipos_total"]])
    ws.append(["Filiais ativas", dados["filiais_ativas"]])
    ws.append(["Unidades rastreadas", dados["total_reais"]])
    ws.append(["Unidades legado", dados["total_legado"]])
    ws.append([])
    ws.append(["Código", "Equipamento", "Total no parque", "Rastreado", "Legado", "Filiais com o item"])
    for item in dados["itens"]:
        ws.append([
            item.get("codigo"), item.get("descricao"), int(item.get("unidades") or 0),
            int(item.get("reais") or 0), int(item.get("legado") or 0), int(item.get("filiais") or 0),
        ])
    for cell in ws[1]:
        cell.font = Font(bold=True, size=14)
    for cell in ws[8]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="D9EAF7")
    ws.freeze_panes = "A9"
    for col, largura in {"A":18,"B":42,"C":18,"D":16,"E":16,"F":20}.items():
        ws.column_dimensions[col].width = largura
    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    return send_file(
        buf, as_attachment=True,
        download_name=f"equipamentos_parque_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/export-equipamentos-parque-pdf")
@login_required
def exportar_equipamentos_parque_pdf():
    dados = _dados_equipamentos_parque()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4), rightMargin=12*mm, leftMargin=12*mm, topMargin=12*mm, bottomMargin=12*mm)
    estilos = getSampleStyleSheet()
    titulo = ParagraphStyle("tituloParque", parent=estilos["Heading1"], fontSize=16, leading=19, spaceAfter=8)
    normal = ParagraphStyle("normalParque", parent=estilos["BodyText"], fontSize=8.5, leading=11)
    story = [
        Paragraph("Equipamentos no Parque — Visão Geral", titulo),
        Paragraph(
            f"Total no parque: <b>{dados['total_unidades']}</b> · Tipos: <b>{dados['tipos_total']}</b> · "
            f"Filiais ativas: <b>{dados['filiais_ativas']}</b> · Rastreado: <b>{dados['total_reais']}</b> · "
            f"Legado: <b>{dados['total_legado']}</b>", normal,
        ), Spacer(1, 6*mm)
    ]
    linhas = [["Código","Equipamento","Total","Rastreado","Legado","Filiais"]]
    for item in dados["itens"]:
        linhas.append([
            str(item.get("codigo") or "-"), str(item.get("descricao") or "-"),
            str(int(item.get("unidades") or 0)), str(int(item.get("reais") or 0)),
            str(int(item.get("legado") or 0)), str(int(item.get("filiais") or 0)),
        ])
    tabela = Table(linhas, repeatRows=1, colWidths=[28*mm,88*mm,25*mm,25*mm,25*mm,24*mm])
    tabela.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#D9EAF7")),
        ("TEXTCOLOR", (0,0), (-1,0), colors.HexColor("#17324D")),
        ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
        ("FONTSIZE", (0,0), (-1,-1), 8),
        ("GRID", (0,0), (-1,-1), 0.35, colors.HexColor("#B8C4D1")),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("ALIGN", (2,1), (-1,-1), "CENTER"),
        ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#F6F8FA")]),
    ]))
    story.append(tabela)
    doc.build(story); buf.seek(0)
    return send_file(
        buf, as_attachment=True,
        download_name=f"equipamentos_parque_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf",
        mimetype="application/pdf",
    )


@app.route("/projecao-lojas")
@login_required
def pagina_projecao_lojas():
    return render_template(
        "projecao_lojas.html",
        username=session.get("username"),
        role=session.get("role") or "user",
        is_admin=session.get("role") == "admin",
    )



# ---------------------------------------------------------------------
# Acompanhamento de Expansão
# ---------------------------------------------------------------------

def _acomp_valor_definido(valor):
    texto = str(valor or "").strip().upper().replace("\\", "/")
    if not texto:
        return False
    bloqueios = ("A DEFINIR", "PENDENTE", "ANO 2027")
    if any(x in texto for x in bloqueios):
        return False
    if texto in ("N/T", "NT", "N T", "INAUGURADA"):
        return False
    return True


def _dados_acompanhamento_expansao():
    linhas = db.listar_acompanhamento_expansao()
    total = len(linhas)
    status = {}
    projetos = {}
    bandeiras = {}
    ufs = {}
    inauguradas = 0
    pendentes = 0
    obra_alcance = 0
    ti_alcance = 0
    inaug_alcance = 0
    pendentes_com_data = 0

    for item in linhas:
        st = str(item.get("status_filial") or "").strip().upper() or "SEM STATUS"
        projeto = str(item.get("projeto") or "").strip().upper() or "SEM PROJETO"
        bandeira = str(item.get("bandeira") or "").strip().upper() or "SEM BANDEIRA"
        uf = str(item.get("uf") or "").strip().upper() or "SEM UF"
        status[st] = status.get(st, 0) + 1
        projetos[projeto] = projetos.get(projeto, 0) + 1
        bandeiras[bandeira] = bandeiras.get(bandeira, 0) + 1
        ufs[uf] = ufs.get(uf, 0) + 1

        concluida = st == "INAUGURADA"
        if concluida:
            inauguradas += 1
        if st == "PENDENTE":
            pendentes += 1

        obra_ok = concluida or _acomp_valor_definido(item.get("term_obra"))
        ti_ok = concluida or _acomp_valor_definido(item.get("entrada_ti"))
        inaug_ok = concluida or _acomp_valor_definido(item.get("inauguracao"))
        obra_alcance += 1 if obra_ok else 0
        ti_alcance += 1 if ti_ok else 0
        inaug_alcance += 1 if inaug_ok else 0
        if st == "PENDENTE" and _acomp_valor_definido(item.get("inauguracao")):
            pendentes_com_data += 1

        if concluida:
            item["situacao_cronograma"] = "Entregue"
        elif _acomp_valor_definido(item.get("inauguracao")):
            item["situacao_cronograma"] = "Inauguração definida"
        elif _acomp_valor_definido(item.get("entrada_ti")):
            item["situacao_cronograma"] = "Entrada TI definida"
        elif _acomp_valor_definido(item.get("term_obra")):
            item["situacao_cronograma"] = "Término de obra definido"
        else:
            item["situacao_cronograma"] = "A definir"

    base = max(1, total)
    ultima_atualizacao = max((str(x.get("atualizado_em") or "") for x in linhas), default="")
    resumo = {
        "total": total,
        "ultima_atualizacao": ultima_atualizacao,
        "inauguradas": inauguradas,
        "pendentes": pendentes,
        "ufs": len([k for k in ufs if k != "SEM UF"]),
        "entrega_realizada_pct": round(inauguradas / base * 100, 1),
        # Alcance projetado = lojas já entregues + pendentes com data de inauguração definida.
        "alcance_projetado": inaug_alcance,
        "alcance_projetado_pct": round(inaug_alcance / base * 100, 1),
        "pendentes_com_inauguracao_definida": pendentes_com_data,
        "pendentes_sem_inauguracao_definida": max(0, pendentes - pendentes_com_data),
        "etapas": {
            "obra": {"qtd": obra_alcance, "pct": round(obra_alcance / base * 100, 1)},
            "ti": {"qtd": ti_alcance, "pct": round(ti_alcance / base * 100, 1)},
            "inauguracao": {"qtd": inaug_alcance, "pct": round(inaug_alcance / base * 100, 1)},
        },
    }
    return {
        "resumo": resumo,
        "status": status,
        "projetos": projetos,
        "bandeiras": bandeiras,
        "ufs": dict(sorted(ufs.items(), key=lambda kv: (-kv[1], kv[0]))),
        "linhas": linhas,
    }



def _gerar_pdf_acompanhamento_expansao(dados):
    """Gera relatório executivo e detalhado do Acompanhamento de Expansão."""
    buf = io.BytesIO()
    page_size = landscape(A4)
    pdf = canvas.Canvas(buf, pagesize=page_size)
    larg, alt = page_size
    margem = 14 * mm
    r = dados.get("resumo") or {}
    linhas = dados.get("linhas") or []

    def _bg():
        pdf.setFillColor(colors.HexColor("#0F1620"))
        pdf.rect(0, 0, larg, alt, stroke=0, fill=1)

    def _footer(page_no):
        pdf.setStrokeColor(colors.HexColor("#283646"))
        pdf.line(margem, 11 * mm, larg - margem, 11 * mm)
        pdf.setFillColor(colors.HexColor("#8398AD"))
        pdf.setFont("Helvetica", 7.2)
        pdf.drawString(margem, 6.5 * mm, "© 2026 · Developed by ALM - Expansão de TI · Acompanhamento de Expansão")
        pdf.drawRightString(larg - margem, 6.5 * mm, f"Página {page_no}")

    def _panel(x, y, w, h, title=None, subtitle=None):
        pdf.setFillColor(colors.HexColor("#151D27"))
        pdf.setStrokeColor(colors.HexColor("#2A3645"))
        pdf.roundRect(x, y, w, h, 10, stroke=1, fill=1)
        if title:
            pdf.setFillColor(colors.white)
            pdf.setFont("Helvetica-Bold", 11)
            pdf.drawString(x + 12, y + h - 20, title)
        if subtitle:
            pdf.setFillColor(colors.HexColor("#8FA5BA"))
            pdf.setFont("Helvetica", 7.5)
            pdf.drawString(x + 12, y + h - 32, subtitle)

    def _kpi(x, y, w, h, titulo, valor, detalhe, cor="#FFFFFF"):
        pdf.setFillColor(colors.HexColor("#182230"))
        pdf.setStrokeColor(colors.HexColor("#314255"))
        pdf.roundRect(x, y, w, h, 10, stroke=1, fill=1)
        pdf.setFillColor(colors.HexColor("#94A9BC"))
        pdf.setFont("Helvetica-Bold", 7.1)
        pdf.drawString(x + 9, y + h - 13, titulo.upper())
        pdf.setFillColor(colors.HexColor(cor))
        pdf.setFont("Helvetica-Bold", 16.5)
        pdf.drawString(x + 9, y + 18, str(valor))
        pdf.setFillColor(colors.HexColor("#7990A6"))
        pdf.setFont("Helvetica", 6.6)
        pdf.drawString(x + 9, y + 7, detalhe[:34])

    def _progress(x, y, w, label, qtd, pct, cor):
        pdf.setFillColor(colors.HexColor("#D9E5F0"))
        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawString(x, y + 13, label)
        pdf.setFillColor(colors.HexColor("#A0B3C4"))
        pdf.setFont("Helvetica", 7.2)
        pdf.drawRightString(x + w, y + 13, f"{pct:.1f}% · {qtd}/{int(r.get('total') or 0)}")
        pdf.setFillColor(colors.HexColor("#0B141E"))
        pdf.roundRect(x, y, w, 7, 3.5, stroke=0, fill=1)
        barra = max(0, min(1, pct / 100.0)) * w
        if barra > 0:
            pdf.setFillColor(colors.HexColor(cor))
            pdf.roundRect(x, y, max(5, barra), 7, 3.5, stroke=0, fill=1)

    def _dist_rows(x, y, w, titulo, pares, cor="#3EA6FF", limite=6):
        pdf.setFillColor(colors.white)
        pdf.setFont("Helvetica-Bold", 9.5)
        pdf.drawString(x, y, titulo)
        total = max(1, sum(v for _, v in pares))
        cy = y - 18
        for nome, valor in pares[:limite]:
            pct = valor / total * 100.0
            pdf.setFillColor(colors.HexColor("#1B2531"))
            pdf.roundRect(x, cy - 8, w, 16, 5, stroke=0, fill=1)
            pdf.setFillColor(colors.HexColor("#D5E1ED"))
            pdf.setFont("Helvetica", 7.3)
            rot = str(nome or "-")
            pdf.drawString(x + 7, cy - 1, rot[:24])
            pdf.setFillColor(colors.HexColor(cor))
            pdf.setFont("Helvetica-Bold", 7.3)
            pdf.drawRightString(x + w - 7, cy - 1, f"{valor} · {pct:.1f}%")
            cy -= 19
        return cy

    # Página 1 - resumo executivo
    _bg()
    pdf.setTitle("Acompanhamento de Expansão")
    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 20)
    pdf.drawString(margem, alt - margem, "Acompanhamento de Expansão")
    pdf.setFillColor(colors.HexColor("#9DB3C8"))
    pdf.setFont("Helvetica", 8.5)
    pdf.drawString(margem, alt - margem - 14, "Relatório executivo do cronograma de obra, entrada de TI e inauguração das filiais acompanhadas.")
    pdf.drawRightString(larg - margem, alt - margem - 14, datetime.now().strftime("Gerado em %d/%m/%Y às %H:%M"))

    total = int(r.get("total") or 0)
    kpis = [
        ("Total acompanhado", total, "Filiais/projetos", "#FFFFFF"),
        ("Inauguradas", int(r.get("inauguradas") or 0), f"{float(r.get('entrega_realizada_pct') or 0):.1f}% concluído", "#4CD792"),
        ("Pendentes", int(r.get("pendentes") or 0), "Aguardando conclusão", "#FFB648"),
        ("Alcance projetado", f"{float(r.get('alcance_projetado_pct') or 0):.1f}%", f"{int(r.get('alcance_projetado') or 0)} de {total}", "#3EA6FF"),
        ("A definir", int(r.get("pendentes_sem_inauguracao_definida") or 0), "Sem data final", "#FF6B6B"),
        ("UFs", int(r.get("ufs") or 0), "Cobertura da base", "#A78BFA"),
    ]
    gap = 8
    kpi_y = alt - margem - 66
    kpi_h = 47
    kpi_w = (larg - 2*margem - gap*5) / 6
    for i, item in enumerate(kpis):
        _kpi(margem + i*(kpi_w+gap), kpi_y, kpi_w, kpi_h, *item)

    content_top = kpi_y - 14
    content_y = 22 * mm
    content_h = content_top - content_y
    left_w = (larg - 2*margem - 10) * 0.58
    right_x = margem + left_w + 10
    right_w = larg - margem - right_x

    _panel(margem, content_y, left_w, content_h, "Projeção de alcance de entrega", "Leitura geral do planejamento e dos principais marcos do cronograma.")
    pdf.setFillColor(colors.HexColor("#4FB2FF"))
    pdf.setFont("Helvetica-Bold", 30)
    pdf.drawString(margem + 16, content_y + content_h - 80, f"{float(r.get('alcance_projetado_pct') or 0):.1f}%")
    pdf.setFillColor(colors.HexColor("#C8D7E5"))
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(margem + 16, content_y + content_h - 96, "alcance projetado")
    pdf.setFillColor(colors.HexColor("#8CA1B6"))
    pdf.setFont("Helvetica", 7.5)
    pdf.drawString(margem + 16, content_y + content_h - 110, f"{int(r.get('alcance_projetado') or 0)} de {total} filial(is) entregues ou com inauguração definida.")

    prog_x = margem + 16
    prog_w = left_w - 32
    base_prog_y = content_y + content_h - 155
    etapas = r.get("etapas") or {}
    for idx, (nome, chave, cor) in enumerate([
        ("Término de obra", "obra", "#A78BFA"),
        ("Entrada de TI", "ti", "#56CFE1"),
        ("Inauguração / entrega", "inauguracao", "#4CD792"),
    ]):
        etapa = etapas.get(chave) or {}
        _progress(prog_x, base_prog_y - idx*40, prog_w, nome, int(etapa.get("qtd") or 0), float(etapa.get("pct") or 0), cor)

    # Mensagem executiva simples
    msg_y = content_y + 42
    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 9.5)
    pdf.drawString(margem + 16, msg_y + 34, "Leitura executiva")
    pdf.setFillColor(colors.HexColor("#B6C6D5"))
    pdf.setFont("Helvetica", 7.4)
    pend_data = int(r.get("pendentes_com_inauguracao_definida") or 0)
    pend_sem = int(r.get("pendentes_sem_inauguracao_definida") or 0)
    pdf.drawString(margem + 16, msg_y + 18, f"• {int(r.get('inauguradas') or 0)} filial(is) já inaugurada(s) e {pend_data} pendente(s) com data de inauguração definida.")
    pdf.drawString(margem + 16, msg_y + 5, f"• {pend_sem} pendente(s) ainda precisam de definição de inauguração para ampliar o alcance projetado.")

    _panel(right_x, content_y, right_w, content_h, "Distribuição da base", "Status, projetos, bandeiras e UFs com maior volume.")
    status_pares = sorted((dados.get("status") or {}).items(), key=lambda kv: (-kv[1], kv[0]))
    projetos_pares = sorted((dados.get("projetos") or {}).items(), key=lambda kv: (-kv[1], kv[0]))
    bandeiras_pares = sorted((dados.get("bandeiras") or {}).items(), key=lambda kv: (-kv[1], kv[0]))
    ufs_pares = sorted((dados.get("ufs") or {}).items(), key=lambda kv: (-kv[1], kv[0]))
    cy = content_y + content_h - 54
    cy = _dist_rows(right_x + 12, cy, right_w - 24, "Status", status_pares, "#4CD792", 3) - 8
    cy = _dist_rows(right_x + 12, cy, right_w - 24, "Projetos", projetos_pares, "#A78BFA", 4) - 8
    cy = _dist_rows(right_x + 12, cy, right_w - 24, "Bandeiras", bandeiras_pares, "#FFB648", 3) - 8
    _dist_rows(right_x + 12, cy, right_w - 24, "Top UFs", ufs_pares, "#3EA6FF", 5)
    _footer(1)
    pdf.showPage()

    # Páginas detalhadas
    page_no = 2
    cols = [
        ("Filial", 42), ("Band.", 38), ("Descrição filial", 132), ("UF", 26),
        ("Projeto", 64), ("Status", 65), ("Term. obra", 68), ("Entrada TI", 68),
        ("Inauguração", 68), ("Situação", 94),
    ]
    table_w = sum(w for _, w in cols)
    table_x = margem
    row_h = 21

    def _header_detail():
        nonlocal page_no
        _bg()
        pdf.setFillColor(colors.white)
        pdf.setFont("Helvetica-Bold", 16)
        pdf.drawString(margem, alt - margem, "Detalhamento do Acompanhamento de Expansão")
        pdf.setFillColor(colors.HexColor("#9DB3C8"))
        pdf.setFont("Helvetica", 8)
        pdf.drawString(margem, alt - margem - 13, "Dados consolidados por filial. Observações relevantes são exibidas abaixo de cada registro.")
        pdf.drawRightString(larg - margem, alt - margem - 13, f"Base: {total} registro(s)")
        y = alt - margem - 39
        pdf.setFillColor(colors.HexColor("#234C74"))
        pdf.roundRect(table_x, y, table_w, 20, 4, stroke=0, fill=1)
        pdf.setFillColor(colors.white)
        pdf.setFont("Helvetica-Bold", 7.1)
        cx = table_x
        for title, w in cols:
            pdf.drawString(cx + 4, y + 6, title)
            cx += w
        return y - 4

    y = _header_detail()
    for item in linhas:
        obs = str(item.get("observacao_ti") or "").strip()
        has_obs = bool(obs and obs.upper().replace("\\", "/") not in ("N/T", "NT", "N T", "-"))
        needed = row_h + (17 if has_obs else 0) + 3
        if y - needed < 19 * mm:
            _footer(page_no)
            pdf.showPage()
            page_no += 1
            y = _header_detail()

        y -= row_h
        fill = "#182230" if (page_no + int((alt-y)//row_h)) % 2 == 0 else "#151D27"
        pdf.setFillColor(colors.HexColor(fill))
        pdf.roundRect(table_x, y, table_w, row_h - 1, 3, stroke=0, fill=1)
        vals = [
            item.get("filial"), item.get("bandeira"), item.get("descricao_filial"), item.get("uf"),
            item.get("projeto"), item.get("status_filial"), item.get("term_obra"), item.get("entrada_ti"),
            item.get("inauguracao"), item.get("situacao_cronograma"),
        ]
        pdf.setFillColor(colors.HexColor("#DCE6F0"))
        pdf.setFont("Helvetica", 6.8)
        cx = table_x
        for (title, w), val in zip(cols, vals):
            text = str(val or "-")
            max_chars = max(4, int((w - 8) / 4.2))
            if len(text) > max_chars:
                text = text[:max(1, max_chars-1)] + "…"
            if title == "Status":
                st = str(val or "").upper()
                if st == "INAUGURADA": pdf.setFillColor(colors.HexColor("#4CD792"))
                elif st == "PENDENTE": pdf.setFillColor(colors.HexColor("#FFB648"))
                else: pdf.setFillColor(colors.HexColor("#DCE6F0"))
            elif title == "Situação":
                sit = str(val or "").upper()
                if "ENTREGUE" in sit: pdf.setFillColor(colors.HexColor("#4CD792"))
                elif "DEFINIDA" in sit: pdf.setFillColor(colors.HexColor("#3EA6FF"))
                else: pdf.setFillColor(colors.HexColor("#FFB648"))
            else:
                pdf.setFillColor(colors.HexColor("#DCE6F0"))
            pdf.drawString(cx + 4, y + 7, text)
            cx += w

        if has_obs:
            y -= 17
            pdf.setFillColor(colors.HexColor("#101923"))
            pdf.roundRect(table_x, y + 2, table_w, 14, 3, stroke=0, fill=1)
            pdf.setFillColor(colors.HexColor("#91A7BD"))
            pdf.setFont("Helvetica-Bold", 6.6)
            pdf.drawString(table_x + 5, y + 6, "Obs. TI:")
            pdf.setFillColor(colors.HexColor("#C4D3E1"))
            pdf.setFont("Helvetica", 6.6)
            texto_obs = obs if len(obs) <= 135 else obs[:132] + "…"
            pdf.drawString(table_x + 40, y + 6, texto_obs)
        y -= 3

    _footer(page_no)
    pdf.save()
    buf.seek(0)
    return buf


def _cabecalho_acomp_normalizado(valor):
    texto = unicodedata.normalize("NFD", str(valor or "").strip().upper())
    texto = "".join(c for c in texto if unicodedata.category(c) != "Mn")
    texto = texto.replace(".", " ").replace("_", " ").replace("-", " ")
    texto = " ".join(texto.split())
    mapa = {
        "FILIAL": "filial",
        "CODIGO FILIAL": "filial",
        "BANDEIRA": "bandeira",
        "DESCRICAO FILIAL": "descricao_filial",
        "DESCRICAO": "descricao_filial",
        "UF": "uf",
        "PROJETO": "projeto",
        "STATUS FILIAL": "status_filial",
        "STATUS": "status_filial",
        "ENVIADA": "enviada",
        "EM SEPARACAO": "em_separacao",
        "EQUIP SEPARADO": "equip_separado",
        "EQUIPAMENTO SEPARADO": "equip_separado",
        "TERM OBRA": "term_obra",
        "TERMINO OBRA": "term_obra",
        "TERMINO DE OBRA": "term_obra",
        "ENTRADA DE TI": "entrada_ti",
        "ENTRADA TI": "entrada_ti",
        "INAUGURACAO": "inauguracao",
        "OBSERVACAO TI": "observacao_ti",
        "OBS TI": "observacao_ti",
    }
    return mapa.get(texto)


def _valor_excel_acomp(valor):
    if valor is None:
        return ""
    if hasattr(valor, "strftime"):
        try:
            return valor.strftime("%d/%m/%Y")
        except Exception:
            pass
    if isinstance(valor, float) and valor.is_integer():
        return str(int(valor))
    return str(valor).strip()


def _ler_planilha_acompanhamento(arquivo):
    wb = load_workbook(arquivo, read_only=True, data_only=True)
    ws = wb.active
    if ws is None:
        raise ValueError("A planilha não possui uma aba com dados.")
    linhas_iter = ws.iter_rows(values_only=True)
    mapa = {}
    cab_row = 0
    for numero_linha, valores in enumerate(linhas_iter, start=1):
        if numero_linha > 10:
            break
        candidato = {}
        for idx, valor in enumerate(valores or ()):
            campo = _cabecalho_acomp_normalizado(valor)
            if campo and campo not in candidato:
                candidato[campo] = idx
        if "filial" in candidato:
            mapa = candidato
            cab_row = numero_linha
            break
    obrigatorios = {
        "filial", "bandeira", "descricao_filial", "uf", "projeto", "status_filial",
        "term_obra", "entrada_ti", "inauguracao", "observacao_ti",
    }
    faltando = sorted(obrigatorios - set(mapa))
    if faltando:
        raise ValueError("Colunas obrigatórias não encontradas: " + ", ".join(faltando))

    registros = []
    erros = []
    vistos = set()
    total = 0
    for numero_linha, valores in enumerate(linhas_iter, start=cab_row + 1):
        valores = tuple(valores or ())
        if not valores or all(v is None or str(v).strip() == "" for v in valores):
            continue
        total += 1
        def ler(campo):
            idx = mapa.get(campo)
            return _valor_excel_acomp(valores[idx]) if idx is not None and idx < len(valores) else ""
        filial = ler("filial").strip()
        if not filial:
            if len(erros) < 20:
                erros.append(f"Linha {numero_linha}: FILIAL não informada.")
            continue
        if filial in vistos:
            if len(erros) < 20:
                erros.append(f"Linha {numero_linha}: FILIAL {filial} repetida no arquivo.")
            continue
        vistos.add(filial)
        registros.append({
            "filial": filial,
            "bandeira": ler("bandeira").upper(),
            "descricao_filial": ler("descricao_filial"),
            "uf": ler("uf").upper(),
            "projeto": ler("projeto").upper(),
            "status_filial": ler("status_filial").upper(),
            "enviada": (ler("enviada") or "NAO").upper(),
            "em_separacao": (ler("em_separacao") or "NAO").upper(),
            "equip_separado": (ler("equip_separado") or "NAO").upper(),
            "term_obra": ler("term_obra"),
            "entrada_ti": ler("entrada_ti"),
            "inauguracao": ler("inauguracao"),
            "observacao_ti": ler("observacao_ti"),
        })
    return registros, erros, total


def _status_filial_por_acompanhamento(status_filial):
    """Traduz o status do Acompanhamento para o status operacional de Filiais."""
    status = _normalizar_exec(status_filial)
    if status == "inaugurada":
        return "1"
    if status == "pendente":
        # Uma loja pendente no Acompanhamento ainda faz parte do pipeline de abertura.
        return "inaugurar"
    return "pendente"


def _previsao_filial_por_acompanhamento(valor, atual=""):
    texto = str(valor or "").strip()
    if not texto or _normalizar_exec(texto) in {"a definir", "pendente", "sem data", "-"}:
        return str(atual or "")
    convertido = _data_filial_iso(texto)
    # Não grava textos livres na coluna de data de Filiais.
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(convertido or "")):
        return convertido
    return str(atual or "")


def _sincronizar_filial_a_partir_acompanhamento(dados, usuario, codigo_anterior=None):
    """Cria/atualiza Filiais usando o Acompanhamento como origem dos campos compartilhados."""
    codigo = str(dados.get("filial") or "").strip()
    if not codigo:
        raise ValueError("Código da filial não informado para sincronização.")

    anterior = str(codigo_anterior or "").strip()
    filial = db.buscar_filial_por_codigo(anterior) if anterior else None
    if not filial:
        filial = db.buscar_filial_por_codigo(codigo)

    nome = str(dados.get("descricao_filial") or "").strip()
    uf = str(dados.get("uf") or "").strip().upper()[:2]
    bandeira = str(dados.get("bandeira") or "").strip().upper()
    if bandeira not in ("DSP", "DPA"):
        bandeira = ""
    ativo = _status_filial_por_acompanhamento(dados.get("status_filial"))

    if filial:
        cidade = str(filial.get("cidade") or "").strip()
        previsao = _previsao_filial_por_acompanhamento(dados.get("inauguracao"), filial.get("previsao_abertura"))
        ok = db.atualizar_filial(
            int(filial["id"]), codigo, nome or str(filial.get("nome") or ""), cidade, uf or str(filial.get("uf") or ""),
            ativo, bandeira=bandeira or filial.get("bandeira"), previsao_abertura=previsao,
        )
        if not ok:
            raise RuntimeError("Não foi possível atualizar a filial sincronizada.")
        return {"acao": "atualizada", "id": int(filial["id"]), "codigo": codigo}

    previsao = _previsao_filial_por_acompanhamento(dados.get("inauguracao"), "")
    novo_id = db.criar_filial(
        codigo, nome, "", uf, ativo, usuario, bandeira=bandeira, previsao_abertura=previsao,
    )
    return {"acao": "criada", "id": int(novo_id), "codigo": codigo}


def _inativar_filial_ao_excluir_acompanhamento(codigo):
    """Retira a loja da projeção sem apagar histórico nem vínculos de equipamentos."""
    filial = db.buscar_filial_por_codigo(str(codigo or "").strip())
    if not filial:
        return None
    db.atualizar_filial(
        int(filial["id"]), str(filial.get("codigo") or ""), str(filial.get("nome") or ""),
        str(filial.get("cidade") or ""), str(filial.get("uf") or ""), "0",
        bandeira=filial.get("bandeira"), previsao_abertura=filial.get("previsao_abertura"),
    )
    return {"acao": "inativada", "id": int(filial["id"]), "codigo": filial.get("codigo")}


@app.route("/acompanhamento-expansao")
@login_required
def pagina_acompanhamento_expansao():
    return render_template(
        "acompanhamento_expansao.html",
        username=session.get("username"),
        role=session.get("role") or "user",
        is_admin=session.get("role") == "admin",
    )


@app.route("/cockpit-implantacao")
@login_required
def pagina_cockpit_implantacao():
    role = session.get("role") or "user"
    if role == "user":
        role = "operador"
    return render_template(
        "cockpit_implantacao.html",
        username=session.get("username"),
        role=role,
        is_admin=role == "admin",
        dados=_dados_cockpit_implantacao(incluir_financeiro=role != "consulta"),
        pode_ver_financeiro=role != "consulta",
    )


@app.route("/api/cockpit-implantacao")
@login_required
def api_cockpit_implantacao():
    role = session.get("role") or "user"
    if role == "user":
        role = "operador"
    return jsonify(_dados_cockpit_implantacao(incluir_financeiro=role != "consulta"))


@app.route("/api/acompanhamento-expansao")
@login_required
def api_acompanhamento_expansao():
    return jsonify(_dados_acompanhamento_expansao())


@app.route("/api/acompanhamento-expansao/cadastrar", methods=["POST"])
@edit_required
def api_cadastrar_acompanhamento_expansao():
    if not _csrf_ok():
        return jsonify({"erro": "A sessão de segurança expirou. Atualize a página e tente novamente."}), 400

    dados = request.get_json(silent=True) or {}
    filial = str(dados.get("filial") or "").strip()
    descricao = str(dados.get("descricao_filial") or "").strip()
    bandeira = str(dados.get("bandeira") or "").strip().upper()
    uf = str(dados.get("uf") or "").strip().upper()
    projeto = str(dados.get("projeto") or "").strip().upper()
    status_filial = str(dados.get("status_filial") or "").strip().upper()

    obrigatorios = []
    if not filial: obrigatorios.append("Filial")
    if not descricao: obrigatorios.append("Descrição filial")
    if not bandeira: obrigatorios.append("Bandeira")
    if not uf: obrigatorios.append("UF")
    if not projeto: obrigatorios.append("Projeto")
    if not status_filial: obrigatorios.append("Status filial")
    if obrigatorios:
        return jsonify({"erro": "Preencha os campos obrigatórios: " + ", ".join(obrigatorios) + "."}), 400

    if _normalizar_exec(status_filial) == "inaugurada":
        filial_existente = db.buscar_filial_por_codigo(filial)
        ja_ativa = bool(filial_existente and str(filial_existente.get("ativo") or "") == "1")
        if not ja_ativa:
            faltantes_kit = _faltantes_kit_real_filial(filial)
            if faltantes_kit:
                return jsonify({
                    "erro": "A loja só pode ser marcada como Inaugurada/Ativa depois que o Kit padrão receber baixa real no Estoque.",
                    "faltantes_kit": faltantes_kit,
                }), 409

    payload = {
        "filial": filial,
        "bandeira": bandeira,
        "descricao_filial": descricao,
        "uf": uf,
        "projeto": projeto,
        "status_filial": status_filial,
        "enviada": "SIM" if str(dados.get("enviada") or "NAO").strip().upper() == "SIM" else "NAO",
        "em_separacao": "SIM" if str(dados.get("em_separacao") or "NAO").strip().upper() == "SIM" else "NAO",
        "equip_separado": "SIM" if str(dados.get("equip_separado") or "NAO").strip().upper() == "SIM" else "NAO",
        "term_obra": str(dados.get("term_obra") or "").strip(),
        "entrada_ti": str(dados.get("entrada_ti") or "").strip(),
        "inauguracao": str(dados.get("inauguracao") or "").strip(),
        "observacao_ti": str(dados.get("observacao_ti") or "").strip(),
    }

    try:
        registro = db.criar_acompanhamento_expansao(payload, session.get("username"))
    except ValueError as e:
        return jsonify({"erro": str(e)}), 409
    except Exception:
        app.logger.exception("Erro ao cadastrar acompanhamento de expansão da filial %s", filial)
        return jsonify({"erro": "Não foi possível cadastrar a loja. Verifique os dados e tente novamente."}), 500

    try:
        sync_filial = _sincronizar_filial_a_partir_acompanhamento(payload, session.get("username"))
    except Exception:
        # Compensação: não deixa o Acompanhamento salvo sem o espelho em Filiais.
        try:
            if registro and registro.get("id"):
                db.excluir_acompanhamento_expansao(int(registro["id"]))
        except Exception:
            app.logger.exception("Falha ao reverter acompanhamento após erro de sincronização da filial %s", filial)
        app.logger.exception("Erro ao sincronizar filial %s a partir do Acompanhamento", filial)
        return jsonify({"erro": "Não foi possível sincronizar a loja com a aba Filiais. Nenhuma alteração foi mantida no Acompanhamento."}), 500

    baixa_inauguracao = {"consumidas": 0, "faltantes": 0, "itens_faltantes": []}

    db.registrar_movimentacao(
        0, "cadastro_acompanhamento_expansao", "1", session.get("username"),
        f"Nova loja cadastrada no Acompanhamento de Expansão · Filial {filial} · {descricao} · {projeto} · {status_filial}",
        tabela="sistema"
    )
    return jsonify({"ok": True, "registro": registro, "filial_sincronizada": sync_filial, "baixa_inauguracao": baixa_inauguracao}), 201


@app.route("/api/acompanhamento-expansao/<int:registro_id>", methods=["PUT"])
@edit_required
def api_atualizar_acompanhamento_expansao(registro_id):
    if not _csrf_ok():
        return jsonify({"erro": "A sessão de segurança expirou. Atualize a página e tente novamente."}), 400

    anterior = db.buscar_acompanhamento_expansao_por_id(registro_id)
    if not anterior:
        return jsonify({"erro": "Registro de acompanhamento não encontrado."}), 404

    dados = request.get_json(silent=True) or {}
    filial = str(dados.get("filial") or "").strip()
    if not filial:
        return jsonify({"erro": "O campo Filial é obrigatório."}), 400

    payload = {
        "filial": filial,
        "bandeira": str(dados.get("bandeira") or "").strip().upper(),
        "descricao_filial": str(dados.get("descricao_filial") or "").strip(),
        "uf": str(dados.get("uf") or "").strip().upper(),
        "projeto": str(dados.get("projeto") or "").strip().upper(),
        "status_filial": str(dados.get("status_filial") or "").strip().upper(),
        "enviada": "SIM" if str(dados.get("enviada") or "NAO").strip().upper() == "SIM" else "NAO",
        "em_separacao": "SIM" if str(dados.get("em_separacao") or "NAO").strip().upper() == "SIM" else "NAO",
        "equip_separado": "SIM" if str(dados.get("equip_separado") or "NAO").strip().upper() == "SIM" else "NAO",
        "term_obra": str(dados.get("term_obra") or "").strip(),
        "entrada_ti": str(dados.get("entrada_ti") or "").strip(),
        "inauguracao": str(dados.get("inauguracao") or "").strip(),
        "observacao_ti": str(dados.get("observacao_ti") or "").strip(),
    }


    if _normalizar_exec(payload.get("status_filial")) == "inaugurada":
        filial_existente = db.buscar_filial_por_codigo(filial)
        ja_ativa = bool(filial_existente and str(filial_existente.get("ativo") or "") == "1")
        if not ja_ativa:
            faltantes_kit = _faltantes_kit_real_filial(filial)
            if faltantes_kit:
                return jsonify({
                    "erro": "A loja só pode ser marcada como Inaugurada/Ativa depois que o Kit padrão receber baixa real no Estoque.",
                    "faltantes_kit": faltantes_kit,
                }), 409

    # Evita duplicidade da chave FILIAL caso o código seja alterado manualmente.
    for existente in db.listar_acompanhamento_expansao():
        if int(existente.get("id") or 0) != registro_id and str(existente.get("filial") or "").strip() == filial:
            return jsonify({"erro": f"Já existe um acompanhamento cadastrado para a filial {filial}."}), 409

    try:
        ok = db.atualizar_acompanhamento_expansao(registro_id, payload, session.get("username"))
    except Exception:
        app.logger.exception("Erro ao atualizar acompanhamento de expansão %s", registro_id)
        return jsonify({"erro": "Não foi possível salvar a alteração. Verifique os dados e tente novamente."}), 500
    if not ok:
        return jsonify({"erro": "Registro de acompanhamento não encontrado."}), 404

    try:
        sync_filial = _sincronizar_filial_a_partir_acompanhamento(
            payload, session.get("username"), codigo_anterior=anterior.get("filial")
        )
    except Exception:
        # Compensação: restaura o Acompanhamento para não deixar as abas divergentes.
        try:
            db.atualizar_acompanhamento_expansao(registro_id, anterior, session.get("username"))
        except Exception:
            app.logger.exception("Falha ao restaurar acompanhamento %s após erro de sincronização", registro_id)
        app.logger.exception("Erro ao sincronizar Filiais após alteração do acompanhamento %s", registro_id)
        return jsonify({"erro": "Não foi possível sincronizar a alteração com a aba Filiais. O Acompanhamento foi restaurado."}), 500

    baixa_inauguracao = {"consumidas": 0, "faltantes": 0, "itens_faltantes": []}

    alteracoes = []
    for campo, rotulo in (("filial","Filial"),("bandeira","Bandeira"),("descricao_filial","Descrição"),("uf","UF"),("projeto","Projeto"),("status_filial","Status"),("enviada","Enviada"),("em_separacao","Em Separação"),("equip_separado","Equip. separado"),("term_obra","Término obra"),("entrada_ti","Entrada TI"),("inauguracao","Inauguração"),("observacao_ti","Observação TI")):
        antes = str(anterior.get(campo) or "").strip()
        depois = str(payload.get(campo) or "").strip()
        if antes != depois:
            alteracoes.append(f"{rotulo}: {antes or '-'} -> {depois or '-'}")
    if alteracoes:
        db.registrar_movimentacao(
            0, "alteracao_acompanhamento_expansao", "1", session.get("username"),
            f"Acompanhamento filial {filial} alterado · " + " | ".join(alteracoes[:10]), tabela="sistema"
        )

    atualizado = db.buscar_acompanhamento_expansao_por_id(registro_id)
    return jsonify({"ok": True, "registro": atualizado, "filial_sincronizada": sync_filial, "baixa_inauguracao": baixa_inauguracao})


@app.route("/api/acompanhamento-expansao/<int:registro_id>", methods=["DELETE"])
@edit_required
def api_excluir_acompanhamento_expansao(registro_id):
    if not _csrf_ok():
        return jsonify({"erro": "A sessão de segurança expirou. Atualize a página e tente novamente."}), 400

    registro = db.buscar_acompanhamento_expansao_por_id(registro_id)
    if not registro:
        return jsonify({"erro": "Registro de acompanhamento não encontrado."}), 404

    try:
        excluido = db.excluir_acompanhamento_expansao(registro_id)
    except Exception:
        app.logger.exception("Erro ao excluir acompanhamento de expansão %s", registro_id)
        return jsonify({"erro": "Não foi possível excluir a loja selecionada."}), 500

    if not excluido:
        return jsonify({"erro": "Registro de acompanhamento não encontrado."}), 404

    filial_sincronizada = None
    try:
        filial_sincronizada = _inativar_filial_ao_excluir_acompanhamento(registro.get("filial"))
    except Exception:
        app.logger.exception("Acompanhamento excluído, mas não foi possível inativar a filial sincronizada %s", registro.get("filial"))

    db.registrar_movimentacao(
        0, "exclusao_acompanhamento_expansao", "1", session.get("username"),
        f"Loja removida do Acompanhamento de Expansão · Filial {registro.get('filial') or registro_id}",
        tabela="sistema"
    )
    return jsonify({"ok": True, "registro": registro, "filial_sincronizada": filial_sincronizada, "build": APP_BUILD})


@app.route("/api/acompanhamento-expansao/excluir-em-lote", methods=["POST"])
@edit_required
def api_excluir_acompanhamento_expansao_em_lote():
    if not _csrf_ok():
        return jsonify({"erro": "A sessão de segurança expirou. Atualize a página e tente novamente."}), 400

    dados = request.get_json(silent=True) or {}
    ids = dados.get("ids") or []
    if not isinstance(ids, list) or not ids:
        return jsonify({"erro": "Nenhum registro selecionado."}), 400

    ids_limpos = []
    for valor in ids:
        try:
            registro_id = int(valor)
        except (TypeError, ValueError):
            continue
        if registro_id > 0 and registro_id not in ids_limpos:
            ids_limpos.append(registro_id)
    if not ids_limpos:
        return jsonify({"erro": "Nenhum registro válido selecionado."}), 400

    try:
        excluidos, nao_encontrados = db.excluir_acompanhamento_expansao_em_lote(ids_limpos)
    except Exception:
        app.logger.exception("Erro ao excluir acompanhamentos de expansão em lote")
        return jsonify({"erro": "Não foi possível excluir os registros selecionados."}), 500

    codigos_excluidos = [str(item.get("filial") or "").strip() for item in excluidos if str(item.get("filial") or "").strip()]
    filiais_inativadas = 0
    try:
        filiais_inativadas = db.inativar_filiais_por_codigos(codigos_excluidos)
    except Exception:
        app.logger.exception("Acompanhamentos excluídos, mas falhou a inativação em lote das Filiais")

    if excluidos:
        resumo_codigos = ", ".join(codigos_excluidos[:30])
        if len(codigos_excluidos) > 30:
            resumo_codigos += f" ... (+{len(codigos_excluidos)-30})"
        try:
            db.registrar_movimentacao(
                0, "exclusao_acompanhamento_expansao", str(len(excluidos)), session.get("username"),
                f"Exclusão em massa no Acompanhamento de Expansão · {len(excluidos)} loja(s) · Filiais: {resumo_codigos or '-'}",
                tabela="sistema"
            )
        except Exception:
            app.logger.exception("Falha ao registrar auditoria da exclusão em massa do Acompanhamento")

    return jsonify({
        "ok": True,
        "excluidos": len(excluidos),
        "nao_encontradas": nao_encontrados,
        "filiais_inativadas": filiais_inativadas,
        "build": APP_BUILD,
    })


@app.route("/export-acompanhamento-expansao")
@role_required("admin", "gestor", "operador")
def exportar_acompanhamento_expansao():
    dados = _dados_acompanhamento_expansao()
    wb = Workbook()
    ws = wb.active
    ws.title = "Acompanhamento"
    headers = ["FILIAL","BANDEIRA","DESCRIÇÃO FILIAL","UF","PROJETO","STATUS FILIAL","ENVIADA","EM SEPARAÇÃO","EQUIP. SEPARADO","TERM. OBRA","ENTRADA DE TI","INAUGURAÇÃO","OBSERVAÇÃO TI"]
    ws.append(headers)
    for item in dados["linhas"]:
        ws.append([
            item.get("filial") or "", item.get("bandeira") or "", item.get("descricao_filial") or "",
            item.get("uf") or "", item.get("projeto") or "", item.get("status_filial") or "",
            item.get("enviada") or "NAO", item.get("em_separacao") or "NAO", item.get("equip_separado") or "NAO",
            item.get("term_obra") or "", item.get("entrada_ti") or "", item.get("inauguracao") or "",
            item.get("observacao_ti") or "",
        ])
    header_fill = PatternFill("solid", fgColor="1F4E78")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:M{max(1,ws.max_row)}"
    widths = [12,12,38,8,16,16,12,16,16,16,18,16,46]
    for i,w in enumerate(widths,1):
        ws.column_dimensions[get_column_letter(i)].width = w
    for row in ws.iter_rows(min_row=2, max_col=13):
        row[0].number_format = "@"
        for c in (1,3,4,5,6,7,8):
            row[c].alignment = Alignment(horizontal="center", vertical="center")
        row[9].alignment = Alignment(wrap_text=True, vertical="top")

    resumo = wb.create_sheet("Resumo")
    r = dados["resumo"]
    linhas_resumo = [
        ("Indicador", "Valor"),
        ("Total de projetos", r["total"]),
        ("Inauguradas", r["inauguradas"]),
        ("Pendentes", r["pendentes"]),
        ("Entrega realizada", f'{r["entrega_realizada_pct"]:.1f}%'),
        ("Alcance projetado", f'{r["alcance_projetado_pct"]:.1f}%'),
        ("Pendentes com inauguração definida", r["pendentes_com_inauguracao_definida"]),
        ("Pendentes sem inauguração definida", r["pendentes_sem_inauguracao_definida"]),
        ("Alcance término de obra", f'{r["etapas"]["obra"]["pct"]:.1f}%'),
        ("Alcance entrada de TI", f'{r["etapas"]["ti"]["pct"]:.1f}%'),
        ("Alcance inauguração", f'{r["etapas"]["inauguracao"]["pct"]:.1f}%'),
    ]
    for linha in linhas_resumo:
        resumo.append(linha)
    for cell in resumo[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = header_fill
    resumo.column_dimensions["A"].width = 38
    resumo.column_dimensions["B"].width = 18

    buf = io.BytesIO(); wb.save(buf); buf.seek(0)
    return send_file(
        buf, as_attachment=True,
        download_name=f"acompanhamento_expansao_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )



@app.route("/pdf-acompanhamento-expansao")
@role_required("admin", "gestor", "operador")
def relatorio_pdf_acompanhamento_expansao():
    dados = _dados_acompanhamento_expansao()
    buf = _gerar_pdf_acompanhamento_expansao(dados)
    return send_file(
        buf,
        as_attachment=True,
        download_name=f"acompanhamento_expansao_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf",
        mimetype="application/pdf",
    )


@app.route("/api/acompanhamento-expansao/importar/validar", methods=["POST"])
@edit_required
def api_validar_importacao_acompanhamento_expansao():
    if not _csrf_ok():
        return jsonify({"erro":"A sessão de segurança expirou. Atualize a página e tente novamente."}), 400
    modo = _normalizar_modo_importacao(request.form.get("modo"))
    arquivo = request.files.get("arquivo")
    if not arquivo or not arquivo.filename:
        return jsonify({"erro":"Nenhum arquivo enviado."}), 400
    if not arquivo.filename.lower().endswith((".xlsx", ".xlsm")):
        return jsonify({"erro":"Envie um arquivo Excel (.xlsx ou .xlsm)."}), 400
    try:
        registros, erros, total = _ler_planilha_acompanhamento(arquivo)
        if erros or len(registros) != total:
            detalhes = " | ".join(erros[:15]) or "Existem linhas inválidas na planilha."
            return jsonify({
                "erro": "A planilha possui inconsistências. Corrija antes de importar. " + detalhes,
                "erros": erros[:50], "total_linhas": total, "validas": len(registros),
            }), 400
        if not registros:
            return jsonify({"erro":"Nenhum registro válido foi encontrado na planilha."}), 400

        itens_estoque_validacao = db.listar_itens() or []
        kit_validacao = db.listar_kit_padrao_loja() or []
        bloqueadas = []
        for reg in registros:
            if _normalizar_exec(reg.get("status_filial")) != "inaugurada":
                continue
            codigo_reg = str(reg.get("filial") or "").strip()
            filial_existente = db.buscar_filial_por_codigo(codigo_reg)
            if filial_existente and str(filial_existente.get("ativo") or "") == "1":
                continue
            if _faltantes_kit_real_filial(codigo_reg, itens_estoque=itens_estoque_validacao, kit=kit_validacao):
                bloqueadas.append(codigo_reg or "sem código")
        if bloqueadas:
            return jsonify({
                "erro": "A planilha contém lojas marcadas como Inaugurada sem o Kit padrão baixado no Estoque.",
                "filiais_bloqueadas": bloqueadas[:50],
            }), 400

        atuais = db.listar_acompanhamento_expansao()
        atuais_codigos = {str(x.get("filial") or "").strip() for x in atuais}
        novos_codigos = {str(x.get("filial") or "").strip() for x in registros}
        novas = len([c for c in novos_codigos if c and c not in atuais_codigos])
        existentes_no_arquivo = len([c for c in novos_codigos if c and c in atuais_codigos])
        return jsonify({
            "ok": True, "arquivo": arquivo.filename, "modo": modo,
            "total_linhas": total, "validas": len(registros), "existentes": len(atuais),
            "registros_previstos": len(registros), "novas": novas,
            "existentes_no_arquivo": existentes_no_arquivo,
        })
    except ValueError as e:
        return jsonify({"erro":str(e)}), 400
    except Exception:
        app.logger.exception("Falha ao validar planilha do Acompanhamento de Expansão")
        return jsonify({"erro":"Erro ao validar a planilha do Acompanhamento de Expansão."}), 500


@app.route("/api/acompanhamento-expansao/importar", methods=["POST"])
@edit_required
def api_importar_acompanhamento_expansao():
    if not _csrf_ok():
        return jsonify({"erro":"A sessão de segurança expirou. Atualize a página e tente novamente."}), 400
    senha = request.form.get("senha", "")
    usuario_atual = db.buscar_usuario_por_id(session["user_id"])
    if not usuario_atual or not check_password_hash(usuario_atual["password_hash"], senha):
        return jsonify({"erro":"Senha incorreta."}), 403
    modo = _normalizar_modo_importacao(request.form.get("modo"))
    arquivo = request.files.get("arquivo")
    if not arquivo or not arquivo.filename:
        return jsonify({"erro":"Nenhum arquivo enviado."}), 400
    if not arquivo.filename.lower().endswith((".xlsx", ".xlsm")):
        return jsonify({"erro":"Envie um arquivo Excel (.xlsx ou .xlsm)."}), 400
    try:
        registros, erros, total = _ler_planilha_acompanhamento(arquivo)
        # Mesmo princípio usado no upload de Estoque: primeiro valida tudo;
        # se uma linha estiver inválida, nada é alterado.
        if erros or len(registros) != total:
            detalhes = " | ".join(erros[:15]) or "Existem linhas inválidas na planilha."
            return jsonify({
                "erro":"A planilha possui inconsistências. Nenhuma alteração foi realizada. " + detalhes,
                "erros":erros[:50], "total_linhas":total, "validas":len(registros),
            }), 400
        if not registros:
            return jsonify({"erro":"Nenhum registro válido foi encontrado na planilha."}), 400

        # v117: uma loja nova não pode entrar como INAUGURADA/ATIVA sem que o
        # Kit padrão esteja realmente baixado no Estoque. Lojas que já eram
        # ativas antes desta regra são tratadas como legado e podem permanecer ativas.
        itens_estoque_validacao = db.listar_itens() or []
        kit_validacao = db.listar_kit_padrao_loja() or []
        bloqueadas = []
        for reg in registros:
            if _normalizar_exec(reg.get("status_filial")) != "inaugurada":
                continue
            codigo_reg = str(reg.get("filial") or "").strip()
            filial_existente = db.buscar_filial_por_codigo(codigo_reg)
            if filial_existente and str(filial_existente.get("ativo") or "") == "1":
                continue
            faltantes = _faltantes_kit_real_filial(codigo_reg, itens_estoque=itens_estoque_validacao, kit=kit_validacao)
            if faltantes:
                bloqueadas.append(codigo_reg or "sem código")
        if bloqueadas:
            return jsonify({
                "erro": "Existem lojas marcadas como Inaugurada sem Kit padrão baixado no Estoque. Nenhuma alteração foi realizada.",
                "filiais_bloqueadas": bloqueadas[:50],
            }), 400

        usuario = session.get("username")
        removidos = 0
        filiais_inativadas = 0
        if modo == "substituir":
            resultado = db.substituir_acompanhamento_expansao_em_lote(registros, usuario)
            criadas = int(resultado.get("criados") or 0)
            atualizadas = 0
            sem_alteracao = 0
            removidos = int(resultado.get("removidos") or 0)
            codigos_novos = {str(x.get("filial") or "").strip() for x in registros}
            codigos_removidos = [c for c in (resultado.get("filiais_anteriores") or []) if c and c not in codigos_novos]
            if codigos_removidos:
                try:
                    filiais_inativadas = db.inativar_filiais_por_codigos(codigos_removidos)
                except Exception:
                    app.logger.exception("Falha ao inativar Filiais removidas pela substituição do Acompanhamento")
        else:
            resultado = db.importar_acompanhamento_expansao_em_lote(registros, usuario)
            criadas = int(resultado.get("criadas") or 0)
            atualizadas = int(resultado.get("atualizadas") or 0)
            sem_alteracao = int(resultado.get("sem_alteracao") or 0)

        sincronizadas = 0
        falhas_sincronizacao = []
        baixas_inauguracao = 0
        faltantes_inauguracao = 0
        for item in registros:
            try:
                _sincronizar_filial_a_partir_acompanhamento(item, usuario)
                sincronizadas += 1
            except Exception as sync_err:
                codigo_sync = str(item.get("filial") or "").strip()
                falhas_sincronizacao.append(codigo_sync or "sem código")
                app.logger.exception("Falha ao sincronizar filial %s após importação do acompanhamento: %s", codigo_sync, sync_err)

        detalhes_auditoria = (
            f"Modo: {modo}. Registros anteriores removidos: {removidos}. "
            f"Sem alteração: {sem_alteracao}. Filiais inativadas: {filiais_inativadas}."
        )
        try:
            db.registrar_importacao(
                "acompanhamento_expansao", arquivo.filename, total, len(registros), criadas,
                atualizadas, 0, usuario, "concluida", detalhes_auditoria,
            )
            db.registrar_movimentacao(
                0, "importacao_acompanhamento_expansao", str(criadas + atualizadas), usuario,
                f"Acompanhamento de Expansão · modo {modo} · {total} linha(s) · {criadas} criada(s) · "
                f"{atualizadas} atualizada(s) · {sem_alteracao} sem alteração · {removidos} removida(s) na substituição.",
                tabela="sistema",
            )
        except Exception as audit_err:
            app.logger.exception("Acompanhamento importado, mas falhou auditoria: %s", audit_err)

        return jsonify({
            "ok":True, "arquivo":arquivo.filename, "modo":modo, "total_linhas":total,
            "processadas":len(registros), "criadas":criadas, "atualizadas":atualizadas,
            "sem_alteracao":sem_alteracao, "ignoradas":0, "erros":[], "removidos":removidos,
            "filiais_sincronizadas": sincronizadas, "filiais_inativadas": filiais_inativadas,
            "falhas_sincronizacao": falhas_sincronizacao,
            "baixas_inauguracao": baixas_inauguracao, "faltantes_inauguracao": faltantes_inauguracao,
        })
    except ValueError as e:
        return jsonify({"erro":str(e)}), 400
    except Exception as e:
        app.logger.exception("Falha ao importar acompanhamento de expansão")
        return jsonify({"erro":f"Erro ao processar a planilha ({type(e).__name__})."}), 500


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
    # A lista de Filiais agora exibe somente dados cadastrais/status. O parque
    # e o Kit ficam nas telas específicas, reduzindo também o tempo de carga.
    return jsonify(db.listar_filiais(incluir_inativas=incluir_inativas) or [])


def _texto_celula_excel_filial(celula):
    """Converte a célula para texto, preservando zeros à esquerda quando possível."""
    valor = celula.value
    if valor is None:
        return ""
    if isinstance(valor, bool):
        return "1" if valor else "0"
    if isinstance(valor, (int, float)) and not isinstance(valor, bool):
        fmt = str(celula.number_format or "")
        # Formatos como 0000 / 000000 preservam o código visual da loja.
        if isinstance(valor, int) or (isinstance(valor, float) and valor.is_integer()):
            inteiro = int(valor)
            apenas_zeros = fmt.replace(";", "").replace("@", "").strip()
            if apenas_zeros and set(apenas_zeros) <= {"0"}:
                return str(inteiro).zfill(len(apenas_zeros))
            return str(inteiro)
    return str(valor).strip()


def _cabecalho_filial_normalizado(valor):
    s = unicodedata.normalize("NFD", str(valor or ""))
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    # Remove pontuação e separadores para aceitar cabeçalhos como
    # "Nome / identificação", exatamente como no Excel exportado pelo sistema.
    s = "".join(ch if ch.isalnum() else " " for ch in s.lower())
    s = " ".join(s.split())
    aliases = {
        "codigo": {
            "codigo", "codigo filial", "codigo da filial", "codigo loja", "codigo da loja",
            "numero loja", "numero da loja", "n loja", "loja", "filial",
        },
        "nome": {"nome", "nome identificacao", "nome filial", "nome da filial", "identificacao", "descricao", "descricao filial"},
        "cidade": {"cidade", "municipio"},
        "uf": {"uf", "estado", "sigla uf"},
        "bandeira": {"bandeira", "marca", "rede"},
        "status": {"status", "situacao", "situacao da loja", "ativo"},
        "previsao_abertura": {"previsao abertura", "previsao de abertura", "data abertura", "data de abertura", "abertura prevista"},
    }
    for campo, nomes in aliases.items():
        if s in nomes:
            return campo
    return None


def _status_filial_importacao(valor, atual=None):
    s = unicodedata.normalize("NFD", str(valor or "")).encode("ascii", "ignore").decode("ascii").strip().lower()
    if not s:
        return str(atual if atual is not None else "1")
    mapa = {
        "1": "1", "ativa": "1", "ativo": "1", "sim": "1", "true": "1",
        "0": "0", "inativa": "0", "inativo": "0", "nao": "0", "false": "0",
        "inaugurar": "inaugurar", "a inaugurar": "inaugurar", "inauguracao": "inaugurar",
        "pendente": "pendente", "pendencia": "pendente",
    }
    return mapa.get(s)

def _data_filial_iso(valor):
    if valor is None or valor == "":
        return ""
    if isinstance(valor, datetime):
        return valor.strftime("%Y-%m-%d")
    texto=str(valor).strip()
    for fmt in ("%Y-%m-%d","%d/%m/%Y","%d-%m-%Y","%Y/%m/%d"):
        try:
            return datetime.strptime(texto[:10],fmt).strftime("%Y-%m-%d")
        except Exception:
            pass
    return texto[:10] if len(texto)>=10 else texto


@app.route("/export-filiais")
@role_required("admin", "gestor", "operador")
def exportar_filiais_excel():
    filiais = db.listar_filiais(incluir_inativas=True)
    wb = Workbook()
    ws = wb.active
    ws.title = "Filiais"
    headers = ["Código da filial", "Nome / identificação", "Cidade", "UF", "Bandeira", "Status", "Previsão de abertura"]
    ws.append(headers)
    status_rotulos = {"1": "Ativa", "0": "Inativa", "inaugurar": "Inaugurar", "pendente": "Pendente"}
    for f in filiais:
        ws.append([
            str(f.get("codigo") or ""), f.get("nome") or "", f.get("cidade") or "",
            str(f.get("uf") or "").upper(), str(f.get("bandeira") or "").upper(),
            status_rotulos.get(str(f.get("ativo") or ""), str(f.get("ativo") or "")),
            str(f.get("previsao_abertura") or ""),
        ])

    cor_header = PatternFill("solid", fgColor="1F4E78")
    for c in ws[1]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = cor_header
        c.alignment = Alignment(horizontal="center", vertical="center")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:G{max(1, ws.max_row)}"
    larguras = [20, 34, 24, 10, 14, 16, 20]
    for idx, largura in enumerate(larguras, start=1):
        ws.column_dimensions[get_column_letter(idx)].width = largura
    for row in ws.iter_rows(min_row=2, max_col=7):
        row[0].number_format = "@"
        row[3].alignment = Alignment(horizontal="center")
        row[4].alignment = Alignment(horizontal="center")
        row[5].alignment = Alignment(horizontal="center")

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf,
        as_attachment=True,
        download_name=f"filiais_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/api/filiais/importar/validar", methods=["POST"])
@manager_required
def api_validar_filiais_excel():
    arquivo=request.files.get("arquivo")
    if not arquivo or not arquivo.filename:
        return jsonify({"erro":"Nenhum arquivo enviado."}),400
    if not arquivo.filename.lower().endswith((".xlsx",".xlsm")):
        return jsonify({"erro":"Envie um arquivo Excel (.xlsx ou .xlsm)."}),400
    try:
        wb=load_workbook(arquivo,read_only=True,data_only=True); ws=wb.active
        linhas_iter=ws.iter_rows(values_only=True)
        mapa_colunas={}; cab_row=0
        for numero_linha,valores in enumerate(linhas_iter,start=1):
            if numero_linha>10: break
            candidato={}
            for idx,valor in enumerate(valores or ()):
                campo=_cabecalho_filial_normalizado(valor)
                if campo and campo not in candidato: candidato[campo]=idx
            if "codigo" in candidato:
                mapa_colunas=candidato; cab_row=numero_linha; break
        if not mapa_colunas:
            return jsonify({"erro":"Não encontrei a coluna de código da filial."}),400
        atuais={str(f.get("codigo") or "").strip():f for f in db.listar_filiais(incluir_inativas=True)}
        vistos=set(); total=validas=ignoradas=criadas=atualizadas=sem_alteracao=0; erros=[]; amostra=[]
        for numero_linha,valores in enumerate(linhas_iter,start=cab_row+1):
            valores=tuple(valores or ())
            if not valores or all(v is None or str(v).strip()=="" for v in valores): continue
            total+=1
            def ler(campo):
                idx=mapa_colunas.get(campo)
                if idx is None or idx>=len(valores): return ""
                v=valores[idx]
                if v is None:return ""
                if isinstance(v,datetime):return v.strftime("%Y-%m-%d")
                if isinstance(v,float) and v.is_integer():return str(int(v))
                return str(v).strip()
            codigo=ler("codigo").strip(); uf=ler("uf").upper().strip(); bandeira=ler("bandeira").upper().strip(); status_txt=ler("status").strip()
            if not codigo or codigo in vistos:
                ignoradas+=1
                if len(erros)<15: erros.append(f"Linha {numero_linha}: código ausente ou repetido ({codigo or '-'}).")
                continue
            vistos.add(codigo)
            if uf and (len(uf)!=2 or uf not in UF_NOMES):
                ignoradas+=1
                if len(erros)<15:erros.append(f"Linha {numero_linha}: UF '{uf}' inválida.")
                continue
            if bandeira and bandeira not in ("DSP","DPA"):
                ignoradas+=1
                if len(erros)<15:erros.append(f"Linha {numero_linha}: bandeira '{bandeira}' inválida.")
                continue
            status="" if not status_txt else _status_filial_importacao(status_txt,None)
            if status_txt and status is None:
                ignoradas+=1
                if len(erros)<15:erros.append(f"Linha {numero_linha}: status '{status_txt}' inválido.")
                continue
            validas+=1
            atual=atuais.get(codigo)
            if atual:
                nome=ler("nome") or str(atual.get("nome") or ""); cidade=ler("cidade") or str(atual.get("cidade") or ""); nova_uf=uf or str(atual.get("uf") or "").upper(); nova_b=bandeira or str(atual.get("bandeira") or "").upper(); novo_s=status or str(atual.get("ativo") or "1"); nova_p=_data_filial_iso(ler("previsao_abertura")) or str(atual.get("previsao_abertura") or "")
                mudou=any([str(atual.get("nome") or "")!=nome,str(atual.get("cidade") or "")!=cidade,str(atual.get("uf") or "").upper()!=nova_uf,str(atual.get("bandeira") or "").upper()!=nova_b,str(atual.get("ativo") or "")!=novo_s,str(atual.get("previsao_abertura") or "")!=nova_p])
                if mudou: atualizadas+=1
                else: sem_alteracao+=1
            else: criadas+=1
            if len(amostra)<8: amostra.append({"codigo":codigo,"nome":ler("nome"),"uf":uf,"status":status or (atual or {}).get("ativo","1"),"acao":"Atualizar" if atual else "Criar"})
        return jsonify({"ok":True,"arquivo":arquivo.filename,"total_linhas":total,"validas":validas,"criadas":criadas,"atualizadas":atualizadas,"sem_alteracao":sem_alteracao,"ignoradas":ignoradas,"erros":erros,"amostra":amostra})
    except Exception as e:
        return jsonify({"erro":f"Erro ao validar a planilha: {e}"}),500

@app.route("/api/filiais/importar", methods=["POST"])
@manager_required
def api_importar_filiais_excel():
    # Mesmo padrão de segurança da importação da aba Estoque.
    senha = request.form.get("senha", "")
    usuario_atual = db.buscar_usuario_por_id(session["user_id"])
    if not usuario_atual or not check_password_hash(usuario_atual["password_hash"], senha):
        return jsonify({"erro": "Senha incorreta."}), 403

    arquivo = request.files.get("arquivo")
    if not arquivo or not arquivo.filename:
        return jsonify({"erro": "Nenhum arquivo enviado."}), 400
    if not arquivo.filename.lower().endswith((".xlsx", ".xlsm")):
        return jsonify({"erro": "Envie um arquivo Excel (.xlsx ou .xlsm)."}), 400

    try:
        try:
            wb = load_workbook(arquivo, read_only=True, data_only=True)
            ws = wb.active
        except Exception:
            return jsonify({"erro": "Não consegui abrir esse arquivo. Confirme se é um .xlsx válido."}), 400

        if ws is None:
            return jsonify({"erro": "A planilha não possui uma aba com dados."}), 400

        # Leitura sequencial, igual à importação do Estoque. Evita milhares de
        # acessos aleatórios ws.cell() no modo read_only, que é lento no Render.
        linhas_iter = ws.iter_rows(values_only=True)
        cabecalho = None
        mapa_colunas = {}
        cab_row = 0
        for numero_linha, valores in enumerate(linhas_iter, start=1):
            if numero_linha > 10:
                break
            candidato = {}
            for idx, valor in enumerate(valores or ()):
                campo = _cabecalho_filial_normalizado(valor)
                if campo and campo not in candidato:
                    candidato[campo] = idx
            if "codigo" in candidato:
                cabecalho = valores
                mapa_colunas = candidato
                cab_row = numero_linha
                break

        if cabecalho is None:
            return jsonify({
                "erro": "Não encontrei a coluna de código da filial. Use a planilha baixada pela própria aba Filiais."
            }), 400

        usuario = session.get("username")
        linhas_validas = []
        vistos = set()
        erros = []
        ignoradas = 0
        total_linhas = 0

        # As linhas restantes do iterador começam imediatamente após o cabeçalho.
        for numero_linha, valores in enumerate(linhas_iter, start=cab_row + 1):
            valores = tuple(valores or ())
            if not valores or all(v is None or str(v).strip() == "" for v in valores):
                continue
            total_linhas += 1

            def ler(campo):
                idx = mapa_colunas.get(campo)
                if idx is None or idx >= len(valores):
                    return ""
                valor = valores[idx]
                if valor is None:
                    return ""
                if isinstance(valor, bool):
                    return "1" if valor else "0"
                if isinstance(valor, float) and valor.is_integer():
                    return str(int(valor))
                return str(valor).strip()

            codigo = ler("codigo").strip()
            if not codigo:
                ignoradas += 1
                if len(erros) < 20:
                    erros.append(f"Linha {numero_linha}: código da filial não informado.")
                continue
            if codigo in vistos:
                ignoradas += 1
                if len(erros) < 20:
                    erros.append(f"Linha {numero_linha}: código {codigo} repetido na planilha.")
                continue
            vistos.add(codigo)

            nome_filial = ler("nome")
            cidade = ler("cidade")
            uf = ler("uf").upper().strip()
            bandeira = ler("bandeira").upper().strip()
            status_txt = ler("status").strip()
            previsao_abertura = _data_filial_iso(ler("previsao_abertura"))

            if uf and (len(uf) != 2 or uf not in UF_NOMES):
                ignoradas += 1
                if len(erros) < 20:
                    erros.append(f"Linha {numero_linha}: UF '{uf}' inválida para a filial {codigo}.")
                continue
            if bandeira and bandeira not in ("DSP", "DPA"):
                ignoradas += 1
                if len(erros) < 20:
                    erros.append(f"Linha {numero_linha}: bandeira '{bandeira}' inválida para a filial {codigo}.")
                continue

            status = "" if not status_txt else _status_filial_importacao(status_txt, None)
            if status_txt and status is None:
                ignoradas += 1
                if len(erros) < 20:
                    erros.append(f"Linha {numero_linha}: status '{status_txt}' inválido para a filial {codigo}.")
                continue

            linhas_validas.append({
                "codigo": codigo,
                "nome": nome_filial,
                "cidade": cidade,
                "uf": uf,
                "bandeira": bandeira,
                "status": status,
                "previsao_abertura": previsao_abertura,
            })

        if not linhas_validas:
            return jsonify({"erro": "Nenhuma filial válida foi encontrada na planilha."}), 400

        resultado = db.importar_filiais_em_lote(linhas_validas, usuario)
        criadas = int(resultado.get("criadas") or 0)
        atualizadas = int(resultado.get("atualizadas") or 0)
        sem_alteracao = int(resultado.get("sem_alteracao") or 0)
        processadas = criadas + atualizadas + sem_alteracao
        try:
            db.registrar_importacao("filiais", arquivo.filename, total_linhas, processadas, criadas, atualizadas, ignoradas, usuario, "concluida", f"Sem alteração: {sem_alteracao}")
        except Exception as audit_err:
            print(f"[aviso] Falha ao registrar auditoria de importação: {audit_err}")

        if criadas or atualizadas:
            try:
                db.registrar_movimentacao(
                    0,
                    "importacao_filiais",
                    str(criadas + atualizadas),
                    usuario,
                    f"Importação de filiais: {total_linhas} linha(s) lida(s), {processadas} processada(s), {criadas} criada(s), {atualizadas} atualizada(s), {sem_alteracao} sem alteração e {ignoradas} ignorada(s).",
                    tabela="sistema",
                )
            except Exception as hist_err:
                # O histórico não deve desfazer uma importação que já foi concluída.
                print(f"[aviso] Importação de filiais concluída, mas falhou o histórico: {type(hist_err).__name__}: {hist_err}")

        return jsonify({
            "ok": True,
            "arquivo": arquivo.filename,
            "total_linhas": total_linhas,
            "processadas": processadas,
            "criadas": criadas,
            "atualizadas": atualizadas,
            "sem_alteracao": sem_alteracao,
            "ignoradas": ignoradas,
            "erros": erros,
        })
    except Exception as e:
        print(f"[erro] Falha ao importar filiais: {type(e).__name__}: {e}")
        return jsonify({"erro": f"Erro ao processar a planilha ({type(e).__name__}). Consulte o log do servidor para o detalhe técnico."}), 500


@app.route("/api/filiais", methods=["POST"])
@manager_required
def api_criar_filial():
    dados = request.get_json(force=True)
    codigo = (dados.get("codigo") or "").strip()
    nome = (dados.get("nome") or "").strip()
    cidade = (dados.get("cidade") or "").strip()
    uf = (dados.get("uf") or "").strip().upper()[:2]
    bandeira = (dados.get("bandeira") or "").strip().upper()
    if bandeira not in ("", "DSP", "DPA"):
        return jsonify({"erro": "Bandeira inválida. Use DSP ou DPA."}), 400
    previsao_abertura = _data_filial_iso(dados.get("previsao_abertura"))
    ativo = str(dados.get("ativo", "1") or "1").strip().lower()
    mapa_status = {"ativa":"1","ativo":"1","1":"1","inativa":"0","inativo":"0","0":"0","inaugurar":"inaugurar","pendente":"pendente"}
    ativo = mapa_status.get(ativo, "1")
    if not codigo:
        return jsonify({"erro": "Código da filial é obrigatório."}), 400
    try:
        novo_id = db.criar_filial(codigo, nome, cidade, uf, ativo, session.get("username"), bandeira=bandeira, previsao_abertura=previsao_abertura)
    except Exception:
        return jsonify({"erro": "Já existe uma filial cadastrada com este código."}), 409
    db.registrar_movimentacao(0,"criacao_filial","1",session.get("username"),f"Filial {codigo} criada · status={ativo} · UF={uf} · previsão={previsao_abertura or '-'}",tabela="sistema")
    return jsonify({"ok": True, "id": novo_id}), 201


@app.route("/api/filiais/<int:filial_id>", methods=["PUT"])
@manager_required
def api_atualizar_filial(filial_id):
    dados = request.get_json(force=True)
    codigo = (dados.get("codigo") or "").strip()
    nome = (dados.get("nome") or "").strip()
    cidade = (dados.get("cidade") or "").strip()
    uf = (dados.get("uf") or "").strip().upper()[:2]
    bandeira = (dados.get("bandeira") or "").strip().upper()
    if bandeira not in ("", "DSP", "DPA"):
        return jsonify({"erro": "Bandeira inválida. Use DSP ou DPA."}), 400
    previsao_abertura = _data_filial_iso(dados.get("previsao_abertura"))
    ativo = str(dados.get("ativo", "1") or "1").strip().lower()
    mapa_status = {"ativa":"1","ativo":"1","1":"1","inativa":"0","inativo":"0","0":"0","inaugurar":"inaugurar","pendente":"pendente"}
    ativo = mapa_status.get(ativo, "1")
    if not codigo:
        return jsonify({"erro": "Código da filial é obrigatório."}), 400
    anterior=db.buscar_filial_por_id(filial_id)
    try:
        ok = db.atualizar_filial(filial_id, codigo, nome, cidade, uf, ativo, bandeira=bandeira, previsao_abertura=previsao_abertura)
    except Exception:
        return jsonify({"erro": "Já existe outra filial com este código."}), 409
    if ok:
        resumo_ant=f"status={anterior.get('ativo') if anterior else '-'}; UF={anterior.get('uf') if anterior else '-'}; bandeira={anterior.get('bandeira') if anterior else '-'}; previsão={anterior.get('previsao_abertura') if anterior else '-'}"
        resumo_novo=f"status={ativo}; UF={uf}; bandeira={bandeira or '-'}; previsão={previsao_abertura or '-'}"
        db.registrar_movimentacao(0,"alteracao_filial","1",session.get("username"),f"Filial {codigo} alterada · antes: {resumo_ant} · depois: {resumo_novo}",tabela="sistema")
        return jsonify({"ok": True})
    return jsonify({"erro": "Filial não encontrada."}), 404


@app.route("/api/filiais/<int:filial_id>", methods=["DELETE"])
@manager_required
def api_excluir_filial(filial_id):
    """Exclusão individual no mesmo padrão visual/operacional do Estoque."""
    filial = db.buscar_filial_por_id(filial_id)
    if not filial:
        return jsonify({"erro": "Filial não encontrada."}), 404
    try:
        excluidas, _ = db.excluir_filiais_em_lote([filial_id], desvincular_equipamentos=True)
    except Exception:
        app.logger.exception("Erro ao excluir filial %s", filial_id)
        return jsonify({"erro": "Erro ao excluir filial."}), 500
    if not excluidas:
        return jsonify({"erro": "Erro ao excluir filial."}), 500
    db.registrar_movimentacao(
        0,
        "exclusao_filial",
        "1",
        session.get("username"),
        f"Filial {filial.get('codigo') or filial_id} excluída.",
        tabela="sistema",
    )
    return jsonify({"ok": True, "filial": filial})


@app.route("/api/filiais/excluir-em-lote", methods=["POST"])
@manager_required
def api_excluir_filiais_em_lote():
    """Exclusão em massa seguindo o mesmo fluxo usado no Estoque."""
    dados = request.get_json(force=True) or {}
    ids = dados.get("ids") or []
    if not isinstance(ids, list) or not ids:
        return jsonify({"erro": "Nenhuma filial selecionada."}), 400

    ids_limpos = []
    for valor in ids:
        try:
            filial_id = int(valor)
        except (TypeError, ValueError):
            continue
        if filial_id > 0 and filial_id not in ids_limpos:
            ids_limpos.append(filial_id)
    if not ids_limpos:
        return jsonify({"erro": "Nenhuma filial válida selecionada."}), 400

    # Captura os dados antes da exclusão para registrar no histórico.
    atuais = {int(f["id"]): f for f in db.listar_filiais(incluir_inativas=True) if f.get("id") is not None}
    try:
        excluidas, nao_encontradas = db.excluir_filiais_em_lote(ids_limpos, desvincular_equipamentos=True)
    except Exception:
        app.logger.exception("Erro ao excluir filiais em lote")
        return jsonify({"erro": "Erro ao excluir as filiais selecionadas."}), 500

    for info in excluidas:
        filial = atuais.get(int(info.get("id") or 0), {})
        db.registrar_movimentacao(
            0,
            "exclusao_filial",
            "1",
            session.get("username"),
            f"Filial {filial.get('codigo') or info.get('codigo') or info.get('id')} excluída (exclusão em massa).",
            tabela="sistema",
        )

    return jsonify({
        "ok": True,
        "excluidos": len(excluidas),
        "nao_encontradas": nao_encontradas,
        "build": APP_BUILD,
    })


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
    try:
        custo = _moeda_canonica(dados.get("custo"), "0.00")
    except ValueError:
        return jsonify({"erro": "Informe um custo válido para o produto."}), 400
    if not codigo or not descricao:
        return jsonify({"erro": "Código de cadastro e descrição são obrigatórios."}), 400
    if qtde_por_loja < 1:
        return jsonify({"erro": "A quantidade necessária por loja deve ser no mínimo 1."}), 400
    if _decimal_moeda(custo) < 0:
        return jsonify({"erro": "O custo do produto não pode ser negativo."}), 400
    try:
        novo_id = db.criar_produto(codigo, descricao, qtde_por_loja, custo, session.get("username"))
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
    try:
        custo = _moeda_canonica(dados.get("custo"), "0.00")
    except ValueError:
        return jsonify({"erro": "Informe um custo válido para o produto."}), 400
    if not codigo or not descricao:
        return jsonify({"erro": "Código de cadastro e descrição são obrigatórios."}), 400
    if qtde_por_loja < 1:
        return jsonify({"erro": "A quantidade necessária por loja deve ser no mínimo 1."}), 400
    if _decimal_moeda(custo) < 0:
        return jsonify({"erro": "O custo do produto não pode ser negativo."}), 400
    try:
        ok = db.atualizar_produto(produto_id, codigo, descricao, qtde_por_loja, custo)
    except Exception:
        return jsonify({"erro": "Já existe outro produto com este código."}), 409
    return (jsonify({"ok": True}) if ok else (jsonify({"erro": "Produto não encontrado."}), 404))


@app.route("/api/produtos/<int:produto_id>", methods=["DELETE"])
@manager_required
def api_excluir_produto(produto_id):
    ok = db.excluir_produto(produto_id)
    return (jsonify({"ok": True}) if ok else (jsonify({"erro": "Produto não encontrado."}), 404))


def _calcular_orcamento_pepi():
    """Cruza Cadastro de Produtos, PEPI, Kit Padrão, Estoque de Expansão e lojas planejadas.

    O Cadastro de Produtos é a fonte mestre de código, descrição e custo do Orçamento.
    O Kit Padrão informa somente a quantidade necessária por loja; o Estoque informa a
    disponibilidade que será descontada antes da sugestão de compra.
    """
    def qtd_num(valor):
        try:
            return int(float(valor or 0))
        except (TypeError, ValueError):
            return 0

    produtos = db.listar_produtos()
    kit = db.listar_kit_padrao_loja()
    base = db.obter_dashboard_compacto(1)
    itens = base.get("itens") or []
    meta = db.obter_meta_lojas_expansao()
    pipeline = _pipeline_acompanhamento_expansao()
    lojas_planejadas = pipeline["pendentes"]
    lojas_base = max(1, len(lojas_planejadas) or int(meta or 1))

    expansao = [
        x for x in itens
        if _normalizar_exec(x.get("tipo_estoque")) == "expansao" and qtd_num(x.get("qtde")) > 0
    ]
    produtos_codigo = {
        str(p.get("codigo") or "").strip().lower(): p
        for p in produtos if str(p.get("codigo") or "").strip()
    }

    def produto_para_kit(k):
        """Localiza no cadastro mestre o produto correspondente ao item do Kit Padrão."""
        codigo = str(k.get("codigo") or "").strip().lower()
        if codigo and codigo in produtos_codigo:
            return produtos_codigo[codigo]
        desc = _normalizar_exec(k.get("descricao"))
        candidatos = []
        for p in produtos:
            pd = _normalizar_exec(p.get("descricao"))
            if desc and pd and (desc == pd or desc in pd or pd in desc):
                candidatos.append((0 if desc == pd else abs(len(desc) - len(pd)), p))
        return sorted(candidatos, key=lambda x: x[0])[0][1] if candidatos else None

    def estoque_disponivel(prod, k):
        """Soma o estoque de Expansão usando primeiro os dados do Cadastro de Produtos."""
        codigos = {
            str(v or "").strip().lower()
            for v in ((prod or {}).get("codigo"), k.get("codigo"))
            if str(v or "").strip()
        }
        descricoes = {
            _normalizar_exec(v)
            for v in ((prod or {}).get("descricao"), k.get("descricao"))
            if _normalizar_exec(v)
        }
        total = 0
        for item in expansao:
            codigo_i = str(item.get("codigo") or "").strip().lower()
            desc_i = _normalizar_exec(item.get("descricao"))
            combina_codigo = bool(codigo_i and codigo_i in codigos)
            combina_desc = bool(desc_i and any(d == desc_i or d in desc_i or desc_i in d for d in descricoes))
            if combina_codigo or combina_desc:
                total += qtd_num(item.get("qtde"))
        return total

    linhas = []
    total_previsto = Decimal("0.00")
    itens_sem_custo = 0
    itens_sem_cadastro = 0
    itens_para_comprar = 0

    for k in kit:
        prod = produto_para_kit(k)
        qtd_kit = qtd_num(k.get("quantidade"))
        qtd_produto = qtd_num((prod or {}).get("qtde_por_loja"))
        qtd_por_loja = max(1, qtd_kit or qtd_produto or 1)
        disponivel = estoque_disponivel(prod, k)
        necessario = qtd_por_loja * lojas_base
        comprar = max(0, necessario - disponivel)

        # Código, descrição e custo exibidos no Orçamento vêm do Cadastro de Produtos.
        codigo_produto = str((prod or {}).get("codigo") or "").strip()
        descricao_produto = str((prod or {}).get("descricao") or "").strip()
        custo = _decimal_moeda((prod or {}).get("custo"), "0.00") if prod else Decimal("0.00")
        custo_informado = bool(prod) and custo > 0
        subtotal = (custo * comprar).quantize(Decimal("0.01")) if custo_informado else Decimal("0.00")

        if comprar > 0:
            itens_para_comprar += 1
            if not prod:
                itens_sem_cadastro += 1
            if not custo_informado:
                itens_sem_custo += 1
            else:
                total_previsto += subtotal

        linhas.append({
            "kit_id": k.get("id"),
            "codigo": codigo_produto or str(k.get("codigo") or "").strip(),
            "descricao": descricao_produto or str(k.get("descricao") or "").strip(),
            "produto_id": (prod or {}).get("id"),
            "produto_descricao": descricao_produto,
            "kit_codigo": str(k.get("codigo") or "").strip(),
            "kit_descricao": str(k.get("descricao") or "").strip(),
            "cadastro_produto_ok": bool(prod),
            "fonte_cadastro": "Cadastro de Produtos" if prod else "Kit Padrão (cadastro pendente)",
            "qtd_por_loja": qtd_por_loja,
            "lojas_base": lojas_base,
            "necessario": necessario,
            "estoque_expansao": disponivel,
            "comprar": comprar,
            "custo": format(custo, ".2f"),
            "custo_informado": custo_informado,
            "subtotal": format(subtotal, ".2f"),
        })

    pepi = _decimal_moeda(db.obter_orcamento_pepi_consolidado(), "0.00")
    saldo = (pepi - total_previsto).quantize(Decimal("0.01"))
    percentual = Decimal("0.00")
    if pepi > 0:
        percentual = min(Decimal("999.99"), (total_previsto / pepi * Decimal("100")).quantize(Decimal("0.01")))
    linhas.sort(key=lambda x: (x["comprar"] <= 0, -int(x["comprar"]), str(x["descricao"]).lower()))

    pedido_linhas = [dict(x) for x in linhas if int(x.get("comprar") or 0) > 0]
    total_unidades_pedido = sum(int(x.get("comprar") or 0) for x in pedido_linhas)
    lojas_consideradas = []
    por_uf = {}
    for filial in lojas_planejadas:
        uf = str(filial.get("uf") or "").strip().upper() or "--"
        por_uf[uf] = por_uf.get(uf, 0) + 1
        lojas_consideradas.append({
            "id": filial.get("id"),
            "codigo": str(filial.get("codigo") or "").strip(),
            "nome": str(filial.get("nome") or "").strip(),
            "uf": uf,
            "previsao_abertura": str(filial.get("previsao_abertura") or "").strip(),
        })
    lojas_consideradas.sort(key=lambda x: (x.get("previsao_abertura") or "9999-99-99", x.get("uf") or "", x.get("codigo") or ""))

    return {
        "pepi_consolidado": format(pepi, ".2f"),
        "total_previsto": format(total_previsto, ".2f"),
        "saldo": format(saldo, ".2f"),
        "percentual_comprometido": format(percentual, ".2f"),
        "itens_sem_custo": itens_sem_custo,
        "itens_sem_cadastro": itens_sem_cadastro,
        "itens_para_comprar": itens_para_comprar,
        "total_unidades_pedido": total_unidades_pedido,
        "lojas_planejadas": len(lojas_planejadas),
        "lojas_consideradas": lojas_consideradas,
        "lojas_por_uf": [{"uf": uf, "quantidade": qtd} for uf, qtd in sorted(por_uf.items())],
        "base_origem": "acompanhamento_expansao" if lojas_planejadas else "meta_expansao",
        "fonte_produtos": "cadastro_produtos",
        "produtos_cadastrados": len(produtos),
        "meta_lojas": int(meta or 10),
        "lojas_base": lojas_base,
        "orcamento_completo": itens_sem_custo == 0 and itens_sem_cadastro == 0,
        "orcamento_suficiente": saldo >= 0 and itens_sem_custo == 0 and itens_sem_cadastro == 0,
        "pedido_pronto": bool(pedido_linhas) and itens_sem_custo == 0 and itens_sem_cadastro == 0,
        "pedido_linhas": pedido_linhas,
        "linhas": linhas,
    }


def _dados_cockpit_implantacao(incluir_financeiro=True):
    """Visão executiva por filial para implantação de TI.

    O Acompanhamento de Expansão é a fonte dos marcos operacionais. O Kit Padrão,
    Cadastro de Produtos e Estoque de Expansão são usados para simular a cobertura
    dos equipamentos em ordem cronológica de inauguração/Entrada de TI.
    """
    def _qtd(valor):
        try:
            return max(0, int(float(valor or 0)))
        except (TypeError, ValueError):
            return 0

    def _sim(valor):
        return _normalizar_exec(valor) == "sim"

    def _dias(iso):
        if not iso:
            return None
        try:
            return (datetime.strptime(iso, "%Y-%m-%d").date() - datetime.now().date()).days
        except Exception:
            return None

    linhas_acomp = db.listar_acompanhamento_expansao()
    pendentes = [x for x in linhas_acomp if _normalizar_exec(x.get("status_filial")) == "pendente"]
    inauguradas = sum(1 for x in linhas_acomp if _normalizar_exec(x.get("status_filial")) == "inaugurada")

    produtos = db.listar_produtos()
    kit = db.listar_kit_padrao_loja()
    itens = (db.obter_dashboard_compacto(1) or {}).get("itens") or []
    estoque_expansao = [
        x for x in itens
        if _normalizar_exec(x.get("tipo_estoque")) == "expansao" and _qtd(x.get("qtde")) > 0
    ]
    produtos_codigo = {
        str(p.get("codigo") or "").strip().lower(): p
        for p in produtos if str(p.get("codigo") or "").strip()
    }

    def _produto_kit(k):
        codigo = str(k.get("codigo") or "").strip().lower()
        if codigo and codigo in produtos_codigo:
            return produtos_codigo[codigo]
        desc = _normalizar_exec(k.get("descricao"))
        candidatos = []
        for prod in produtos:
            pd = _normalizar_exec(prod.get("descricao"))
            if desc and pd and (desc == pd or desc in pd or pd in desc):
                candidatos.append((0 if desc == pd else abs(len(desc) - len(pd)), prod))
        return sorted(candidatos, key=lambda x: x[0])[0][1] if candidatos else None

    def _estoque_produto(prod, k):
        codigos = {
            str(v or "").strip().lower()
            for v in ((prod or {}).get("codigo"), k.get("codigo"))
            if str(v or "").strip()
        }
        descricoes = {
            _normalizar_exec(v)
            for v in ((prod or {}).get("descricao"), k.get("descricao"))
            if _normalizar_exec(v)
        }
        total = 0
        for item in estoque_expansao:
            cod = str(item.get("codigo") or "").strip().lower()
            desc = _normalizar_exec(item.get("descricao"))
            if (cod and cod in codigos) or (desc and any(d == desc or d in desc or desc in d for d in descricoes)):
                total += _qtd(item.get("qtde"))
        return total

    specs = []
    for k in kit:
        prod = _produto_kit(k)
        qtd_kit = _qtd(k.get("quantidade"))
        qtd_prod = _qtd((prod or {}).get("qtde_por_loja"))
        qtd_loja = max(1, qtd_kit or qtd_prod or 1)
        custo = _decimal_moeda((prod or {}).get("custo"), "0.00") if prod else Decimal("0.00")
        specs.append({
            "codigo": str((prod or {}).get("codigo") or k.get("codigo") or "").strip(),
            "descricao": str((prod or {}).get("descricao") or k.get("descricao") or "Item sem descrição").strip(),
            "qtd_loja": qtd_loja,
            "custo": custo,
            "estoque": _estoque_produto(prod, k),
            "restante": _estoque_produto(prod, k),
            "custo_informado": bool(prod) and custo > 0,
        })

    def _ordem_loja(item):
        inaug = _data_acompanhamento_iso(item.get("inauguracao"))
        entrada = _data_acompanhamento_iso(item.get("entrada_ti"))
        return (inaug or entrada or "9999-99-99", entrada or "9999-99-99", str(item.get("filial") or ""))

    pendentes.sort(key=_ordem_loja)
    lojas = []
    faltantes_detalhe = []
    bloqueios_detalhe = []
    valor_total_faltante = Decimal("0.00")
    itens_faltantes_codigos = set()
    unidades_faltantes = 0

    for item in pendentes:
        score = 0
        bloqueios = []
        term_ok = _acomp_valor_definido(item.get("term_obra"))
        enviada_ok = _sim(item.get("enviada"))
        separacao_ok = _sim(item.get("em_separacao"))
        equip_ok = _sim(item.get("equip_separado"))
        entrada_iso = _data_acompanhamento_iso(item.get("entrada_ti"))
        inaug_iso = _data_acompanhamento_iso(item.get("inauguracao"))
        if term_ok: score += 15
        else: bloqueios.append("Término da obra não definido")
        if enviada_ok: score += 15
        else: bloqueios.append("Envio não confirmado")
        if separacao_ok: score += 15
        else: bloqueios.append("Separação não confirmada")
        if equip_ok: score += 20
        else: bloqueios.append("Equipamentos não separados")
        if entrada_iso: score += 20
        else: bloqueios.append("Entrada de TI sem data")
        if inaug_iso: score += 15
        else: bloqueios.append("Inauguração sem data")

        dias_ti = _dias(entrada_iso)
        dias_inaug = _dias(inaug_iso)
        if dias_ti is not None and dias_ti < 0:
            bloqueios.append(f"Entrada de TI vencida há {abs(dias_ti)} dia(s)")
        if dias_inaug is not None and dias_inaug < 0:
            bloqueios.append(f"Inauguração vencida há {abs(dias_inaug)} dia(s)")

        faltantes = []
        valor_loja = Decimal("0.00")
        for spec in specs:
            necessario = int(spec["qtd_loja"])
            disponivel = max(0, int(spec["restante"]))
            atendido = min(disponivel, necessario)
            falta = necessario - atendido
            spec["restante"] = disponivel - atendido
            if falta > 0:
                codigo_chave = spec["codigo"] or spec["descricao"]
                itens_faltantes_codigos.add(codigo_chave)
                unidades_faltantes += falta
                subtotal = (spec["custo"] * falta).quantize(Decimal("0.01")) if spec["custo_informado"] else Decimal("0.00")
                if spec["custo_informado"]:
                    valor_loja += subtotal
                    valor_total_faltante += subtotal
                falt = {
                    "codigo": spec["codigo"],
                    "descricao": spec["descricao"],
                    "quantidade": falta,
                    "custo": format(spec["custo"], ".2f"),
                    "custo_informado": spec["custo_informado"],
                    "valor": format(subtotal, ".2f"),
                }
                faltantes.append(falt)
                faltantes_detalhe.append({
                    "filial": str(item.get("filial") or ""),
                    "loja": str(item.get("descricao_filial") or ""),
                    **falt,
                })
        if faltantes:
            bloqueios.append(f"Estoque insuficiente: {sum(x['quantidade'] for x in faltantes)} unidade(s)")

        if score >= 80 and not faltantes and not any("vencida" in b.lower() for b in bloqueios):
            faixa = "PRONTA"
        elif score >= 50:
            faixa = "ATENCAO"
        else:
            faixa = "CRITICA"

        # Exibição executiva: somente possíveis bloqueios que podem comprometer a inauguração.
        bloqueios_inauguracao = []
        term_iso = _data_acompanhamento_iso(item.get("term_obra"))
        if not inaug_iso:
            bloqueios_inauguracao.append("Data de inauguração não definida")
        elif dias_inaug is not None and dias_inaug < 0:
            bloqueios_inauguracao.append(f"Data de inauguração vencida há {abs(dias_inaug)} dia(s)")

        if not term_ok:
            bloqueios_inauguracao.append("Término da obra não definido")
        elif term_iso and inaug_iso and term_iso > inaug_iso:
            bloqueios_inauguracao.append("Término da obra posterior à inauguração")

        if not entrada_iso:
            bloqueios_inauguracao.append("Entrada de TI sem data")
        elif inaug_iso and entrada_iso > inaug_iso:
            bloqueios_inauguracao.append("Entrada de TI posterior à inauguração")

        if not equip_ok:
            bloqueios_inauguracao.append("Equipamentos não separados")
        elif not enviada_ok:
            bloqueios_inauguracao.append("Equipamentos ainda não enviados")
        elif not separacao_ok:
            bloqueios_inauguracao.append("Separação ainda não concluída")

        if faltantes:
            bloqueios_inauguracao.append(f"Estoque insuficiente: {sum(x['quantidade'] for x in faltantes)} unidade(s)")

        # Remove duplicidades preservando a ordem de prioridade.
        bloqueios_inauguracao = list(dict.fromkeys(bloqueios_inauguracao))

        filial = str(item.get("filial") or "").strip()
        for b in bloqueios_inauguracao:
            bloqueios_detalhe.append({"filial": filial, "loja": str(item.get("descricao_filial") or ""), "bloqueio": b})

        lojas.append({
            "id": item.get("id"),
            "filial": filial,
            "descricao_filial": str(item.get("descricao_filial") or "").strip(),
            "bandeira": str(item.get("bandeira") or "").strip().upper(),
            "uf": str(item.get("uf") or "").strip().upper(),
            "projeto": str(item.get("projeto") or "").strip().upper(),
            "status_filial": str(item.get("status_filial") or "").strip().upper(),
            "readiness": score,
            "faixa": faixa,
            "term_obra": _data_acompanhamento_legivel(item.get("term_obra")),
            "enviada": "SIM" if enviada_ok else "NÃO",
            "em_separacao": "SIM" if separacao_ok else "NÃO",
            "equip_separado": "SIM" if equip_ok else "NÃO",
            "entrada_ti": _data_acompanhamento_legivel(item.get("entrada_ti")),
            "entrada_ti_iso": entrada_iso,
            "dias_entrada_ti": dias_ti,
            "inauguracao": _data_acompanhamento_legivel(item.get("inauguracao")),
            "inauguracao_iso": inaug_iso,
            "dias_inauguracao": dias_inaug,
            "estoque_situacao": "OK" if not faltantes else "RISCO",
            "itens_faltantes": len(faltantes),
            "unidades_faltantes": sum(x["quantidade"] for x in faltantes),
            "faltantes": faltantes,
            "bloqueios": bloqueios_inauguracao,
            "bloqueios_total": len(bloqueios_inauguracao),
            "valor_faltante": format(valor_loja, ".2f") if incluir_financeiro else None,
            "observacao_ti": str(item.get("observacao_ti") or "").strip(),
        })

    prontas = sum(1 for x in lojas if x["faixa"] == "PRONTA")
    atencao = sum(1 for x in lojas if x["faixa"] == "ATENCAO")
    criticas = sum(1 for x in lojas if x["faixa"] == "CRITICA")
    pepi = _decimal_moeda(db.obter_orcamento_pepi_consolidado(), "0.00") if incluir_financeiro else Decimal("0.00")
    saldo = (pepi - valor_total_faltante).quantize(Decimal("0.01")) if incluir_financeiro else Decimal("0.00")

    projetos = {}
    ufs = {}
    for loja in lojas:
        projetos[loja["projeto"] or "SEM PROJETO"] = projetos.get(loja["projeto"] or "SEM PROJETO", 0) + 1
        ufs[loja["uf"] or "SEM UF"] = ufs.get(loja["uf"] or "SEM UF", 0) + 1

    return {
        "gerado_em": datetime.now().strftime("%d/%m/%Y %H:%M"),
        "resumo": {
            "total_acompanhado": len(linhas_acomp),
            "inauguradas": inauguradas,
            "pendentes": len(lojas),
            "prontas": prontas,
            "atencao": atencao,
            "criticas": criticas,
            "itens_faltantes": len(itens_faltantes_codigos),
            "unidades_faltantes": unidades_faltantes,
            "bloqueios": len(bloqueios_detalhe),
            "readiness_medio": round(sum(x["readiness"] for x in lojas) / max(1, len(lojas)), 1),
            "valor_faltante": format(valor_total_faltante, ".2f") if incluir_financeiro else None,
            "pepi_disponivel": format(pepi, ".2f") if incluir_financeiro else None,
            "saldo_pepi": format(saldo, ".2f") if incluir_financeiro else None,
        },
        "criterios": [
            {"nome": "Término da obra definido", "peso": 15},
            {"nome": "Enviada = Sim", "peso": 15},
            {"nome": "Em Separação = Sim", "peso": 15},
            {"nome": "Equip. separado = Sim", "peso": 20},
            {"nome": "Entrada de TI definida", "peso": 20},
            {"nome": "Inauguração definida", "peso": 15},
        ],
        "projetos": projetos,
        "ufs": dict(sorted(ufs.items(), key=lambda kv: (-kv[1], kv[0]))),
        "lojas": lojas,
        "faltantes": faltantes_detalhe,
        "bloqueios": bloqueios_detalhe,
        "financeiro_disponivel": bool(incluir_financeiro),
    }


def _gerar_excel_cockpit_implantacao(dados):
    wb = Workbook()
    ws = wb.active
    ws.title = "Resumo"
    azul = "244C74"
    azul2 = "172433"
    verde = "2E8B65"
    amarelo = "B87916"
    vermelho = "A84343"
    branco = "FFFFFF"
    borda = Border(bottom=Side(style="thin", color="D8DEE6"))

    ws["A1"] = "COCKPIT DE IMPLANTAÇÃO · EXPANSÃO DE TI"
    ws["A1"].font = Font(size=16, bold=True, color=branco)
    ws["A1"].fill = PatternFill("solid", fgColor=azul)
    ws.merge_cells("A1:D1")
    ws["A2"] = "Gerado em"
    ws["B2"] = dados.get("gerado_em")
    resumo = dados.get("resumo") or {}
    linhas_resumo = [
        ("Total acompanhado", resumo.get("total_acompanhado")),
        ("Inauguradas", resumo.get("inauguradas")),
        ("Pendentes", resumo.get("pendentes")),
        ("Prontas", resumo.get("prontas")),
        ("Atenção", resumo.get("atencao")),
        ("Críticas", resumo.get("criticas")),
        ("Readiness médio", f"{resumo.get('readiness_medio',0)}%"),
        ("Itens faltantes", resumo.get("itens_faltantes")),
        ("Unidades faltantes", resumo.get("unidades_faltantes")),
        ("Possíveis bloqueios na inauguração", resumo.get("bloqueios")),
    ]
    if dados.get("financeiro_disponivel"):
        linhas_resumo += [
            ("Valor estimado faltante", float(resumo.get("valor_faltante") or 0)),
            ("PEPI disponível", float(resumo.get("pepi_disponivel") or 0)),
            ("Saldo PEPI", float(resumo.get("saldo_pepi") or 0)),
        ]
    for i, (rot, val) in enumerate(linhas_resumo, start=4):
        ws.cell(i, 1, rot).font = Font(bold=True, color="3A4654")
        ws.cell(i, 2, val)
        if "Valor" in rot or "PEPI" in rot:
            ws.cell(i, 2).number_format = 'R$ #,##0.00'
    ws.column_dimensions["A"].width = 29
    ws.column_dimensions["B"].width = 20

    lojas_ws = wb.create_sheet("Lojas")
    headers = ["Filial","Loja","Bandeira","UF","Projeto","Readiness %","Faixa","Término obra","Enviada","Em Separação","Equip. separado","Entrada TI","Dias p/ TI","Inauguração","Dias p/ inaug.","Estoque","Itens faltantes","Unid. faltantes","Possíveis bloqueios na inauguração"]
    if dados.get("financeiro_disponivel"):
        headers.append("Valor faltante")
    for c, h in enumerate(headers, 1):
        cell = lojas_ws.cell(1, c, h); cell.font=Font(bold=True,color=branco); cell.fill=PatternFill("solid",fgColor=azul); cell.alignment=Alignment(horizontal="center")
    for r_idx, loja in enumerate(dados.get("lojas") or [], 2):
        vals = [
            loja.get("filial"), loja.get("descricao_filial"), loja.get("bandeira"), loja.get("uf"), loja.get("projeto"), loja.get("readiness"), loja.get("faixa"), loja.get("term_obra"), loja.get("enviada"), loja.get("em_separacao"), loja.get("equip_separado"), loja.get("entrada_ti"), loja.get("dias_entrada_ti"), loja.get("inauguracao"), loja.get("dias_inauguracao"), loja.get("estoque_situacao"), loja.get("itens_faltantes"), loja.get("unidades_faltantes"), " | ".join(loja.get("bloqueios") or []),
        ]
        if dados.get("financeiro_disponivel"):
            vals.append(float(loja.get("valor_faltante") or 0))
        for c, v in enumerate(vals, 1):
            lojas_ws.cell(r_idx, c, v).border = borda
        if dados.get("financeiro_disponivel"):
            lojas_ws.cell(r_idx, len(headers)).number_format = 'R$ #,##0.00'
        faixa = loja.get("faixa")
        fill = verde if faixa == "PRONTA" else amarelo if faixa == "ATENCAO" else vermelho
        lojas_ws.cell(r_idx, 7).fill = PatternFill("solid", fgColor=fill)
        lojas_ws.cell(r_idx, 7).font = Font(color=branco, bold=True)
    lojas_ws.freeze_panes = "A2"
    lojas_ws.auto_filter.ref = lojas_ws.dimensions
    widths=[12,34,11,7,15,13,13,15,11,14,15,15,11,15,13,11,14,14,60,17]
    for i,w in enumerate(widths[:len(headers)],1): lojas_ws.column_dimensions[get_column_letter(i)].width=w

    falt_ws = wb.create_sheet("Itens faltantes")
    fh=["Filial","Loja","Código","Produto","Qtd. faltante","Custo unitário","Valor estimado","Custo informado"]
    for c,h in enumerate(fh,1):
        cell=falt_ws.cell(1,c,h); cell.font=Font(bold=True,color=branco); cell.fill=PatternFill("solid",fgColor=azul)
    for r_idx, item in enumerate(dados.get("faltantes") or [],2):
        vals=[item.get("filial"),item.get("loja"),item.get("codigo"),item.get("descricao"),item.get("quantidade"),float(item.get("custo") or 0),float(item.get("valor") or 0),"SIM" if item.get("custo_informado") else "NÃO"]
        for c,v in enumerate(vals,1): falt_ws.cell(r_idx,c,v).border=borda
        falt_ws.cell(r_idx,6).number_format='R$ #,##0.00'; falt_ws.cell(r_idx,7).number_format='R$ #,##0.00'
    falt_ws.freeze_panes="A2"; falt_ws.auto_filter.ref=falt_ws.dimensions
    for i,w in enumerate([12,34,14,38,15,16,16,16],1): falt_ws.column_dimensions[get_column_letter(i)].width=w

    bloq_ws = wb.create_sheet("Bloqueios inauguração")
    bh=["Filial","Loja","Possível bloqueio para inauguração"]
    for c,h in enumerate(bh,1):
        cell=bloq_ws.cell(1,c,h); cell.font=Font(bold=True,color=branco); cell.fill=PatternFill("solid",fgColor=azul)
    for r_idx, item in enumerate(dados.get("bloqueios") or [],2):
        for c,v in enumerate([item.get("filial"),item.get("loja"),item.get("bloqueio")],1): bloq_ws.cell(r_idx,c,v).border=borda
    bloq_ws.freeze_panes="A2"; bloq_ws.auto_filter.ref=bloq_ws.dimensions
    bloq_ws.column_dimensions["A"].width=12; bloq_ws.column_dimensions["B"].width=36; bloq_ws.column_dimensions["C"].width=60

    crit_ws = wb.create_sheet("Critérios Readiness")
    crit_ws.append(["Critério","Peso (%)"])
    for c in crit_ws[1]: c.font=Font(bold=True,color=branco); c.fill=PatternFill("solid",fgColor=azul)
    for c in dados.get("criterios") or []: crit_ws.append([c.get("nome"),c.get("peso")])
    crit_ws.column_dimensions["A"].width=40; crit_ws.column_dimensions["B"].width=12

    buf=io.BytesIO(); wb.save(buf); buf.seek(0); return buf


def _gerar_pdf_cockpit_implantacao(dados):
    buf = io.BytesIO()
    page_size = landscape(A4)
    pdf = canvas.Canvas(buf, pagesize=page_size)
    larg, alt = page_size
    margem = 14 * mm
    resumo = dados.get("resumo") or {}
    lojas = dados.get("lojas") or []

    def bg():
        pdf.setFillColor(colors.HexColor("#0F1620")); pdf.rect(0,0,larg,alt,stroke=0,fill=1)
    def footer(n):
        pdf.setStrokeColor(colors.HexColor("#283646")); pdf.line(margem,11*mm,larg-margem,11*mm)
        pdf.setFillColor(colors.HexColor("#8398AD")); pdf.setFont("Helvetica",7.2)
        pdf.drawString(margem,6.5*mm,"© 2026 · Developed by ALM - Expansão de TI · Cockpit de Implantação")
        pdf.drawRightString(larg-margem,6.5*mm,f"Página {n}")
    def kpi(x,y,w,h,titulo,valor,detalhe,cor="#FFFFFF"):
        pdf.setFillColor(colors.HexColor("#182230")); pdf.setStrokeColor(colors.HexColor("#314255")); pdf.roundRect(x,y,w,h,9,stroke=1,fill=1)
        pdf.setFillColor(colors.HexColor("#94A9BC")); pdf.setFont("Helvetica-Bold",6.8); pdf.drawString(x+8,y+h-13,titulo.upper())
        pdf.setFillColor(colors.HexColor(cor)); pdf.setFont("Helvetica-Bold",16); pdf.drawString(x+8,y+18,str(valor))
        pdf.setFillColor(colors.HexColor("#7990A6")); pdf.setFont("Helvetica",6.3); pdf.drawString(x+8,y+7,str(detalhe)[:34])
    def titulo_pagina(titulo, subtitulo):
        bg(); pdf.setFillColor(colors.white); pdf.setFont("Helvetica-Bold",19); pdf.drawString(margem,alt-margem,titulo)
        pdf.setFillColor(colors.HexColor("#9DB3C8")); pdf.setFont("Helvetica",8); pdf.drawString(margem,alt-margem-14,subtitulo)
        pdf.drawRightString(larg-margem,alt-margem-14,f"Gerado em {dados.get('gerado_em') or '-'}")

    titulo_pagina("Cockpit de Implantação","Readiness operacional das lojas, bloqueios, cobertura de equipamentos e cronograma de TI.")
    cards=[
        ("Pendentes",resumo.get("pendentes",0),"Lojas em implantação","#FFFFFF"),
        ("Prontas",resumo.get("prontas",0),"Readiness ≥ 80% e sem falta","#4CD792"),
        ("Atenção",resumo.get("atencao",0),"Readiness entre 50% e 79%","#FFB648"),
        ("Críticas",resumo.get("criticas",0),"Readiness abaixo de 50%","#FF6B6B"),
        ("Readiness médio",f"{resumo.get('readiness_medio',0):.1f}%","Média das lojas pendentes","#3EA6FF"),
        ("Unid. faltantes",resumo.get("unidades_faltantes",0),f"{resumo.get('itens_faltantes',0)} item(ns) do kit","#A78BFA"),
    ]
    gap=8; y=alt-margem-66; h=47; w=(larg-2*margem-gap*5)/6
    for i,c in enumerate(cards): kpi(margem+i*(w+gap),y,w,h,*c)

    panel_y=23*mm; panel_h=y-panel_y-14; left_w=(larg-2*margem-10)*.58; right_x=margem+left_w+10; right_w=larg-margem-right_x
    pdf.setFillColor(colors.HexColor("#151D27")); pdf.setStrokeColor(colors.HexColor("#2A3645")); pdf.roundRect(margem,panel_y,left_w,panel_h,10,stroke=1,fill=1)
    pdf.setFillColor(colors.white); pdf.setFont("Helvetica-Bold",11); pdf.drawString(margem+12,panel_y+panel_h-21,"Lojas que exigem atenção")
    pdf.setFillColor(colors.HexColor("#8FA5BA")); pdf.setFont("Helvetica",7.3); pdf.drawString(margem+12,panel_y+panel_h-33,"Priorização por menor readiness e proximidade de inauguração.")
    criticas = sorted(lojas,key=lambda x:(x.get("readiness",0), x.get("inauguracao_iso") or "9999-99-99"))[:8]
    cy=panel_y+panel_h-55
    for loja in criticas:
        cor="#FF6B6B" if loja.get("faixa")=="CRITICA" else "#FFB648" if loja.get("faixa")=="ATENCAO" else "#4CD792"
        pdf.setFillColor(colors.HexColor("#182230")); pdf.roundRect(margem+12,cy-14,left_w-24,24,5,stroke=0,fill=1)
        pdf.setFillColor(colors.HexColor(cor)); pdf.setFont("Helvetica-Bold",8); pdf.drawString(margem+20,cy-1,f"{loja.get('filial')} · {str(loja.get('descricao_filial') or '')[:31]}")
        pdf.setFillColor(colors.HexColor("#A7B8C8")); pdf.setFont("Helvetica",6.8); pdf.drawString(margem+20,cy-10,f"{loja.get('projeto')} · {loja.get('uf')} · Inaug.: {loja.get('inauguracao')} · {loja.get('readiness')}%")
        pdf.setFillColor(colors.HexColor(cor)); pdf.setFont("Helvetica-Bold",8); pdf.drawRightString(margem+left_w-20,cy-4,loja.get("faixa"))
        cy-=29

    pdf.setFillColor(colors.HexColor("#151D27")); pdf.setStrokeColor(colors.HexColor("#2A3645")); pdf.roundRect(right_x,panel_y,right_w,panel_h,10,stroke=1,fill=1)
    pdf.setFillColor(colors.white); pdf.setFont("Helvetica-Bold",11); pdf.drawString(right_x+12,panel_y+panel_h-21,"Riscos consolidados")
    pdf.setFillColor(colors.HexColor("#9DB3C8")); pdf.setFont("Helvetica",7.3); pdf.drawString(right_x+12,panel_y+panel_h-33,"Possíveis bloqueios que podem comprometer a data de inauguração.")
    metrics=[
        ("Bloqueios de inauguração",resumo.get("bloqueios",0),"#FFB648"),
        ("Itens do kit faltantes",resumo.get("itens_faltantes",0),"#A78BFA"),
        ("Unidades faltantes",resumo.get("unidades_faltantes",0),"#FF6B6B"),
        ("Inauguradas",resumo.get("inauguradas",0),"#4CD792"),
    ]
    my=panel_y+panel_h-62
    for label,val,cor in metrics:
        pdf.setFillColor(colors.HexColor("#1B2531")); pdf.roundRect(right_x+12,my-9,right_w-24,22,5,stroke=0,fill=1)
        pdf.setFillColor(colors.HexColor("#C8D6E3")); pdf.setFont("Helvetica",7.5); pdf.drawString(right_x+20,my,label)
        pdf.setFillColor(colors.HexColor(cor)); pdf.setFont("Helvetica-Bold",10); pdf.drawRightString(right_x+right_w-20,my,str(val)); my-=28
    if dados.get("financeiro_disponivel"):
        my-=4
        for label,key,cor in [("Valor estimado faltante","valor_faltante","#FFB648"),("PEPI disponível","pepi_disponivel","#3EA6FF"),("Saldo após cobertura","saldo_pepi","#4CD792")]:
            try: valor=f"R$ {float(resumo.get(key) or 0):,.2f}".replace(",","X").replace(".",",").replace("X",".")
            except Exception: valor="R$ 0,00"
            pdf.setFillColor(colors.HexColor("#1B2531")); pdf.roundRect(right_x+12,my-9,right_w-24,22,5,stroke=0,fill=1)
            pdf.setFillColor(colors.HexColor("#C8D6E3")); pdf.setFont("Helvetica",7.2); pdf.drawString(right_x+20,my,label)
            pdf.setFillColor(colors.HexColor(cor)); pdf.setFont("Helvetica-Bold",8.5); pdf.drawRightString(right_x+right_w-20,my,valor); my-=27
    footer(1); pdf.showPage()

    page_no=2
    cols=[("Filial",38),("Loja",116),("Proj.",56),("UF",24),("Ready",40),("Faixa",52),("Env.",30),("Sep.",30),("Equip.",34),("Entrada TI",58),("Inaug.",58),("Estoque",43),("Bloq.",34)]
    if dados.get("financeiro_disponivel"): cols.append(("Valor",62))
    table_w=sum(w for _,w in cols); row_h=20
    def header_detail():
        titulo_pagina("Detalhamento do Cockpit de Implantação","Situação por loja pendente, com marcos operacionais, estoque e possíveis bloqueios para inauguração.")
        y0=alt-margem-42
        pdf.setFillColor(colors.HexColor("#234C74")); pdf.roundRect(margem,y0,table_w,20,4,stroke=0,fill=1)
        pdf.setFillColor(colors.white); pdf.setFont("Helvetica-Bold",6.6); cx=margem
        for title,wc in cols: pdf.drawString(cx+3,y0+6,title); cx+=wc
        return y0-3
    y0=header_detail()
    for loja in lojas:
        extra = 15 if loja.get("bloqueios") else 0
        if y0-row_h-extra < 19*mm:
            footer(page_no); pdf.showPage(); page_no+=1; y0=header_detail()
        y0-=row_h
        pdf.setFillColor(colors.HexColor("#182230")); pdf.roundRect(margem,y0,table_w,row_h-1,3,stroke=0,fill=1)
        vals=[loja.get("filial"),loja.get("descricao_filial"),loja.get("projeto"),loja.get("uf"),f"{loja.get('readiness')}%",loja.get("faixa"),loja.get("enviada"),loja.get("em_separacao"),loja.get("equip_separado"),loja.get("entrada_ti"),loja.get("inauguracao"),loja.get("estoque_situacao"),loja.get("bloqueios_total")]
        if dados.get("financeiro_disponivel"):
            try: vals.append(f"R$ {float(loja.get('valor_faltante') or 0):,.0f}".replace(",","."))
            except Exception: vals.append("R$ 0")
        cx=margem
        for (title,wc),val in zip(cols,vals):
            txt=str(val if val is not None else "-"); maxc=max(4,int((wc-6)/4.1)); txt=txt if len(txt)<=maxc else txt[:maxc-1]+"…"
            if title=="Faixa":
                cor="#4CD792" if txt=="PRONTA" else "#FFB648" if txt=="ATENCAO" else "#FF6B6B"; pdf.setFillColor(colors.HexColor(cor)); pdf.setFont("Helvetica-Bold",6.4)
            elif title=="Estoque":
                pdf.setFillColor(colors.HexColor("#4CD792" if txt=="OK" else "#FF6B6B")); pdf.setFont("Helvetica-Bold",6.4)
            else:
                pdf.setFillColor(colors.HexColor("#DCE6F0")); pdf.setFont("Helvetica",6.3)
            pdf.drawString(cx+3,y0+7,txt); cx+=wc
        if loja.get("bloqueios"):
            y0-=15; pdf.setFillColor(colors.HexColor("#101923")); pdf.roundRect(margem,y0+2,table_w,12,3,stroke=0,fill=1)
            pdf.setFillColor(colors.HexColor("#91A7BD")); pdf.setFont("Helvetica",6.1)
            txt="Possíveis bloqueios: "+" · ".join(loja.get("bloqueios")[:3]); txt=txt if len(txt)<150 else txt[:147]+"…"; pdf.drawString(margem+5,y0+6,txt)
        y0-=3
    footer(page_no); pdf.save(); buf.seek(0); return buf



# ---------------------------------------------------------------------
# Central de Pendências e Ações
# ---------------------------------------------------------------------

def _parse_data_simples(valor):
    texto=str(valor or '').strip()
    if not texto:
        return None
    for fmt in ('%Y-%m-%d','%d/%m/%Y'):
        try:
            return datetime.strptime(texto[:10],fmt).date()
        except Exception:
            pass
    return None



_NOTIFICATION_NEXT_CHECK = 0.0
_NOTIFICATION_CHECK_LOCK = threading.Lock()


def _split_destinatarios(valor):
    return [x.strip() for x in re.split(r'[,;\n]+', str(valor or '')) if x.strip()]


def _normalizar_whatsapp(valor):
    return re.sub(r'\D+', '', str(valor or ''))


def _notificacao_agora_local():
    """Horário local usado para a agenda semanal. Padrão: UTC-3 (São Paulo)."""
    try:
        offset = int(os.environ.get('PENDENCIA_WEEKLY_UTC_OFFSET', '-3'))
    except Exception:
        offset = -3
    offset = max(-12, min(14, offset))
    return datetime.utcnow() + timedelta(hours=offset)


def _status_notificacoes():
    smtp_ok = bool(os.environ.get('SMTP_HOST') and os.environ.get('SMTP_FROM'))
    whatsapp_ok = bool(
        os.environ.get('WHATSAPP_WEBHOOK_URL')
        or (os.environ.get('WHATSAPP_API_URL') and os.environ.get('WHATSAPP_TOKEN'))
    )
    try:
        dias = max(0, int(os.environ.get('PENDENCIA_ALERT_DAYS', '7')))
    except Exception:
        dias = 7
    try:
        dia_semana = int(os.environ.get('PENDENCIA_WEEKLY_WEEKDAY', '0'))
    except Exception:
        dia_semana = 0
    try:
        hora = int(os.environ.get('PENDENCIA_WEEKLY_HOUR', '8'))
    except Exception:
        hora = 8
    dia_semana = max(0, min(6, dia_semana))
    hora = max(0, min(23, hora))
    return {
        'email_configurado': smtp_ok,
        'whatsapp_configurado': whatsapp_ok,
        'dias_proximidade': dias,
        'automatico': smtp_ok or whatsapp_ok,
        'cron_configurado': bool(os.environ.get('NOTIFICATION_CRON_TOKEN')),
        'frequencia': 'SEMANAL',
        'dia_semana': dia_semana,
        'hora': hora,
    }


def _gatilhos_pendencia(item, hoje=None):
    hoje = hoje or _notificacao_agora_local().date()
    status = str(item.get('status') or 'ABERTA').strip().upper()
    if status in {'CONCLUIDA','CANCELADA'}:
        return []
    out=[]
    if str(item.get('prioridade') or '').strip().upper() == 'CRITICA':
        out.append('PRIORIDADE CRÍTICA')
    prazo = _parse_data_simples(item.get('prazo'))
    if prazo:
        dias = (prazo-hoje).days
        try:
            limite=max(0,int(os.environ.get('PENDENCIA_ALERT_DAYS','7')))
        except Exception:
            limite=7
        if dias < 0:
            out.append('VENCIDA')
        elif dias <= limite:
            out.append('PRÓXIMA DO PRAZO' if dias > 0 else 'VENCE HOJE')
    return out


def _lojas_pendentes_resumo_semanal():
    """Lojas PENDENTES do Acompanhamento, com os dois marcos solicitados."""
    lojas=[]
    for x in db.listar_acompanhamento_expansao():
        if _normalizar_exec(x.get('status_filial')) != 'pendente':
            continue
        lojas.append({
            'filial': str(x.get('filial') or '').strip() or '-',
            'loja': str(x.get('descricao_filial') or '').strip() or '-',
            'uf': str(x.get('uf') or '').strip().upper() or '-',
            'projeto': str(x.get('projeto') or '').strip() or '-',
            'entrada_ti': _data_acompanhamento_legivel(x.get('entrada_ti')),
            'entrada_ti_iso': _data_acompanhamento_iso(x.get('entrada_ti')),
            'inauguracao': _data_acompanhamento_legivel(x.get('inauguracao')),
            'inauguracao_iso': _data_acompanhamento_iso(x.get('inauguracao')),
        })
    lojas.sort(key=lambda x:(x.get('inauguracao_iso') or '9999-99-99', x.get('entrada_ti_iso') or '9999-99-99', x.get('filial') or ''))
    return lojas


def _contatos_responsavel(item):
    emails=[]; whats=[]
    resp=str(item.get('responsavel') or '').strip()
    if resp:
        u=db.buscar_usuario_por_username(resp)
        if u:
            if u.get('email'): emails.append(str(u.get('email')).strip())
            if u.get('whatsapp'): whats.append(str(u.get('whatsapp')).strip())
    return list(dict.fromkeys(x for x in emails if x)), list(dict.fromkeys(x for x in whats if x))


def _destinatarios_resumo_semanal(pendencias):
    """Retorna destinatários e as pendências que devem aparecer no resumo de cada um."""
    email_map={}; whats_map={}
    todas_ids=[int(x.get('id') or 0) for x in pendencias]
    for dest in _split_destinatarios(os.environ.get('PENDENCIA_ALERT_EMAILS')):
        email_map[dest]=set(todas_ids)
    for dest in _split_destinatarios(os.environ.get('PENDENCIA_ALERT_WHATSAPP')):
        whats_map[dest]=set(todas_ids)
    for item in pendencias:
        item_id=int(item.get('id') or 0)
        emails, whats=_contatos_responsavel(item)
        for dest in emails: email_map.setdefault(dest,set()).add(item_id)
        for dest in whats: whats_map.setdefault(dest,set()).add(item_id)
    by_id={int(x.get('id') or 0):x for x in pendencias}
    def expand(mapa):
        return {dest:[by_id[i] for i in sorted(ids) if i in by_id] for dest,ids in mapa.items()}
    return expand(email_map), expand(whats_map)



def _pendencias_resumo_usuario(username, hoje=None):
    """Pendências do resumo manual de um usuário, apenas quando ele é o responsável."""
    hoje = hoje or _notificacao_agora_local().date()
    alvo = str(username or '').strip().casefold()
    if not alvo:
        return []
    itens=[]
    for item in db.listar_pendencias_acoes():
        responsavel=str(item.get('responsavel') or '').strip().casefold()
        if responsavel != alvo:
            continue
        if _gatilhos_pendencia(item, hoje):
            itens.append(item)
    return itens


def _reservar_envio_manual_usuario(canal, destinatario):
    agora=_notificacao_agora_local()
    bruto=f"RESUMO_MANUAL_USUARIO|{canal}|{destinatario}|{agora.isoformat()}|{session.get('username')}"
    fingerprint=hashlib.sha256(bruto.encode('utf-8')).hexdigest()
    if not db.reservar_notificacao_pendencia(0,canal,'RESUMO MANUAL USUÁRIO',destinatario,fingerprint):
        return None
    return fingerprint

def _texto_resumo_semanal(pendencias, lojas, gerado_em=None):
    agora=gerado_em or _notificacao_agora_local()
    vencidas=sum(1 for x in pendencias if 'VENCIDA' in _gatilhos_pendencia(x,agora.date()))
    criticas=sum(1 for x in pendencias if str(x.get('prioridade') or '').strip().upper()=='CRITICA')
    proximas=sum(1 for x in pendencias if any(g in _gatilhos_pendencia(x,agora.date()) for g in ('PRÓXIMA DO PRAZO','VENCE HOJE')))
    linhas=[
        'RESUMO SEMANAL · EXPANSÃO DE TI',
        f"Gerado em: {agora.strftime('%d/%m/%Y %H:%M')}",
        '',
        'PENDÊNCIAS E AÇÕES',
        f"Total no resumo: {len(pendencias)} | Vencidas: {vencidas} | Próximas do prazo: {proximas} | Críticas: {criticas}",
    ]
    if pendencias:
        for x in sorted(pendencias,key=lambda z:(_parse_data_simples(z.get('prazo')) or datetime(9999,12,31).date(), int(z.get('id') or 0))):
            gat=' / '.join(_gatilhos_pendencia(x,agora.date())) or 'ACOMPANHAMENTO'
            linhas.append(f"• #{x.get('id')} | Filial {x.get('filial') or '-'} | {x.get('titulo') or 'Pendência'} | Resp.: {x.get('responsavel') or 'Não definido'} | Prazo: {x.get('prazo') or 'Sem prazo'} | {gat}")
    else:
        linhas.append('• Nenhuma pendência vencida, crítica ou próxima do prazo neste ciclo.')
    linhas += ['', 'LOJAS PENDENTES DE INAUGURAÇÃO', f'Total: {len(lojas)}']
    if lojas:
        for x in lojas:
            linhas.append(f"• Filial {x.get('filial')} | {x.get('loja')} | {x.get('uf')} | Entrada TI: {x.get('entrada_ti')} | Inauguração: {x.get('inauguracao')}")
    else:
        linhas.append('• Nenhuma loja pendente de inauguração.')
    base_url=(os.environ.get('APP_PUBLIC_URL') or '').strip().rstrip('/')
    if base_url:
        linhas += ['', f'Cockpit de Implantação: {base_url}/cockpit-implantacao', f'Acompanhamento de Expansão: {base_url}/acompanhamento-expansao']
    linhas += ['', 'Mensagem automática · Developed by ALM - Expansão de TI']
    return '\n'.join(linhas)


def _limpar_cabecalho_email(valor, campo, email=False):
    # Evita erro do EmailMessage e bloqueia header injection por CR/LF ocultos.
    bruto=str(valor or '')
    limpo=re.sub(r'[\r\n]+', ' ' if not email else '', bruto).strip()
    if not limpo:
        raise RuntimeError(f'{campo} não informado.')
    if email:
        # E-mail de cabeçalho deve ser um único endereço simples.
        limpo=re.sub(r'\s+', '', limpo)
        if not re.match(r'^[^\s@<>]+@[^\s@<>]+\.[^\s@<>]+$', limpo):
            raise RuntimeError(f'{campo} inválido: verifique o endereço cadastrado.')
    return limpo


def _enviar_email_texto(*args, **kwargs):
    raise RuntimeError('Envio por e-mail desativado no sistema.')


def _quebrar_mensagem_whatsapp(texto, limite=2800):
    partes=[]; atual=[]; tamanho=0
    for linha in str(texto or '').splitlines():
        extra=len(linha)+1
        if atual and tamanho+extra>limite:
            partes.append('\n'.join(atual)); atual=[]; tamanho=0
        if len(linha)>limite:
            for i in range(0,len(linha),limite):
                trecho=linha[i:i+limite]
                if atual: partes.append('\n'.join(atual)); atual=[]; tamanho=0
                partes.append(trecho)
            continue
        atual.append(linha); tamanho+=extra
    if atual: partes.append('\n'.join(atual))
    return partes or ['Resumo semanal sem conteúdo.']


def _enviar_whatsapp_texto(*args, **kwargs):
    raise RuntimeError('Envio por WhatsApp desativado no sistema.')


def _processar_notificacoes_pendencias(force=False, only_id=None):
    # Funcionalidade de notificações desativada a partir da v109.
    return {'ok': False, 'desativado': True, 'mensagem': 'Envios por e-mail e WhatsApp estão desativados.'}


def _notificacao_background_worker():
    try: _processar_notificacoes_pendencias()
    except Exception as exc: print(f"[notificacao] Verificação automática falhou: {exc}")


@app.before_request
def _agendar_notificacoes_automaticas():
    # Notificações externas desativadas na v109.
    return None

def _dados_central_pendencias(filial=None):
    hoje=datetime.now().date()
    linhas=db.listar_pendencias_acoes()
    if filial:
        alvo=str(filial).strip().lower()
        linhas=[x for x in linhas if str(x.get('filial') or '').strip().lower()==alvo]
    acomp={str(x.get('filial') or '').strip():x for x in db.listar_acompanhamento_expansao()}
    abertas=[]; vencidas=[]; hoje_arr=[]; proximas=[]; criticas=[]; sem_resp=[]
    status_fechados={'CONCLUIDA','CANCELADA'}
    por_status={}; por_prioridade={}; por_responsavel={}
    for x in linhas:
        status=str(x.get('status') or 'ABERTA').strip().upper()
        prioridade=str(x.get('prioridade') or 'MEDIA').strip().upper()
        prazo=_parse_data_simples(x.get('prazo'))
        dias=(prazo-hoje).days if prazo else None
        filial_x=str(x.get('filial') or '').strip()
        ac=acomp.get(filial_x,{})
        x['loja']=str(ac.get('descricao_filial') or '').strip()
        x['projeto']=str(ac.get('projeto') or '').strip().upper()
        x['uf']=str(ac.get('uf') or '').strip().upper()
        x['dias_prazo']=dias
        x['vencida']=bool(status not in status_fechados and dias is not None and dias<0)
        x['vence_hoje']=bool(status not in status_fechados and dias==0)
        x['alerta_3d']=bool(status not in status_fechados and dias is not None and 0<dias<=3)
        x['sem_responsavel']=bool(status not in status_fechados and not str(x.get('responsavel') or '').strip())
        por_status[status]=por_status.get(status,0)+1
        por_prioridade[prioridade]=por_prioridade.get(prioridade,0)+1
        resp=str(x.get('responsavel') or 'Sem responsável').strip() or 'Sem responsável'
        por_responsavel[resp]=por_responsavel.get(resp,0)+1
        if status not in status_fechados: abertas.append(x)
        if x['vencida']: vencidas.append(x)
        if x['vence_hoje']: hoje_arr.append(x)
        if x['alerta_3d']: proximas.append(x)
        if status not in status_fechados and prioridade=='CRITICA': criticas.append(x)
        if x['sem_responsavel']: sem_resp.append(x)
    notificacoes=db.listar_notificacoes_pendencias(limit=120)
    notif_resumo={
        'enviadas':sum(1 for n in notificacoes if str(n.get('status') or '').upper()=='ENVIADO'),
        'email':sum(1 for n in notificacoes if str(n.get('canal') or '').upper()=='EMAIL' and str(n.get('status') or '').upper()=='ENVIADO'),
        'whatsapp':sum(1 for n in notificacoes if str(n.get('canal') or '').upper()=='WHATSAPP' and str(n.get('status') or '').upper()=='ENVIADO'),
    }
    return {
        'gerado_em':datetime.now().strftime('%d/%m/%Y %H:%M'),
        'resumo':{
            'total':len(linhas),'abertas':len(abertas),'vencidas':len(vencidas),'vence_hoje':len(hoje_arr),
            'proximas_3d':len(proximas),'criticas':len(criticas),'sem_responsavel':len(sem_resp),
            'concluidas':sum(1 for x in linhas if str(x.get('status') or '').upper()=='CONCLUIDA'),
        },
        'alertas':{'vencidas':vencidas,'vence_hoje':hoje_arr,'proximas_3d':proximas,'criticas':criticas,'sem_responsavel':sem_resp},
        'por_status':por_status,'por_prioridade':por_prioridade,'por_responsavel':dict(sorted(por_responsavel.items(),key=lambda kv:(-kv[1],kv[0]))),
        'pendencias':linhas,
        'notificacoes':notificacoes,
        'notificacoes_resumo':notif_resumo,
        'notificacoes_status':_status_notificacoes(),
    }


@app.route('/central-pendencias')
@login_required
def pagina_central_pendencias():
    # Aba desativada na v108. Mantemos os dados no banco sem expor a tela.
    return redirect(url_for('pagina_cockpit_implantacao'))


@app.route('/api/pendencias', methods=['GET'])
@login_required
def api_listar_pendencias():
    return jsonify(_dados_central_pendencias(request.args.get('filial')))


@app.route('/api/pendencias/<int:pendencia_id>', methods=['GET'])
@login_required
def api_detalhar_pendencia(pendencia_id):
    item=db.buscar_pendencia_acao(pendencia_id)
    if not item: return jsonify({'erro':'Pendência não encontrada.'}),404
    item['comentarios']=db.listar_comentarios_pendencia(pendencia_id)
    item['evidencias']=db.listar_evidencias_pendencia(pendencia_id)
    item['notificacoes']=db.listar_notificacoes_pendencias(limit=30,pendencia_id=pendencia_id)
    return jsonify(item)


def _validar_pendencia_payload(dados, parcial=False):
    permit_status={'ABERTA','EM ANDAMENTO','AGUARDANDO','CONCLUIDA','CANCELADA'}
    permit_prio={'BAIXA','MEDIA','ALTA','CRITICA'}
    if not parcial and not str(dados.get('titulo') or '').strip(): return 'Informe o título da pendência/ação.'
    if 'status' in dados and str(dados.get('status') or '').strip().upper() not in permit_status: return 'Status inválido.'
    if 'prioridade' in dados and str(dados.get('prioridade') or '').strip().upper() not in permit_prio: return 'Prioridade inválida.'
    prazo=str(dados.get('prazo') or '').strip()
    if prazo and not _parse_data_simples(prazo): return 'Prazo inválido.'
    return None


@app.route('/api/pendencias', methods=['POST'])
@role_required('admin','gestor','operador')
def api_criar_pendencia():
    dados=request.get_json(silent=True) or {}; erro=_validar_pendencia_payload(dados)
    if erro: return jsonify({'erro':erro}),400
    item=db.criar_pendencia_acao(dados,session.get('username'))
    db.registrar_movimentacao(0,'pendencia_criada','1',session.get('username'),f"Pendência #{item.get('id')} criada para filial {item.get('filial') or '-'}: {item.get('titulo')}",tabela='sistema')
    return jsonify({'ok':True,'item':item,'dados':_dados_central_pendencias()}),201


@app.route('/api/pendencias/<int:pendencia_id>', methods=['PUT'])
@role_required('admin','gestor','operador')
def api_atualizar_pendencia(pendencia_id):
    dados=request.get_json(silent=True) or {}; erro=_validar_pendencia_payload(dados,True)
    if erro: return jsonify({'erro':erro}),400
    antes=db.buscar_pendencia_acao(pendencia_id)
    if not antes: return jsonify({'erro':'Pendência não encontrada.'}),404
    item=db.atualizar_pendencia_acao(pendencia_id,dados,session.get('username'))
    mud=[]
    for c in ('filial','titulo','responsavel','prazo','prioridade','status'):
        if c in dados and str(antes.get(c) or '')!=str(item.get(c) or ''): mud.append(f"{c}: {antes.get(c) or '-'} → {item.get(c) or '-'}")
    db.registrar_movimentacao(0,'pendencia_editada','1',session.get('username'),f"Pendência #{pendencia_id} atualizada. "+(' | '.join(mud) or 'Dados atualizados.'),tabela='sistema')
    return jsonify({'ok':True,'item':item,'dados':_dados_central_pendencias()})


@app.route('/api/pendencias/<int:pendencia_id>', methods=['DELETE'])
@role_required('admin','gestor','operador')
def api_excluir_pendencia(pendencia_id):
    item=db.buscar_pendencia_acao(pendencia_id)
    if not item: return jsonify({'erro':'Pendência não encontrada.'}),404
    db.excluir_pendencia_acao(pendencia_id)
    db.registrar_movimentacao(0,'pendencia_excluida','1',session.get('username'),f"Pendência #{pendencia_id} excluída: {item.get('titulo')}",tabela='sistema')
    return jsonify({'ok':True,'dados':_dados_central_pendencias()})


@app.route('/api/pendencias/<int:pendencia_id>/comentarios', methods=['POST'])
@role_required('admin','gestor','operador')
def api_comentar_pendencia(pendencia_id):
    if not db.buscar_pendencia_acao(pendencia_id): return jsonify({'erro':'Pendência não encontrada.'}),404
    dados=request.get_json(silent=True) or {}; comentario=str(dados.get('comentario') or '').strip()
    if not comentario: return jsonify({'erro':'Digite o comentário.'}),400
    arr=db.adicionar_comentario_pendencia(pendencia_id,comentario,session.get('username'))
    db.registrar_movimentacao(0,'pendencia_comentario','1',session.get('username'),f"Comentário adicionado na pendência #{pendencia_id}.",tabela='sistema')
    return jsonify({'ok':True,'comentarios':arr})


@app.route('/api/pendencias/<int:pendencia_id>/evidencias', methods=['POST'])
@role_required('admin','gestor','operador')
def api_evidencia_pendencia(pendencia_id):
    if not db.buscar_pendencia_acao(pendencia_id): return jsonify({'erro':'Pendência não encontrada.'}),404
    if request.content_type and 'multipart/form-data' in request.content_type:
        titulo=str(request.form.get('titulo') or '').strip(); referencia=str(request.form.get('referencia') or '').strip(); arquivo=request.files.get('arquivo')
    else:
        dados=request.get_json(silent=True) or {}; titulo=str(dados.get('titulo') or '').strip(); referencia=str(dados.get('referencia') or '').strip(); arquivo=None
    if not titulo: return jsonify({'erro':'Informe o título/descrição da evidência.'}),400
    arquivo_nome=None; mime_type=None; conteudo=None
    if arquivo and arquivo.filename:
        conteudo=arquivo.read()
        if len(conteudo)>5*1024*1024: return jsonify({'erro':'O arquivo da evidência deve ter no máximo 5 MB.'}),400
        arquivo_nome=re.sub(r'[^A-Za-z0-9._() -]+','_',arquivo.filename)[:180]
        mime_type=(arquivo.mimetype or 'application/octet-stream')[:100]
    if not referencia and not conteudo: return jsonify({'erro':'Informe uma referência ou selecione um arquivo de evidência.'}),400
    arr=db.adicionar_evidencia_pendencia(pendencia_id,titulo,referencia,session.get('username'),arquivo_nome,mime_type,conteudo)
    db.registrar_movimentacao(0,'pendencia_evidencia','1',session.get('username'),f"Evidência adicionada na pendência #{pendencia_id}: {titulo}.",tabela='sistema')
    return jsonify({'ok':True,'evidencias':arr})


@app.route('/pendencias/evidencia/<int:evidencia_id>/arquivo')
@login_required
def baixar_arquivo_evidencia(evidencia_id):
    ev=db.obter_arquivo_evidencia(evidencia_id)
    if not ev or not ev.get('conteudo'): return jsonify({'erro':'Arquivo de evidência não encontrado.'}),404
    conteudo=ev.get('conteudo')
    if isinstance(conteudo,memoryview): conteudo=conteudo.tobytes()
    return send_file(io.BytesIO(bytes(conteudo)),as_attachment=True,download_name=ev.get('arquivo_nome') or f'evidencia_{evidencia_id}',mimetype=ev.get('mime_type') or 'application/octet-stream')


def _gerar_excel_pendencias(dados):
    wb=Workbook(); ws=wb.active; ws.title='Resumo'
    azul='234C74'; escuro='172331'; branco='FFFFFF'; cinza='DCE6F0'; amarelo='FFB648'; vermelho='FF6B6B'; verde='4CD792'
    ws.append(['CENTRAL DE PENDÊNCIAS E AÇÕES']); ws['A1'].font=Font(bold=True,size=16,color=branco); ws['A1'].fill=PatternFill('solid',fgColor=azul); ws.merge_cells('A1:D1')
    ws.append(['Gerado em',dados.get('gerado_em')]);
    for k,v in [('Total',dados['resumo']['total']),('Abertas',dados['resumo']['abertas']),('Vencidas',dados['resumo']['vencidas']),('Vence hoje',dados['resumo']['vence_hoje']),('Próximas 3 dias',dados['resumo']['proximas_3d']),('Críticas',dados['resumo']['criticas']),('Sem responsável',dados['resumo']['sem_responsavel']),('Concluídas',dados['resumo']['concluidas'])]: ws.append([k,v])
    ws.column_dimensions['A'].width=25; ws.column_dimensions['B'].width=28
    p=wb.create_sheet('Pendências'); headers=['ID','Filial','Loja','Projeto','UF','Título','Descrição','Responsável','Prazo','Dias','Prioridade','Status','Origem','Criado por','Criado em','Atualizado por','Atualizado em']
    p.append(headers)
    for c in p[1]: c.font=Font(bold=True,color=branco); c.fill=PatternFill('solid',fgColor=azul); c.alignment=Alignment(horizontal='center')
    for x in dados['pendencias']:
        p.append([x.get('id'),x.get('filial'),x.get('loja'),x.get('projeto'),x.get('uf'),x.get('titulo'),x.get('descricao'),x.get('responsavel'),x.get('prazo'),x.get('dias_prazo'),x.get('prioridade'),x.get('status'),x.get('origem'),x.get('criado_por'),x.get('criado_em'),x.get('atualizado_por'),x.get('atualizado_em')])
    for i,w in enumerate([8,12,28,16,7,32,48,22,13,9,12,18,12,18,20,18,20],1): p.column_dimensions[get_column_letter(i)].width=w
    p.freeze_panes='A2'; p.auto_filter.ref=p.dimensions
    c=wb.create_sheet('Comentários'); c.append(['Pendência ID','Filial','Título','Comentário','Usuário','Data/hora'])
    e=wb.create_sheet('Evidências'); e.append(['Pendência ID','Filial','Título pendência','Evidência','Referência','Arquivo','Usuário','Data/hora'])
    for sh in (c,e):
        for cc in sh[1]: cc.font=Font(bold=True,color=branco); cc.fill=PatternFill('solid',fgColor=azul)
    for x in dados['pendencias']:
        for cm in db.listar_comentarios_pendencia(x['id']): c.append([x['id'],x.get('filial'),x.get('titulo'),cm.get('comentario'),cm.get('usuario'),cm.get('data_hora')])
        for ev in db.listar_evidencias_pendencia(x['id']): e.append([x['id'],x.get('filial'),x.get('titulo'),ev.get('titulo'),ev.get('referencia'),ev.get('arquivo_nome'),ev.get('usuario'),ev.get('data_hora')])
    for sh in (c,e):
        for col in range(1,sh.max_column+1): sh.column_dimensions[get_column_letter(col)].width=min(52,max(12,max(len(str(sh.cell(r,col).value or '')) for r in range(1,min(sh.max_row,200)+1))+2))
        sh.freeze_panes='A2'; sh.auto_filter.ref=sh.dimensions
    a=wb.create_sheet('Alertas'); a.append(['Tipo','ID','Filial','Título','Responsável','Prazo','Prioridade','Status'])
    for cc in a[1]: cc.font=Font(bold=True,color=branco); cc.fill=PatternFill('solid',fgColor=azul)
    for nome,chave in [('Vencida','vencidas'),('Vence hoje','vence_hoje'),('Próximos 3 dias','proximas_3d'),('Crítica','criticas'),('Sem responsável','sem_responsavel')]:
        for x in dados['alertas'][chave]: a.append([nome,x.get('id'),x.get('filial'),x.get('titulo'),x.get('responsavel'),x.get('prazo'),x.get('prioridade'),x.get('status')])
    for i,w in enumerate([20,8,12,36,22,14,12,18],1): a.column_dimensions[get_column_letter(i)].width=w
    n=wb.create_sheet('Notificações'); n.append(['ID','Pendência ID','Canal','Gatilho','Destinatário','Status','Detalhe','Enviado em'])
    for cc in n[1]: cc.font=Font(bold=True,color=branco); cc.fill=PatternFill('solid',fgColor=azul)
    for x in dados.get('notificacoes',[]): n.append([x.get('id'),('Resumo semanal' if int(x.get('pendencia_id') or 0)==0 else x.get('pendencia_id')),x.get('canal'),x.get('gatilho'),x.get('destinatario'),x.get('status'),x.get('detalhe'),x.get('enviado_em')])
    for i,w in enumerate([8,12,14,28,34,14,42,20],1): n.column_dimensions[get_column_letter(i)].width=w
    n.freeze_panes='A2'; n.auto_filter.ref=n.dimensions
    buf=io.BytesIO(); wb.save(buf); buf.seek(0); return buf


def _gerar_pdf_pendencias(dados):
    buf=io.BytesIO(); pdf=canvas.Canvas(buf,pagesize=landscape(A4)); larg,alt=landscape(A4); margem=14*mm
    def bg(): pdf.setFillColor(colors.HexColor('#0F151C')); pdf.rect(0,0,larg,alt,stroke=0,fill=1)
    def footer(pg): pdf.setFillColor(colors.HexColor('#70869B')); pdf.setFont('Helvetica',6.5); pdf.drawString(margem,8*mm,'Developed by ALM - Expansão de TI'); pdf.drawRightString(larg-margem,8*mm,f'Página {pg}')
    def titulo(t,sub): bg(); pdf.setFillColor(colors.white); pdf.setFont('Helvetica-Bold',19); pdf.drawString(margem,alt-margem,t); pdf.setFillColor(colors.HexColor('#9DB3C8')); pdf.setFont('Helvetica',8); pdf.drawString(margem,alt-margem-14,sub); pdf.drawRightString(larg-margem,alt-margem-14,f"Gerado em {dados.get('gerado_em')}")
    titulo('Central de Pendências e Ações','Responsáveis, prazos, prioridades, status, alertas, comentários e evidências do processo de implantação.')
    cards=[('Abertas',dados['resumo']['abertas'],'#3EA6FF'),('Vencidas',dados['resumo']['vencidas'],'#FF6B6B'),('Próx. prazo',dados['resumo']['proximas_3d']+dados['resumo']['vence_hoje'],'#A78BFA'),('Críticas',dados['resumo']['criticas'],'#FF6B6B'),('Notif. e-mail',dados.get('notificacoes_resumo',{}).get('email',0),'#4CD792'),('Notif. WhatsApp',dados.get('notificacoes_resumo',{}).get('whatsapp',0),'#4CD792')]
    y=alt-margem-67; gap=8; w=(larg-2*margem-gap*5)/6; h=48
    for i,(lab,val,cor) in enumerate(cards):
        x=margem+i*(w+gap); pdf.setFillColor(colors.HexColor('#171F29')); pdf.setStrokeColor(colors.HexColor('#2A3645')); pdf.roundRect(x,y,w,h,7,stroke=1,fill=1); pdf.setFillColor(colors.HexColor('#8EA0B3')); pdf.setFont('Helvetica-Bold',6.8); pdf.drawString(x+8,y+h-14,lab.upper()); pdf.setFillColor(colors.HexColor(cor)); pdf.setFont('Helvetica-Bold',17); pdf.drawString(x+8,y+16,str(val))
    py=22*mm; ph=y-py-15; pdf.setFillColor(colors.HexColor('#151D27')); pdf.roundRect(margem,py,larg-2*margem,ph,9,stroke=0,fill=1); pdf.setFillColor(colors.white); pdf.setFont('Helvetica-Bold',11); pdf.drawString(margem+12,py+ph-20,'Alertas prioritários')
    alertas=[]
    for nome,ch in [('VENCIDA','vencidas'),('VENCE HOJE','vence_hoje'),('CRÍTICA','criticas'),('PRÓX. 3 DIAS','proximas_3d'),('SEM RESPONSÁVEL','sem_responsavel')]:
        for x in dados['alertas'][ch]: alertas.append((nome,x))
    cy=py+ph-42
    for nome,x in alertas[:12]:
        cor='#FF6B6B' if nome in ('VENCIDA','CRÍTICA') else '#FFB648' if nome=='VENCE HOJE' else '#A78BFA'
        pdf.setFillColor(colors.HexColor('#1B2531')); pdf.roundRect(margem+12,cy-12,larg-2*margem-24,21,4,stroke=0,fill=1); pdf.setFillColor(colors.HexColor(cor)); pdf.setFont('Helvetica-Bold',7); pdf.drawString(margem+20,cy-1,nome); pdf.setFillColor(colors.HexColor('#DCE6F0')); pdf.setFont('Helvetica',7); txt=f"#{x.get('id')} · Filial {x.get('filial') or '-'} · {x.get('titulo')} · Resp.: {x.get('responsavel') or 'Não definido'} · Prazo: {x.get('prazo') or '-'}"; pdf.drawString(margem+85,cy-1,txt[:145]); cy-=25
    footer(1); pdf.showPage()
    cols=[('ID',26),('Filial',42),('Loja',110),('Título',150),('Responsável',82),('Prazo',55),('Dias',35),('Prior.',48),('Status',72),('Comentários',50),('Evid.',35)]; tw=sum(x[1] for x in cols); rh=20; pg=2
    def head():
        titulo('Detalhamento das Pendências','Situação completa das ações cadastradas, com integração por filial ao Cockpit de Implantação.'); yy=alt-margem-42; pdf.setFillColor(colors.HexColor('#234C74')); pdf.roundRect(margem,yy,tw,20,4,stroke=0,fill=1); pdf.setFillColor(colors.white); pdf.setFont('Helvetica-Bold',6.5); cx=margem
        for h,wc in cols: pdf.drawString(cx+3,yy+6,h); cx+=wc
        return yy-3
    yy=head()
    for x in dados['pendencias']:
        if yy-rh<18*mm: footer(pg); pdf.showPage(); pg+=1; yy=head()
        yy-=rh; pdf.setFillColor(colors.HexColor('#182230')); pdf.roundRect(margem,yy,tw,rh-1,3,stroke=0,fill=1); vals=[x.get('id'),x.get('filial'),x.get('loja'),x.get('titulo'),x.get('responsavel'),x.get('prazo'),x.get('dias_prazo'),x.get('prioridade'),x.get('status'),len(db.listar_comentarios_pendencia(x['id'])),len(db.listar_evidencias_pendencia(x['id']))]; cx=margem
        for (h,wc),v in zip(cols,vals):
            txt=str(v if v not in (None,'') else '-'); maxc=max(4,int((wc-6)/4.2)); txt=txt if len(txt)<=maxc else txt[:maxc-1]+'…'; pdf.setFillColor(colors.HexColor('#FF6B6B' if h=='Dias' and isinstance(v,int) and v<0 else '#DCE6F0')); pdf.setFont('Helvetica-Bold' if h in ('Prior.','Status') else 'Helvetica',6.2); pdf.drawString(cx+3,yy+7,txt); cx+=wc
        yy-=3
    footer(pg)
    # Páginas finais: comentários e evidências completos para auditoria.
    registros_detalhe=[]
    for x in dados['pendencias']:
        comentarios=db.listar_comentarios_pendencia(x['id']); evidencias=db.listar_evidencias_pendencia(x['id'])
        if comentarios or evidencias: registros_detalhe.append((x,comentarios,evidencias))
    if registros_detalhe:
        pdf.showPage(); pg+=1; titulo('Comentários e Evidências','Histórico textual e referências anexadas a cada pendência/ação.')
        yy=alt-margem-43
        def linhas_texto(txt,maxc=118):
            txt=str(txt or '').replace('\n',' ').strip(); palavras=txt.split(); linhas=[]; atual=''
            for pal in palavras:
                teste=(atual+' '+pal).strip()
                if len(teste)>maxc and atual: linhas.append(atual); atual=pal
                else: atual=teste
            if atual: linhas.append(atual)
            return linhas or ['-']
        for x,comentarios,evidencias in registros_detalhe:
            necessidade=31 + 13*min(4,len(comentarios)+len(evidencias))
            if yy-necesidade<18*mm:
                footer(pg); pdf.showPage(); pg+=1; titulo('Comentários e Evidências','Continuação do histórico de auditoria das ações.'); yy=alt-margem-43
            pdf.setFillColor(colors.HexColor('#1B2531')); pdf.roundRect(margem,yy-22,larg-2*margem,24,5,stroke=0,fill=1); pdf.setFillColor(colors.white); pdf.setFont('Helvetica-Bold',8); pdf.drawString(margem+8,yy-8,f"#{x.get('id')} · Filial {x.get('filial') or '-'} · {str(x.get('titulo') or '')[:90]}"); yy-=29
            for label,arr in [('Comentário',comentarios),('Evidência',evidencias)]:
                for item in arr:
                    texto=item.get('comentario') if label=='Comentário' else f"{item.get('titulo') or ''} | Ref.: {item.get('referencia') or '-'} | Arquivo: {item.get('arquivo_nome') or '-'}"
                    linhas=linhas_texto(texto)[:3]
                    altura=12+8*len(linhas)
                    if yy-altura<18*mm:
                        footer(pg); pdf.showPage(); pg+=1; titulo('Comentários e Evidências','Continuação do histórico de auditoria das ações.'); yy=alt-margem-43
                    pdf.setFillColor(colors.HexColor('#151D27')); pdf.roundRect(margem+8,yy-altura+2,larg-2*margem-16,altura,4,stroke=0,fill=1); pdf.setFillColor(colors.HexColor('#3EA6FF' if label=='Comentário' else '#A78BFA')); pdf.setFont('Helvetica-Bold',6.7); pdf.drawString(margem+14,yy-7,label.upper()); pdf.setFillColor(colors.HexColor('#DCE6F0')); pdf.setFont('Helvetica',6.4)
                    ly=yy-16
                    for ln in linhas: pdf.drawString(margem+14,ly,ln[:120]); ly-=8
                    pdf.setFillColor(colors.HexColor('#7E93A8')); pdf.setFont('Helvetica',5.8); pdf.drawRightString(larg-margem-14,yy-7,f"{item.get('usuario') or '-'} · {item.get('data_hora') or ''}"); yy-=altura+4
            yy-=5
        footer(pg)
    # Histórico de notificações automáticas para auditoria.
    notificacoes=dados.get('notificacoes') or []
    if notificacoes:
        pdf.showPage(); pg+=1; titulo('Notificações Automáticas','Histórico recente de alertas enviados por e-mail e WhatsApp para pendências da implantação.')
        yy=alt-margem-43
        cab=['Pend.','Canal','Gatilho','Destinatário','Status','Enviado em']; widths=[36,55,120,190,60,90]; totalw=sum(widths)
        pdf.setFillColor(colors.HexColor('#234C74')); pdf.roundRect(margem,yy,totalw,20,4,stroke=0,fill=1); pdf.setFillColor(colors.white); pdf.setFont('Helvetica-Bold',6.5); cx=margem
        for h,wc in zip(cab,widths): pdf.drawString(cx+3,yy+6,h); cx+=wc
        yy-=3
        for n in notificacoes[:80]:
            if yy-20<18*mm:
                footer(pg); pdf.showPage(); pg+=1; titulo('Notificações Automáticas','Continuação do histórico de alertas enviados.'); yy=alt-margem-43
                pdf.setFillColor(colors.HexColor('#234C74')); pdf.roundRect(margem,yy,totalw,20,4,stroke=0,fill=1); pdf.setFillColor(colors.white); pdf.setFont('Helvetica-Bold',6.5); cx=margem
                for h,wc in zip(cab,widths): pdf.drawString(cx+3,yy+6,h); cx+=wc
                yy-=3
            yy-=20; pdf.setFillColor(colors.HexColor('#182230')); pdf.roundRect(margem,yy,totalw,19,3,stroke=0,fill=1); vals=[('Resumo' if int(n.get('pendencia_id') or 0)==0 else n.get('pendencia_id')),n.get('canal'),n.get('gatilho'),n.get('destinatario'),n.get('status'),n.get('enviado_em')]; cx=margem
            for wc,v in zip(widths,vals):
                txt=str(v or '-'); maxc=max(5,int((wc-6)/4.1)); txt=txt if len(txt)<=maxc else txt[:maxc-1]+'…'; pdf.setFillColor(colors.HexColor('#DCE6F0')); pdf.setFont('Helvetica',6.1); pdf.drawString(cx+3,yy+6.5,txt); cx+=wc
            yy-=3
        footer(pg)
    pdf.save(); buf.seek(0); return buf


@app.route('/api/notificacoes/pendencias/processar', methods=['POST'])
@admin_required
def api_processar_notificacoes_pendencias():
    return jsonify({'erro':'Notificações por e-mail e WhatsApp foram desativadas.'}), 410


@app.route('/tasks/notificar-pendencias', methods=['GET','POST'])
def task_notificar_pendencias():
    return jsonify({'erro':'Notificações por e-mail e WhatsApp foram desativadas.'}), 410


@app.route('/export-pendencias')
@role_required('admin','gestor','operador')
def exportar_pendencias():
    dados=_dados_central_pendencias(); return send_file(_gerar_excel_pendencias(dados),as_attachment=True,download_name=f"central_pendencias_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx",mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/pdf-pendencias')
@role_required('admin','gestor','operador')
def relatorio_pdf_pendencias():
    dados=_dados_central_pendencias(); return send_file(_gerar_pdf_pendencias(dados),as_attachment=True,download_name=f"central_pendencias_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf",mimetype='application/pdf')


@app.route("/export-cockpit-implantacao")
@role_required("admin", "gestor", "operador")
def exportar_cockpit_implantacao():
    dados = _dados_cockpit_implantacao(incluir_financeiro=True)
    return send_file(_gerar_excel_cockpit_implantacao(dados), as_attachment=True, download_name=f"cockpit_implantacao_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/pdf-cockpit-implantacao")
@role_required("admin", "gestor", "operador")
def relatorio_pdf_cockpit_implantacao():
    dados = _dados_cockpit_implantacao(incluir_financeiro=True)
    return send_file(_gerar_pdf_cockpit_implantacao(dados), as_attachment=True, download_name=f"cockpit_implantacao_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf", mimetype="application/pdf")


@app.route("/api/orcamento", methods=["GET"])
@role_required("admin", "gestor", "operador")
def api_orcamento():
    return jsonify(_calcular_orcamento_pepi())


@app.route("/api/orcamento/pepi", methods=["PUT"])
@manager_required
def api_salvar_orcamento_pepi():
    dados = request.get_json(force=True) or {}
    try:
        valor = _decimal_moeda(dados.get("valor"), "0.00")
    except ValueError:
        return jsonify({"erro": "Informe um valor consolidado válido para a PEPI."}), 400
    if valor < 0:
        return jsonify({"erro": "O valor consolidado da PEPI não pode ser negativo."}), 400
    anterior = _decimal_moeda(db.obter_orcamento_pepi_consolidado(), "0.00")
    db.salvar_orcamento_pepi_consolidado(format(valor, ".2f"), session.get("username"))
    if anterior != valor:
        db.registrar_movimentacao(
            0,
            "orcamento_pepi",
            format(valor, ".2f"),
            session.get("username"),
            f"Valor consolidado da PEPI alterado de R$ {anterior:.2f} para R$ {valor:.2f}.",
            tabela="sistema",
        )
    return jsonify({"ok": True, "valor": format(valor, ".2f"), "orcamento": _calcular_orcamento_pepi()})


def _status_orcamento_texto(dados):
    if int(dados.get("itens_sem_custo") or 0) > 0:
        return "INCOMPLETO - EXISTEM ITENS SEM CUSTO"
    if _decimal_moeda(dados.get("saldo"), "0.00") < 0:
        return "PEPI INSUFICIENTE PARA O PEDIDO SUGERIDO"
    if int(dados.get("total_unidades_pedido") or 0) <= 0:
        return "ESTOQUE SUFICIENTE - SEM COMPRA SUGERIDA"
    return "PEDIDO SUGERIDO COBERTO PELA PEPI"


def _preparar_planilha_orcamento(ws, headers, linhas, larguras=None):
    ws.append(headers)
    fill = PatternFill("solid", fgColor="1F4E78")
    side = Side(style="thin", color="D9E2F3")
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = Border(bottom=side)
    for linha in linhas:
        ws.append(linha)
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{max(1, ws.max_row)}"
    if larguras:
        for idx, largura in enumerate(larguras, 1):
            ws.column_dimensions[get_column_letter(idx)].width = largura
    for row in ws.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(vertical="top", wrap_text=True)
    return ws


def _gerar_excel_orcamento(dados):
    wb = Workbook()
    ws = wb.active
    ws.title = "Resumo"
    status = _status_orcamento_texto(dados)
    origem = "Lojas PENDENTES no Acompanhamento de Expansão" if dados.get("base_origem") == "acompanhamento_expansao" else "Meta de Expansão (provisória)"
    resumo = [
        ("Indicador", "Valor"),
        ("Status", status),
        ("PEPI consolidada", float(_decimal_moeda(dados.get("pepi_consolidado"), "0.00"))),
        ("Valor projetado da compra", float(_decimal_moeda(dados.get("total_previsto"), "0.00"))),
        ("Saldo estimado da PEPI", float(_decimal_moeda(dados.get("saldo"), "0.00"))),
        ("PEPI comprometida", float(_decimal_moeda(dados.get("percentual_comprometido"), "0.00")) / 100),
        ("Lojas consideradas", int(dados.get("lojas_base") or 0)),
        ("Origem da projeção", origem),
        ("SKUs para comprar", int(dados.get("itens_para_comprar") or 0)),
        ("Unidades sugeridas", int(dados.get("total_unidades_pedido") or 0)),
        ("Itens sem custo", int(dados.get("itens_sem_custo") or 0)),
        ("Itens sem Cadastro de Produtos", int(dados.get("itens_sem_cadastro") or 0)),
        ("Fonte mestre dos itens", "Cadastro de Produtos"),
        ("Gerado em", datetime.now().strftime("%d/%m/%Y %H:%M")),
        ("Gerado por", session.get("username") or "Administrador"),
    ]
    for row in resumo:
        ws.append(row)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1F4E78")
    ws.column_dimensions["A"].width = 31
    ws.column_dimensions["B"].width = 42
    for row in (3, 4, 5):
        ws[f"B{row}"].number_format = 'R$ #,##0.00'
    ws["B6"].number_format = '0.00%'
    ws["B2"].font = Font(bold=True, color="C65911" if int(dados.get("itens_sem_custo") or 0) else ("C00000" if _decimal_moeda(dados.get("saldo"), "0.00") < 0 else "008000"))

    headers = ["Código", "Produto (Cadastro)", "Qtd./loja", "Lojas", "Necessário", "Estoque Expansão", "Comprar", "Custo unitário", "Valor projetado", "Custo informado"]
    linhas_pedido = []
    for x in dados.get("pedido_linhas") or []:
        custo_ok = bool(x.get("custo_informado"))
        linhas_pedido.append([
            x.get("codigo") or "-", x.get("descricao") or "", int(x.get("qtd_por_loja") or 0), int(x.get("lojas_base") or dados.get("lojas_base") or 0),
            int(x.get("necessario") or 0), int(x.get("estoque_expansao") or 0), int(x.get("comprar") or 0),
            float(_decimal_moeda(x.get("custo"), "0.00")) if custo_ok else None,
            float(_decimal_moeda(x.get("subtotal"), "0.00")) if custo_ok else None,
            "SIM" if custo_ok else "NÃO",
        ])
    pedido = wb.create_sheet("Pedido sugerido")
    _preparar_planilha_orcamento(pedido, headers, linhas_pedido, [15, 40, 11, 9, 12, 18, 11, 17, 18, 15])
    for row in range(2, pedido.max_row + 1):
        pedido[f"H{row}"].number_format = 'R$ #,##0.00'
        pedido[f"I{row}"].number_format = 'R$ #,##0.00'

    linhas_det = []
    for x in dados.get("linhas") or []:
        custo_ok = bool(x.get("custo_informado"))
        linhas_det.append([
            x.get("codigo") or "-", x.get("descricao") or "", x.get("kit_descricao") or "", int(x.get("qtd_por_loja") or 0),
            int(x.get("necessario") or 0), int(x.get("estoque_expansao") or 0), int(x.get("comprar") or 0),
            float(_decimal_moeda(x.get("custo"), "0.00")) if custo_ok else None,
            float(_decimal_moeda(x.get("subtotal"), "0.00")) if custo_ok else None,
            "SIM" if custo_ok else "NÃO",
        ])
    det = wb.create_sheet("Detalhamento")
    _preparar_planilha_orcamento(det, ["Código", "Produto (Cadastro)", "Referência do Kit", "Qtd./loja", "Necessário", "Estoque Expansão", "Comprar", "Custo unitário", "Valor projetado", "Custo informado"], linhas_det, [15, 36, 36, 11, 12, 18, 11, 17, 18, 15])
    for row in range(2, det.max_row + 1):
        det[f"H{row}"].number_format = 'R$ #,##0.00'
        det[f"I{row}"].number_format = 'R$ #,##0.00'

    lojas = wb.create_sheet("Lojas consideradas")
    linhas_lojas = [[x.get("codigo") or "", x.get("nome") or "", x.get("uf") or "", x.get("previsao_abertura") or ""] for x in dados.get("lojas_consideradas") or []]
    _preparar_planilha_orcamento(lojas, ["Filial", "Nome", "UF", "Previsão de abertura"], linhas_lojas, [16, 42, 8, 22])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def _gerar_pdf_orcamento(dados):
    """Gera relatório executivo de Orçamento no mesmo padrão visual dos demais PDFs."""
    buf = io.BytesIO()
    page_size = landscape(A4)
    pdf = canvas.Canvas(buf, pagesize=page_size)
    larg, alt = page_size
    margem = 14 * mm

    def brl(v):
        val = float(_decimal_moeda(v, "0.00"))
        s = f"{val:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
        return f"R$ {s}"

    def _bg():
        pdf.setFillColor(colors.HexColor("#0F1620"))
        pdf.rect(0, 0, larg, alt, stroke=0, fill=1)

    def _footer(page_no, secao="Orçamento e Pedido de Compra"):
        pdf.setStrokeColor(colors.HexColor("#283646"))
        pdf.line(margem, 11 * mm, larg - margem, 11 * mm)
        pdf.setFillColor(colors.HexColor("#8398AD"))
        pdf.setFont("Helvetica", 7.2)
        pdf.drawString(margem, 6.5 * mm, f"© 2026 · Developed by ALM - Expansão de TI · {secao}")
        pdf.drawRightString(larg - margem, 6.5 * mm, f"Página {page_no}")

    def _panel(x, y, w, h, title=None, subtitle=None):
        pdf.setFillColor(colors.HexColor("#151D27"))
        pdf.setStrokeColor(colors.HexColor("#2A3645"))
        pdf.roundRect(x, y, w, h, 10, stroke=1, fill=1)
        if title:
            pdf.setFillColor(colors.white)
            pdf.setFont("Helvetica-Bold", 11)
            pdf.drawString(x + 12, y + h - 20, title)
        if subtitle:
            pdf.setFillColor(colors.HexColor("#8FA5BA"))
            pdf.setFont("Helvetica", 7.5)
            pdf.drawString(x + 12, y + h - 32, subtitle[:108])

    def _kpi(x, y, w, h, titulo, valor, detalhe, cor="#FFFFFF"):
        pdf.setFillColor(colors.HexColor("#182230"))
        pdf.setStrokeColor(colors.HexColor("#314255"))
        pdf.roundRect(x, y, w, h, 10, stroke=1, fill=1)
        pdf.setFillColor(colors.HexColor("#94A9BC"))
        pdf.setFont("Helvetica-Bold", 6.9)
        pdf.drawString(x + 9, y + h - 13, str(titulo).upper()[:24])
        pdf.setFillColor(colors.HexColor(cor))
        pdf.setFont("Helvetica-Bold", 15.2 if len(str(valor)) <= 14 else 12.3)
        pdf.drawString(x + 9, y + 18, str(valor))
        pdf.setFillColor(colors.HexColor("#7990A6"))
        pdf.setFont("Helvetica", 6.5)
        pdf.drawString(x + 9, y + 7, str(detalhe)[:34])

    def _fit(texto, width, font="Helvetica", size=6.7):
        texto = str(texto or "-")
        if pdf.stringWidth(texto, font, size) <= width:
            return texto
        while len(texto) > 1 and pdf.stringWidth(texto + "…", font, size) > width:
            texto = texto[:-1]
        return texto + "…"

    pepi = float(_decimal_moeda(dados.get("pepi_consolidado"), "0.00"))
    total_previsto = float(_decimal_moeda(dados.get("total_previsto"), "0.00"))
    saldo = float(_decimal_moeda(dados.get("saldo"), "0.00"))
    comprometido = float(_decimal_moeda(dados.get("percentual_comprometido"), "0.00"))
    lojas_base = int(dados.get("lojas_base") or 0)
    unidades = int(dados.get("total_unidades_pedido") or 0)
    skus = int(dados.get("itens_para_comprar") or 0)
    sem_custo = int(dados.get("itens_sem_custo") or 0)
    pedido = dados.get("pedido_linhas") or []
    linhas = dados.get("linhas") or []
    lojas = dados.get("lojas_consideradas") or []
    base_projecao = dados.get("base_origem") == "acompanhamento_expansao"
    origem = "Lojas PENDENTES no Acompanhamento de Expansão" if base_projecao else "Meta de Expansão provisória"
    status = _status_orcamento_texto(dados)

    if sem_custo > 0:
        status_cor = "#FFB648"
        status_detalhe = f"{sem_custo} item(ns) ainda precisam de custo cadastrado."
    elif saldo < 0:
        status_cor = "#FF6B6B"
        status_detalhe = f"Déficit estimado de {brl(abs(saldo))} para cobrir o pedido sugerido."
    elif unidades <= 0:
        status_cor = "#4CD792"
        status_detalhe = "O estoque de Expansão cobre a necessidade projetada."
    else:
        status_cor = "#4CD792"
        status_detalhe = f"Saldo estimado de {brl(saldo)} após o pedido sugerido."

    # Página 1 - resumo executivo
    _bg()
    pdf.setTitle("Orçamento - PEPI e Pedido de Compra")
    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 20)
    pdf.drawString(margem, alt - margem, "Orçamento - PEPI e Pedido de Compra")
    pdf.setFillColor(colors.HexColor("#9DB3C8"))
    pdf.setFont("Helvetica", 8.5)
    pdf.drawString(margem, alt - margem - 14, "Relatório executivo do orçamento, cobertura de estoque e sugestão de compra para as lojas a inaugurar.")
    pdf.drawRightString(larg - margem, alt - margem - 14, datetime.now().strftime("Gerado em %d/%m/%Y às %H:%M"))

    gap = 8
    kpi_y = alt - margem - 66
    kpi_h = 47
    kpi_w = (larg - 2*margem - gap*5) / 6
    kpis = [
        ("PEPI disponível", brl(pepi), "Valor consolidado", "#3EA6FF"),
        ("Compra projetada", brl(total_previsto), f"{skus} SKU(s) para comprar", "#FFB648"),
        ("Saldo estimado", brl(saldo), "Após pedido sugerido", "#4CD792" if saldo >= 0 else "#FF6B6B"),
        ("PEPI comprometida", f"{comprometido:.1f}%", "Percentual do orçamento", "#A78BFA"),
        ("Lojas consideradas", lojas_base, origem, "#56CFE1"),
        ("Unidades sugeridas", unidades, "Quantidade total a comprar", "#FFFFFF"),
    ]
    for i, item in enumerate(kpis):
        _kpi(margem + i*(kpi_w+gap), kpi_y, kpi_w, kpi_h, *item)

    content_top = kpi_y - 14
    content_y = 22 * mm
    content_h = content_top - content_y
    left_w = (larg - 2*margem - 10) * 0.60
    right_x = margem + left_w + 10
    right_w = larg - margem - right_x

    _panel(margem, content_y, left_w, content_h, "Situação do orçamento", "Leitura consolidada da PEPI frente à necessidade calculada para a expansão.")
    pdf.setFillColor(colors.HexColor(status_cor))
    pdf.setFont("Helvetica-Bold", 13.5)
    pdf.drawString(margem + 16, content_y + content_h - 62, status[:58])
    pdf.setFillColor(colors.HexColor("#C6D5E3"))
    pdf.setFont("Helvetica", 7.8)
    pdf.drawString(margem + 16, content_y + content_h - 79, status_detalhe[:100])

    prog_x = margem + 16
    prog_y = content_y + content_h - 112
    prog_w = left_w - 32
    pdf.setFillColor(colors.HexColor("#D8E4EF"))
    pdf.setFont("Helvetica-Bold", 8)
    pdf.drawString(prog_x, prog_y + 15, "Comprometimento da PEPI")
    pdf.setFillColor(colors.HexColor("#95A8BA"))
    pdf.setFont("Helvetica", 7.2)
    pdf.drawRightString(prog_x + prog_w, prog_y + 15, f"{comprometido:.2f}%")
    pdf.setFillColor(colors.HexColor("#0B141E"))
    pdf.roundRect(prog_x, prog_y, prog_w, 8, 4, stroke=0, fill=1)
    barra_pct = max(0.0, min(100.0, comprometido)) / 100.0
    if barra_pct > 0:
        pdf.setFillColor(colors.HexColor("#4CD792" if comprometido <= 80 else ("#FFB648" if comprometido <= 100 else "#FF6B6B")))
        pdf.roundRect(prog_x, prog_y, max(6, prog_w * barra_pct), 8, 4, stroke=0, fill=1)

    resumo_y = prog_y - 33
    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 9.2)
    pdf.drawString(margem + 16, resumo_y, "Composição do cálculo")
    composicao = [
        f"• Base considerada: {lojas_base} loja(s) - {origem}.",
        f"• {skus} SKU(s) precisam de compra, totalizando {unidades} unidade(s).",
        f"• Valor projetado do pedido: {brl(total_previsto)}.",
        f"• Itens sem custo cadastrado: {sem_custo}.",
    ]
    pdf.setFont("Helvetica", 7.4)
    pdf.setFillColor(colors.HexColor("#B7C7D6"))
    for idx, linha in enumerate(composicao):
        pdf.drawString(margem + 16, resumo_y - 16 - idx*14, linha[:105])

    top_pedido = sorted(
        pedido,
        key=lambda x: (float(_decimal_moeda(x.get("subtotal"), "0.00")), int(x.get("comprar") or 0)),
        reverse=True,
    )[:5]
    top_y = content_y + 20
    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 9.2)
    pdf.drawString(margem + 16, top_y + 68, "Principais itens do pedido")
    if not top_pedido:
        pdf.setFillColor(colors.HexColor("#4CD792"))
        pdf.setFont("Helvetica-Bold", 8.2)
        pdf.drawString(margem + 16, top_y + 47, "Nenhuma compra sugerida: estoque suficiente para a projeção atual.")
    else:
        ry = top_y + 49
        for x in top_pedido:
            custo_ok = bool(x.get("custo_informado"))
            pdf.setFillColor(colors.HexColor("#1B2531"))
            pdf.roundRect(margem + 16, ry - 8, left_w - 32, 15, 5, stroke=0, fill=1)
            pdf.setFillColor(colors.HexColor("#D9E5F0"))
            pdf.setFont("Helvetica", 7.1)
            nome = _fit(x.get("descricao") or "-", left_w - 170, "Helvetica", 7.1)
            pdf.drawString(margem + 23, ry - 1, nome)
            pdf.setFillColor(colors.HexColor("#3EA6FF"))
            pdf.setFont("Helvetica-Bold", 7.1)
            pdf.drawRightString(margem + left_w - 112, ry - 1, f"Comprar {int(x.get('comprar') or 0)}")
            pdf.setFillColor(colors.HexColor("#FFB648" if not custo_ok else "#4CD792"))
            pdf.drawRightString(margem + left_w - 23, ry - 1, brl(x.get("subtotal")) if custo_ok else "SEM CUSTO")
            ry -= 18

    _panel(right_x, content_y, right_w, content_h, "Base da projeção", "Lojas e UFs utilizadas para dimensionar o pedido sugerido.")
    pdf.setFillColor(colors.HexColor("#3EA6FF"))
    pdf.setFont("Helvetica-Bold", 27)
    pdf.drawString(right_x + 14, content_y + content_h - 72, str(lojas_base))
    pdf.setFillColor(colors.HexColor("#C9D7E4"))
    pdf.setFont("Helvetica-Bold", 8.5)
    pdf.drawString(right_x + 14, content_y + content_h - 86, "loja(s) consideradas")
    pdf.setFillColor(colors.HexColor("#8FA4B8"))
    pdf.setFont("Helvetica", 7.2)
    pdf.drawString(right_x + 14, content_y + content_h - 100, origem[:45])

    cy = content_y + content_h - 132
    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(right_x + 14, cy, "Distribuição por UF")
    cy -= 18
    por_uf = dados.get("lojas_por_uf") or []
    if por_uf:
        total_uf = max(1, sum(int(x.get("quantidade") or 0) for x in por_uf))
        for item in por_uf[:7]:
            uf = str(item.get("uf") or "--")
            qtd = int(item.get("quantidade") or 0)
            pct = qtd / total_uf * 100.0
            pdf.setFillColor(colors.HexColor("#1B2531"))
            pdf.roundRect(right_x + 14, cy - 8, right_w - 28, 16, 5, stroke=0, fill=1)
            pdf.setFillColor(colors.HexColor("#D6E2ED"))
            pdf.setFont("Helvetica-Bold", 7.4)
            pdf.drawString(right_x + 21, cy - 1, uf)
            pdf.setFillColor(colors.HexColor("#56CFE1"))
            pdf.drawRightString(right_x + right_w - 21, cy - 1, f"{qtd} loja(s) · {pct:.1f}%")
            cy -= 19
    else:
        pdf.setFillColor(colors.HexColor("#8FA4B8"))
        pdf.setFont("Helvetica", 7.2)
        pdf.drawString(right_x + 14, cy, "Sem filiais marcadas como Inaugurar; usando a meta provisória.")

    cy -= 8
    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 9)
    pdf.drawString(right_x + 14, cy, "Regra aplicada")
    pdf.setFillColor(colors.HexColor("#B9C8D6"))
    pdf.setFont("Helvetica", 7.1)
    regra = [
        "Cadastro de Produtos define item e custo",
        "Kit Padrão define a quantidade por loja",
        "e o Estoque reduz a compra sugerida.",
    ]
    for idx, linha in enumerate(regra):
        pdf.drawString(right_x + 14, cy - 15 - idx*13, linha)

    _footer(1)
    pdf.showPage()

    # Página(s) 2+ - pedido sugerido detalhado
    page_no = 2
    cols = [
        ("Código", 57), ("Item", 170), ("Qtd/loja", 50), ("Lojas", 38),
        ("Necessário", 58), ("Estoque", 52), ("Comprar", 50), ("Custo unit.", 72), ("Valor", 78),
    ]
    table_w = sum(w for _, w in cols)
    row_h = 21

    def _header_pedido(titulo="Sugestão de Pedido de Compra", subt="Dimensionamento automático pela projeção de lojas e estoque de Expansão."):
        _bg()
        pdf.setFillColor(colors.white)
        pdf.setFont("Helvetica-Bold", 16)
        pdf.drawString(margem, alt - margem, titulo)
        pdf.setFillColor(colors.HexColor("#9DB3C8"))
        pdf.setFont("Helvetica", 8)
        pdf.drawString(margem, alt - margem - 13, subt)
        pdf.drawRightString(larg - margem, alt - margem - 13, f"Base: {lojas_base} loja(s) · {unidades} unidade(s) sugeridas")
        y0 = alt - margem - 40
        pdf.setFillColor(colors.HexColor("#234C74"))
        pdf.roundRect(margem, y0, table_w, 20, 4, stroke=0, fill=1)
        pdf.setFillColor(colors.white)
        pdf.setFont("Helvetica-Bold", 6.8)
        cx = margem
        for title, w in cols:
            pdf.drawString(cx + 4, y0 + 6, title)
            cx += w
        return y0 - 4

    y = _header_pedido()
    if pedido:
        for idx, item in enumerate(pedido):
            if y - row_h < 20 * mm:
                _footer(page_no)
                pdf.showPage()
                page_no += 1
                y = _header_pedido()
            y -= row_h
            pdf.setFillColor(colors.HexColor("#182230" if idx % 2 == 0 else "#151D27"))
            pdf.roundRect(margem, y, table_w, row_h - 1, 3, stroke=0, fill=1)
            custo_ok = bool(item.get("custo_informado"))
            vals = [
                str(item.get("codigo") or "-"), str(item.get("descricao") or "-"), str(item.get("qtd_por_loja") or 0),
                str(item.get("lojas_base") or lojas_base), str(item.get("necessario") or 0), str(item.get("estoque_expansao") or 0),
                str(item.get("comprar") or 0), brl(item.get("custo")) if custo_ok else "SEM CUSTO",
                brl(item.get("subtotal")) if custo_ok else "-",
            ]
            cx = margem
            for col_idx, ((title, w), val) in enumerate(zip(cols, vals)):
                if title == "Comprar":
                    pdf.setFillColor(colors.HexColor("#3EA6FF"))
                    pdf.setFont("Helvetica-Bold", 6.9)
                elif title in ("Custo unit.", "Valor") and not custo_ok:
                    pdf.setFillColor(colors.HexColor("#FFB648"))
                    pdf.setFont("Helvetica-Bold", 6.5)
                elif title == "Valor":
                    pdf.setFillColor(colors.HexColor("#4CD792"))
                    pdf.setFont("Helvetica-Bold", 6.7)
                else:
                    pdf.setFillColor(colors.HexColor("#DCE6F0"))
                    pdf.setFont("Helvetica", 6.7)
                shown = _fit(val, w - 8, "Helvetica-Bold" if title in ("Comprar", "Valor") else "Helvetica", 6.7)
                if col_idx >= 2:
                    pdf.drawRightString(cx + w - 4, y + 7, shown)
                else:
                    pdf.drawString(cx + 4, y + 7, shown)
                cx += w
    else:
        y -= 34
        pdf.setFillColor(colors.HexColor("#4CD792"))
        pdf.setFont("Helvetica-Bold", 10)
        pdf.drawString(margem + 8, y + 8, "O estoque atual cobre toda a necessidade da projeção. Nenhuma compra sugerida.")

    if y - 45 < 20 * mm:
        _footer(page_no)
        pdf.showPage()
        page_no += 1
        _bg()
        y = alt - margem - 50
    pdf.setFillColor(colors.HexColor("#151D27"))
    pdf.setStrokeColor(colors.HexColor("#2A3645"))
    pdf.roundRect(margem, y - 37, table_w, 32, 7, stroke=1, fill=1)
    pdf.setFillColor(colors.HexColor("#9BB0C4"))
    pdf.setFont("Helvetica-Bold", 7.3)
    pdf.drawString(margem + 10, y - 18, "TOTAL DO PEDIDO SUGERIDO")
    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 10.5)
    pdf.drawRightString(margem + table_w - 10, y - 18, f"{unidades} unidade(s) · {brl(total_previsto)}")
    _footer(page_no)
    pdf.showPage()
    page_no += 1

    # Conciliação completa do kit x estoque x projeção
    cols2 = [
        ("Código", 60), ("Produto (Cadastro)", 198), ("Qtd/loja", 55), ("Necessário", 65),
        ("Estoque", 58), ("Comprar", 55), ("Situação", 100), ("Valor projetado", 105),
    ]
    table_w2 = sum(w for _, w in cols2)

    def _header_conciliacao():
        _bg()
        pdf.setFillColor(colors.white)
        pdf.setFont("Helvetica-Bold", 16)
        pdf.drawString(margem, alt - margem, "Conciliação - Cadastro de Produtos x Estoque x Projeção")
        pdf.setFillColor(colors.HexColor("#9DB3C8"))
        pdf.setFont("Helvetica", 8)
        pdf.drawString(margem, alt - margem - 13, "Produtos do cadastro mestre cruzados com Kit Padrão, estoque de Expansão e projeção de lojas.")
        pdf.drawRightString(larg - margem, alt - margem - 13, f"{len(linhas)} item(ns) analisado(s)")
        y0 = alt - margem - 40
        pdf.setFillColor(colors.HexColor("#234C74"))
        pdf.roundRect(margem, y0, table_w2, 20, 4, stroke=0, fill=1)
        pdf.setFillColor(colors.white)
        pdf.setFont("Helvetica-Bold", 6.8)
        cx = margem
        for title, w in cols2:
            pdf.drawString(cx + 4, y0 + 6, title)
            cx += w
        return y0 - 4

    y = _header_conciliacao()
    for idx, item in enumerate(linhas):
        if y - row_h < 20 * mm:
            _footer(page_no, "Conciliação de Orçamento")
            pdf.showPage()
            page_no += 1
            y = _header_conciliacao()
        y -= row_h
        pdf.setFillColor(colors.HexColor("#182230" if idx % 2 == 0 else "#151D27"))
        pdf.roundRect(margem, y, table_w2, row_h - 1, 3, stroke=0, fill=1)
        comprar = int(item.get("comprar") or 0)
        custo_ok = bool(item.get("custo_informado"))
        if comprar <= 0:
            situacao = "Coberto pelo estoque"
            sit_cor = "#4CD792"
        elif not custo_ok:
            situacao = "Comprar - sem custo"
            sit_cor = "#FFB648"
        else:
            situacao = "Comprar"
            sit_cor = "#3EA6FF"
        vals = [
            str(item.get("codigo") or "-"), str(item.get("descricao") or "-"), str(item.get("qtd_por_loja") or 0),
            str(item.get("necessario") or 0), str(item.get("estoque_expansao") or 0), str(comprar), situacao,
            brl(item.get("subtotal")) if comprar > 0 and custo_ok else ("SEM CUSTO" if comprar > 0 else "R$ 0,00"),
        ]
        cx = margem
        for col_idx, ((title, w), val) in enumerate(zip(cols2, vals)):
            if title == "Situação":
                pdf.setFillColor(colors.HexColor(sit_cor))
                pdf.setFont("Helvetica-Bold", 6.7)
            elif title == "Valor projetado" and comprar > 0 and custo_ok:
                pdf.setFillColor(colors.HexColor("#4CD792"))
                pdf.setFont("Helvetica-Bold", 6.7)
            elif title == "Valor projetado" and comprar > 0 and not custo_ok:
                pdf.setFillColor(colors.HexColor("#FFB648"))
                pdf.setFont("Helvetica-Bold", 6.7)
            else:
                pdf.setFillColor(colors.HexColor("#DCE6F0"))
                pdf.setFont("Helvetica", 6.7)
            shown = _fit(val, w - 8, "Helvetica-Bold" if title in ("Situação", "Valor projetado") else "Helvetica", 6.7)
            if col_idx >= 2 and title != "Situação":
                pdf.drawRightString(cx + w - 4, y + 7, shown)
            else:
                pdf.drawString(cx + 4, y + 7, shown)
            cx += w

    _footer(page_no, "Conciliação de Orçamento")

    # Página adicional com as filiais consideradas, quando houver base real de projeção.
    if lojas:
        pdf.showPage()
        page_no += 1
        _bg()
        pdf.setFillColor(colors.white)
        pdf.setFont("Helvetica-Bold", 16)
        pdf.drawString(margem, alt - margem, "Lojas consideradas no orçamento")
        pdf.setFillColor(colors.HexColor("#9DB3C8"))
        pdf.setFont("Helvetica", 8)
        pdf.drawString(margem, alt - margem - 13, "Lojas com status PENDENTE no Acompanhamento de Expansão utilizadas para dimensionar o Kit Padrão e o pedido sugerido.")
        pdf.drawRightString(larg - margem, alt - margem - 13, f"Total: {len(lojas)} loja(s)")

        cols3 = [("Filial", 85), ("Nome", 300), ("UF", 55), ("Previsão de abertura", 135)]
        table_w3 = sum(w for _, w in cols3)
        y = alt - margem - 40
        pdf.setFillColor(colors.HexColor("#234C74"))
        pdf.roundRect(margem, y, table_w3, 20, 4, stroke=0, fill=1)
        pdf.setFillColor(colors.white)
        pdf.setFont("Helvetica-Bold", 7)
        cx = margem
        for title, w in cols3:
            pdf.drawString(cx + 4, y + 6, title)
            cx += w
        y -= 4
        for idx, item in enumerate(lojas):
            if y - row_h < 20 * mm:
                _footer(page_no, "Lojas consideradas no Orçamento")
                pdf.showPage()
                page_no += 1
                _bg()
                pdf.setFillColor(colors.white)
                pdf.setFont("Helvetica-Bold", 15)
                pdf.drawString(margem, alt - margem, "Lojas consideradas no orçamento - continuação")
                y = alt - margem - 34
                pdf.setFillColor(colors.HexColor("#234C74"))
                pdf.roundRect(margem, y, table_w3, 20, 4, stroke=0, fill=1)
                pdf.setFillColor(colors.white)
                pdf.setFont("Helvetica-Bold", 7)
                cx = margem
                for title, w in cols3:
                    pdf.drawString(cx + 4, y + 6, title)
                    cx += w
                y -= 4
            y -= row_h
            pdf.setFillColor(colors.HexColor("#182230" if idx % 2 == 0 else "#151D27"))
            pdf.roundRect(margem, y, table_w3, row_h - 1, 3, stroke=0, fill=1)
            vals = [item.get("codigo") or "-", item.get("nome") or "-", item.get("uf") or "--", item.get("previsao_abertura") or "-"]
            cx = margem
            for (title, w), val in zip(cols3, vals):
                pdf.setFillColor(colors.HexColor("#DCE6F0"))
                pdf.setFont("Helvetica", 6.9)
                pdf.drawString(cx + 4, y + 7, _fit(val, w - 8, "Helvetica", 6.9))
                cx += w
        _footer(page_no, "Lojas consideradas no Orçamento")

    pdf.save()
    buf.seek(0)
    return buf


@app.route("/export-orcamento")
@role_required("admin", "gestor", "operador")
def exportar_orcamento_excel():
    dados = _calcular_orcamento_pepi()
    buf = _gerar_excel_orcamento(dados)
    return send_file(buf, as_attachment=True, download_name=f"orcamento_pepi_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx", mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/pdf-orcamento")
@role_required("admin", "gestor", "operador")
def relatorio_pdf_orcamento():
    dados = _calcular_orcamento_pepi()
    buf = _gerar_pdf_orcamento(dados)
    return send_file(buf, as_attachment=True, download_name=f"orcamento_pepi_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf", mimetype="application/pdf")


# ---------------------------------------------------------------------
# API - Kit padrão de loja
# ---------------------------------------------------------------------

@app.route("/api/configuracao-expansao", methods=["GET"])
@login_required
def api_configuracao_expansao():
    return jsonify({"meta_lojas": db.obter_meta_lojas_expansao()})


@app.route("/api/configuracao-expansao", methods=["PUT"])
@edit_required
def api_salvar_configuracao_expansao():
    dados = request.get_json(force=True) or {}
    try:
        meta_lojas = int(dados.get("meta_lojas"))
    except (TypeError, ValueError):
        return jsonify({"erro": "Informe uma quantidade válida de lojas."}), 400
    if meta_lojas < 1 or meta_lojas > 999:
        return jsonify({"erro": "A quantidade de lojas deve ficar entre 1 e 999."}), 400
    meta_anterior = db.obter_meta_lojas_expansao()
    meta_lojas = db.salvar_meta_lojas_expansao(meta_lojas, session.get("username"))
    if int(meta_anterior) != int(meta_lojas):
        db.registrar_movimentacao(
            0,
            "meta_lojas",
            str(meta_lojas),
            session.get("username"),
            f"Meta do lote de inauguração alterada de {meta_anterior} para {meta_lojas} loja(s).",
            tabela="sistema",
        )
    return jsonify({"ok": True, "meta_lojas": meta_lojas})


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
# Edição em massa - Estoque / Imobilizados
# ---------------------------------------------------------------------

CAMPOS_EDICAO_MASSA = {
    "codigo", "qtde", "localizacao", "nf_entrada", "data_entrada",
    "nf_saida", "data_saida", "vd_loja", "filial_destino", "local",
    "armazenagem", "status", "nro_imobilizado", "nro_serie",
    "nro_patrimonio", "tipo_estoque", "pedido", "val_aquis", "chamado",
}


def _preparar_edicao_massa(payload):
    """Valida e normaliza o payload usado pela edição em massa.

    A edição em massa é deliberadamente não destrutiva: somente campos com um
    novo valor preenchido são modificados. Valores vazios ou ``None`` são
    ignorados, preservando o conteúdo atual de cada registro selecionado.
    """
    payload = payload or {}
    ids_brutos = payload.get("ids") or []
    campos_brutos = payload.get("campos") or {}

    if not isinstance(ids_brutos, list) or not ids_brutos:
        return None, None, "Nenhum item selecionado."
    if not isinstance(campos_brutos, dict) or not campos_brutos:
        return None, None, "Selecione pelo menos um campo para alterar."

    ids = []
    vistos = set()
    for valor in ids_brutos:
        try:
            item_id = int(valor)
        except (TypeError, ValueError):
            continue
        if item_id > 0 and item_id not in vistos:
            vistos.add(item_id)
            ids.append(item_id)
    if not ids:
        return None, None, "Nenhum item válido selecionado."
    if len(ids) > 5000:
        return None, None, "Selecione no máximo 5.000 registros por edição em massa."

    campos = {}
    for nome, valor in campos_brutos.items():
        if nome not in CAMPOS_EDICAO_MASSA:
            continue
        # Regra de segurança da edição em massa: vazio nunca apaga um valor
        # existente. Para incluir/alterar, é obrigatório informar um novo valor.
        if valor is None:
            continue
        if isinstance(valor, str):
            valor = valor.strip()
            if valor == "":
                continue
        campos[nome] = valor

    if not campos:
        return None, None, "Informe pelo menos um novo valor. Campos vazios mantêm os dados atuais."

    # O código do item vem do cadastro mestre de produtos. Quando ele muda,
    # a descrição acompanha automaticamente para manter Estoque/Imobilizados
    # consistentes com o Cadastro de Produtos.
    if "codigo" in campos:
        codigo = str(campos.get("codigo") or "").strip()
        if not codigo:
            return None, None, "O código do item não pode ficar vazio."
        produto = db.buscar_produto_por_codigo(codigo)
        if not produto:
            return None, None, "Código do item não encontrado no Cadastro de Produtos."
        campos["codigo"] = codigo
        campos["descricao"] = (produto.get("descricao") or "").strip()

    if "qtde" in campos:
        try:
            qtde = int(float(campos.get("qtde") or 0))
        except (TypeError, ValueError):
            return None, None, "Quantidade inválida."
        if qtde < 0:
            return None, None, "A quantidade não pode ser negativa."
        campos["qtde"] = str(qtde)

    return ids, campos, None


ROTULOS_CAMPOS_EDICAO_MASSA = {
    "codigo": "Código", "descricao": "Descrição", "qtde": "Quantidade",
    "localizacao": "UF", "nf_entrada": "NF entrada",
    "data_entrada": "Data entrada", "nf_saida": "NF saída",
    "data_saida": "Data saída", "vd_loja": "VD / referência",
    "filial_destino": "Filial / destino", "local": "Local",
    "armazenagem": "Armazenamento", "status": "Status",
    "nro_imobilizado": "Nº imobilizado", "nro_serie": "Nº série",
    "nro_patrimonio": "Nº patrimônio", "tipo_estoque": "Tipo de estoque",
    "pedido": "Pedido", "val_aquis": "ValAquis.", "chamado": "Chamado",
}


def _validar_senha_edicao_massa(payload):
    """Exige a senha atual do usuário para confirmar edição em massa."""
    senha = str((payload or {}).get("senha") or "")
    if not senha:
        return "Informe sua senha para confirmar a edição em massa."
    usuario_atual = db.buscar_usuario_por_id(session.get("user_id"))
    if not usuario_atual or not check_password_hash(usuario_atual["password_hash"], senha):
        return "Senha incorreta. Nenhuma alteração foi gravada."
    return None


@app.route("/api/edicao-em-massa/validar-senha", methods=["POST"])
@edit_required
def api_validar_senha_edicao_massa():
    payload = request.get_json(silent=True) or {}
    erro = _validar_senha_edicao_massa(payload)
    if erro:
        return jsonify({"erro": erro}), 403
    return jsonify({"ok": True})


def _preparar_edicao_massa_por_codigo(payload):
    """Valida o código-alvo e os campos que serão aplicados a todos os registros dele."""
    payload = payload or {}
    codigo_alvo = str(payload.get("codigo_alvo") or "").strip()
    if not codigo_alvo:
        return None, None, "Selecione o código do item que deseja editar em massa."

    # Reaproveita a normalização de campos sem depender da seleção do grid.
    campos_brutos = payload.get("campos") or {}
    if not isinstance(campos_brutos, dict) or not campos_brutos:
        return None, None, "Selecione pelo menos um campo para alterar."

    campos = {}
    for nome, valor in campos_brutos.items():
        if nome not in CAMPOS_EDICAO_MASSA:
            continue
        if valor is None:
            continue
        if isinstance(valor, str):
            valor = valor.strip()
            if valor == "":
                continue
        campos[nome] = valor

    if not campos:
        return None, None, "Informe pelo menos um novo valor. Campos vazios mantêm os dados atuais."

    if "codigo" in campos:
        novo_codigo = str(campos.get("codigo") or "").strip()
        produto = db.buscar_produto_por_codigo(novo_codigo) if novo_codigo else None
        if not produto:
            return None, None, "Novo código do item não encontrado no Cadastro de Produtos."
        campos["codigo"] = novo_codigo
        campos["descricao"] = (produto.get("descricao") or "").strip()

    if "qtde" in campos:
        try:
            qtde = int(float(campos.get("qtde") or 0))
        except (TypeError, ValueError):
            return None, None, "Quantidade inválida."
        if qtde < 0:
            return None, None, "A quantidade não pode ser negativa."
        campos["qtde"] = str(qtde)

    return codigo_alvo, campos, None


def _resumo_alteracoes_edicao_massa(item_antes, campos):
    """Monta o antes → depois que aparecerá no histórico/relatórios."""
    alteracoes = []
    for campo, novo in campos.items():
        if campo == "descricao" and "codigo" in campos:
            # A descrição acompanha o código e não precisa duplicar o registro.
            continue
        anterior = str((item_antes or {}).get(campo) or "").strip()
        depois = str(novo if novo is not None else "").strip()
        if anterior == depois:
            continue
        rotulo = ROTULOS_CAMPOS_EDICAO_MASSA.get(campo, campo)
        alteracoes.append(f"{rotulo}: {anterior or '(vazio)'} → {depois or '(vazio)'}")
    return "; ".join(alteracoes) or "Nenhuma diferença de valor identificada"


# ---------------------------------------------------------------------
# Validação de campos obrigatórios - Estoque / Imobilizados
# ---------------------------------------------------------------------

UFS_VALIDAS = {"AC","AL","AP","AM","BA","CE","DF","ES","GO","MA","MT","MS","MG","PA","PB","PR","PE","PI","RJ","RN","RS","RO","RR","SC","SP","SE","TO"}

CAMPOS_OBRIGATORIOS_CADASTRO = [
    ("local", "Local"),
    ("armazenagem", "Armazenamento"),
    ("status", "Status"),
    ("tipo_estoque", "Tipo de estoque"),
    ("localizacao", "UF"),
    ("qtde", "Qtde"),
]


def _validar_campos_obrigatorios_cadastro(dados):
    """Valida os campos mínimos exigidos em novos cadastros/importações.

    ``localizacao`` continua sendo o nome interno da coluna no banco para
    preservar compatibilidade, mas na interface esse campo representa a UF.
    """
    dados = dados or {}
    faltantes = []
    for campo, rotulo in CAMPOS_OBRIGATORIOS_CADASTRO:
        valor = dados.get(campo)
        if valor is None or str(valor).strip() == "":
            faltantes.append(rotulo)

    if "Qtde" not in faltantes:
        try:
            qtd = int(float(str(dados.get("qtde")).replace(",", ".")))
        except (TypeError, ValueError):
            qtd = 0
        if qtd <= 0:
            faltantes.append("Qtde")

    # Padroniza UF informada nos novos registros. A validação é propositalmente
    # simples para não bloquear dados legados; o formulário usa lista oficial.
    if "UF" not in faltantes:
        uf = str(dados.get("localizacao") or "").strip().upper()
        dados["localizacao"] = uf
        if uf not in UFS_VALIDAS:
            return "Falta informação para liberar o cadastro para ser salvo. Informe uma UF válida (ex.: SP, RJ, MG)."

    if faltantes:
        nomes = ", ".join(dict.fromkeys(faltantes))
        return f"Falta informação para liberar o cadastro para ser salvo. Preencha: {nomes}."
    return None


def _normalizar_modo_importacao(valor):
    valor = str(valor or "adicionar").strip().lower()
    return "substituir" if valor == "substituir" else "adicionar"


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
    erro_obrigatorios = _validar_campos_obrigatorios_cadastro(dados)
    if erro_obrigatorios:
        return jsonify({"erro": erro_obrigatorios}), 400

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


@app.route("/api/imobilizados/editar-em-lote", methods=["POST"])
@edit_required
def api_editar_imobilizados_em_lote():
    payload = request.get_json(silent=True) or {}
    erro_senha = _validar_senha_edicao_massa(payload)
    if erro_senha:
        return jsonify({"erro": erro_senha}), 403
    codigo_alvo, campos, erro = _preparar_edicao_massa_por_codigo(payload)
    if erro:
        return jsonify({"erro": erro}), 400
    registros = [
        item for item in (db.listar_imobilizados() or [])
        if str(item.get("codigo") or "").strip() == codigo_alvo
    ]
    if not registros:
        return jsonify({"erro": "Nenhum imobilizado encontrado para o código selecionado."}), 404

    usuario = session.get("username")
    agora = datetime.now().strftime("%Y-%m-%d %H:%M")
    lote = datetime.now().strftime("%Y%m%d%H%M%S")
    campos_atualizacao = dict(campos, atualizado_por=usuario, atualizado_em=agora)
    atualizados = 0
    ignorados = 0

    for item_antes in registros:
        item_id = item_antes.get("id")
        if not item_id:
            ignorados += 1
            continue
        resumo = _resumo_alteracoes_edicao_massa(item_antes, campos)
        if db.atualizar_imobilizado(item_id, campos_atualizacao):
            atualizados += 1
            db.registrar_movimentacao(
                item_id, "edicao", None, usuario,
                f"Edição em massa por código confirmada com senha · código-alvo {codigo_alvo} · lote {lote} · {resumo}",
                tabela="imobilizados",
            )

    return jsonify({
        "ok": True, "codigo_alvo": codigo_alvo, "total_encontrados": len(registros),
        "atualizados": atualizados, "ignorados": ignorados, "lote": lote
    })


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
    dados = request.get_json(silent=True) or {}
    ids = dados.get("ids") or []
    if not isinstance(ids, list) or not ids:
        return jsonify({"erro": "Nenhum item selecionado."}), 400
    try:
        excluidos, nao_encontrados = db.excluir_imobilizados_em_lote_auditado(ids, session.get("username"))
    except Exception:
        app.logger.exception("Erro na exclusão em massa de Imobilizados")
        return jsonify({"erro": "Não foi possível excluir os imobilizados selecionados. Tente novamente."}), 500
    return jsonify({"ok": True, "excluidos": len(excluidos), "nao_encontrados": nao_encontrados})


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


def _texto_pdf(valor):
    """Texto seguro para células Paragraph do ReportLab."""
    if valor is None:
        return "-"
    texto = str(valor).strip()
    if not texto:
        return "-"
    return (texto.replace("&", "&amp;")
                 .replace("<", "&lt;")
                 .replace(">", "&gt;"))


def _numero_inteiro_seguro(valor, padrao=0):
    try:
        return int(float(valor or 0))
    except (TypeError, ValueError):
        return padrao


def _gerar_pdf_equipamentos(itens, titulo, subtitulo, prefixo_arquivo):
    """Gera PDF operacional no servidor, sem depender de bibliotecas JS/CDN.

    O relatório é dividido em duas seções para continuar legível mesmo com
    milhares de registros: visão operacional e rastreabilidade/movimentação.
    """
    itens = list(itens or [])
    buffer = io.BytesIO()
    largura_pagina, altura_pagina = landscape(A4)
    margem = 9 * mm

    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        rightMargin=margem,
        leftMargin=margem,
        topMargin=17 * mm,
        bottomMargin=15 * mm,
        title=titulo,
        author="Expansão de TI",
    )

    estilos = getSampleStyleSheet()
    estilo_titulo = ParagraphStyle(
        "RptTitulo", parent=estilos["Heading1"], fontName="Helvetica-Bold",
        fontSize=17, leading=20, textColor=colors.HexColor("#1A2029"), spaceAfter=3 * mm,
    )
    estilo_sub = ParagraphStyle(
        "RptSub", parent=estilos["BodyText"], fontName="Helvetica",
        fontSize=8.5, leading=11, textColor=colors.HexColor("#66707D"), spaceAfter=4 * mm,
    )
    estilo_secao = ParagraphStyle(
        "RptSecao", parent=estilos["Heading2"], fontName="Helvetica-Bold",
        fontSize=11, leading=14, textColor=colors.HexColor("#1A2029"), spaceBefore=2 * mm, spaceAfter=2 * mm,
    )
    estilo_celula = ParagraphStyle(
        "RptCelula", parent=estilos["BodyText"], fontName="Helvetica",
        fontSize=5.8, leading=7.1, textColor=colors.HexColor("#1A2029"),
    )
    estilo_celula_centro = ParagraphStyle(
        "RptCelulaCentro", parent=estilo_celula, alignment=TA_CENTER,
    )

    def P(valor, centro=False):
        return Paragraph(_texto_pdf(valor), estilo_celula_centro if centro else estilo_celula)

    total_unidades = sum(max(0, _numero_inteiro_seguro(x.get("qtde"), 1)) for x in itens)
    com_serie = sum(1 for x in itens if str(x.get("nro_serie") or "").strip())
    com_patrimonio = sum(1 for x in itens if str(x.get("nro_patrimonio") or "").strip())
    com_destino = sum(1 for x in itens if str(x.get("filial_destino") or "").strip())

    historia = [
        Paragraph(_texto_pdf(titulo), estilo_titulo),
        Paragraph(
            _texto_pdf(subtitulo) + "<br/>Gerado em " + datetime.now().strftime("%d/%m/%Y %H:%M"),
            estilo_sub,
        ),
    ]

    resumo = [
        ["REGISTROS", "UNIDADES", "COM Nº SÉRIE", "COM PATRIMÔNIO", "COM FILIAL DESTINO"],
        [str(len(itens)), str(total_unidades), str(com_serie), str(com_patrimonio), str(com_destino)],
    ]
    t_resumo = Table(resumo, colWidths=[52 * mm] * 5, hAlign="LEFT")
    t_resumo.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#212934")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#D8DEE9")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 6.5),
        ("BACKGROUND", (0, 1), (-1, 1), colors.HexColor("#F5F7FA")),
        ("TEXTCOLOR", (0, 1), (-1, 1), colors.HexColor("#2876BE")),
        ("FONTNAME", (0, 1), (-1, 1), "Helvetica-Bold"),
        ("FONTSIZE", (0, 1), (-1, 1), 13),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("BOX", (0, 0), (-1, -1), 0.35, colors.HexColor("#DDE2E8")),
        ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#E6E9ED")),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    historia.extend([t_resumo, Spacer(1, 5 * mm)])

    historia.append(Paragraph("Dados operacionais", estilo_secao))
    cab1 = ["ID", "Código", "Descrição", "Qtd.", "UF", "Local", "Armazenamento", "Status", "Tipo estoque", "Filial destino", "Nº imobilizado", "Nº série", "Patrimônio"]
    dados1 = [[Paragraph(c, estilo_celula_centro) for c in cab1]]
    for it in itens:
        dados1.append([
            P(it.get("id"), True), P(it.get("codigo"), True), P(it.get("descricao")),
            P(it.get("qtde"), True), P(it.get("localizacao"), True), P(it.get("local")),
            P(it.get("armazenagem")), P(it.get("status")), P(it.get("tipo_estoque")),
            P(it.get("filial_destino")), P(it.get("nro_imobilizado")), P(it.get("nro_serie")), P(it.get("nro_patrimonio")),
        ])
    col1 = [8, 16, 48, 8, 8, 19, 19, 16, 19, 24, 20, 25, 20]
    t1 = Table(dados1, colWidths=[x * mm for x in col1], repeatRows=1, hAlign="LEFT")
    t1.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1A2029")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 5.7),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.22, colors.HexColor("#D8DEE6")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F6F8FA")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 2.3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.3),
    ]))
    historia.append(t1)

    historia.extend([PageBreak(), Paragraph("Movimentação e auditoria", estilo_secao)])
    cab2 = ["ID", "Código", "NF entrada", "Data entrada", "NF saída", "Data saída", "VD / referência", "Pedido", "ValAquis.", "Chamado", "Criado por", "Alterado por", "Alterado em"]
    dados2 = [[Paragraph(c, estilo_celula_centro) for c in cab2]]
    for it in itens:
        dados2.append([
            P(it.get("id"), True), P(it.get("codigo"), True), P(it.get("nf_entrada")), P(it.get("data_entrada")),
            P(it.get("nf_saida")), P(it.get("data_saida")), P(it.get("vd_loja")), P(it.get("pedido")),
            P(it.get("val_aquis")), P(it.get("chamado")), P(it.get("criado_por")), P(it.get("atualizado_por")), P(it.get("atualizado_em")),
        ])
    col2 = [8, 17, 19, 19, 19, 19, 24, 18, 18, 18, 23, 23, 25]
    t2 = Table(dados2, colWidths=[x * mm for x in col2], repeatRows=1, hAlign="LEFT")
    t2.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2876BE")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 5.7),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.22, colors.HexColor("#D8DEE6")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F6F8FA")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 2),
        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
        ("TOPPADDING", (0, 0), (-1, -1), 2.3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2.3),
    ]))
    historia.append(t2)

    def rodape(c, d):
        c.saveState()
        c.setStrokeColor(colors.HexColor("#D8DEE6"))
        c.setLineWidth(0.4)
        c.line(margem, 10 * mm, largura_pagina - margem, 10 * mm)
        c.setFont("Helvetica", 6.5)
        c.setFillColor(colors.HexColor("#7A8492"))
        c.drawString(margem, 6.5 * mm, "© 2026 · Developed by ALM - Expansão de TI")
        c.drawRightString(largura_pagina - margem, 6.5 * mm, f"Página {d.page}")
        c.restoreState()

    doc.build(historia, onFirstPage=rodape, onLaterPages=rodape)
    buffer.seek(0)
    nome = f"{prefixo_arquivo}_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
    return buffer, nome


@app.route("/export-imobilizados-pdf")
@role_required("admin", "gestor", "operador")
def exportar_imobilizados_pdf():
    buffer, nome = _gerar_pdf_equipamentos(
        db.listar_imobilizados(),
        "Relatório de Imobilizados",
        "Cadastro completo dos equipamentos imobilizados, com dados operacionais, rastreabilidade e auditoria.",
        "imobilizados",
    )
    return send_file(buffer, as_attachment=True, download_name=nome, mimetype="application/pdf")


@app.route("/export-imobilizados")
@role_required("admin", "gestor", "operador")
def exportar_imobilizados_excel():
    itens = db.listar_imobilizados()
    wb = Workbook()
    ws = wb.active
    ws.title = "Imobilizados"
    colunas = ["ID", "Codigo do item", "Descricao", "Qtde", "UF",
               "NF de entrada", "Data de entrada", "NF de saida",
               "Data de saida", "VD / referencia", "Filial destino", "Local",
               "Armazenamento", "Status", "Nro Imobilizado", "Nro Serie",
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
@role_required("admin", "gestor", "operador")
def exportar_relatorio_lojas_excel():
    """Gera um relatório gerencial em Excel com o mesmo resumo usado no PDF."""
    dados = request.get_json(force=True) or {}
    estoque = dados.get("estoque") or []
    faltantes = dados.get("faltantes") or []
    kit = dados.get("kit") or []
    meta_lojas = int(dados.get("meta_lojas") or 10)
    lote_pronto = bool(dados.get("lote_pronto"))

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
        ("Meta de inauguração", f"{meta_lojas} lojas", cor_verde if lote_pronto else cor_laranja),
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
    ws_f = wb.create_sheet(f"Faltantes meta {meta_lojas} lojas")
    titulo_planilha(ws_f, f"Itens faltantes para completar a premissa de {meta_lojas} lojas", "Premissa padrão usada na preparação das inaugurações", 5)
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
        ws_f["A5"] = f"Premissa atendida: nenhum item faltante para o lote padrão de {meta_lojas} lojas."
        ws_f["A5"].font = Font(bold=True, color=cor_verde)
        ws_f["A5"].alignment = Alignment(horizontal="center", vertical="center")
    ajustar_larguras(ws_f, [22, 48, 18, 20, 16])
    ws_f.freeze_panes = "A5"
    if faltantes:
        ws_f.auto_filter.ref = f"A4:E{ws_f.max_row}"

    # Kit padrão
    ws_k = wb.create_sheet("Kit padrao por loja")
    titulo_planilha(ws_k, "Kit padrão por loja", f"Base por loja utilizada na premissa de inauguração de {meta_lojas} lojas", 5)
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
        sheet.oddFooter.center.text = "© 2026 · Developed by ALM - Expansão de TI"
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
    erro_obrigatorios = _validar_campos_obrigatorios_cadastro(dados)
    if erro_obrigatorios:
        return jsonify({"erro": erro_obrigatorios}), 400
    if _normalizar_exec(dados.get("status")) == "enviado":
        return jsonify({
            "erro": "Para enviar um item a uma filial, cadastre-o primeiro no estoque e depois use a ação 'Baixa de estoque'."
        }), 400

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

    dados, erro_baixa = _preparar_baixa_envio_item(item_antes, dados)
    if erro_baixa:
        return jsonify({"erro": erro_baixa}), 400

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
                obs = (
                    f"Baixa de estoque / envio para filial "
                    f"(NF {dados.get('nf_saida')}, destino: {dados.get('filial_destino') or dados.get('vd_loja') or '-'}, "
                    f"imobilizado: {dados.get('nro_imobilizado') or '-'}, série: {dados.get('nro_serie') or '-'}, "
                    f"patrimônio: {dados.get('nro_patrimonio') or '-'})"
                )
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

    ativacao = {"ativada": False}
    if _normalizar_exec(dados.get("status")) == "enviado" and str(dados.get("filial_destino") or item_antes.get("filial_destino") or "").strip():
        codigo_destino = str(dados.get("filial_destino") or item_antes.get("filial_destino") or "").strip()
        try:
            ativacao = _ativar_filial_se_kit_real_completo(codigo_destino, session.get("username"))
        except Exception:
            app.logger.exception("Falha ao verificar ativação automática da filial %s após baixa", codigo_destino)

    return jsonify({"ok": True, "ativacao_filial": ativacao})


@app.route("/api/itens/editar-em-lote", methods=["POST"])
@edit_required
def api_editar_itens_em_lote():
    payload = request.get_json(silent=True) or {}
    erro_senha = _validar_senha_edicao_massa(payload)
    if erro_senha:
        return jsonify({"erro": erro_senha}), 403
    codigo_alvo, campos, erro = _preparar_edicao_massa_por_codigo(payload)
    if erro:
        return jsonify({"erro": erro}), 400
    if _normalizar_exec(campos.get("status")) == "enviado":
        return jsonify({
            "erro": "O status Enviado não pode ser aplicado em massa. Use 'Baixa de estoque' por unidade para informar NF, data, filial, Nº imobilizado, Nº série e Nº patrimônio."
        }), 400

    registros = [
        item for item in (db.listar_itens() or [])
        if str(item.get("codigo") or "").strip() == codigo_alvo
    ]
    if not registros:
        return jsonify({"erro": "Nenhum item de estoque encontrado para o código selecionado."}), 404

    usuario = session.get("username")
    agora = datetime.now().strftime("%Y-%m-%d %H:%M")
    lote = datetime.now().strftime("%Y%m%d%H%M%S")
    campos_atualizacao = dict(campos, atualizado_por=usuario, atualizado_em=agora)
    atualizados = 0
    ignorados = 0

    for item_antes in registros:
        item_id = item_antes.get("id")
        if not item_id:
            ignorados += 1
            continue
        resumo = _resumo_alteracoes_edicao_massa(item_antes, campos)
        if db.atualizar_item(item_id, campos_atualizacao):
            atualizados += 1
            db.registrar_movimentacao(
                item_id, "edicao", None, usuario,
                f"Edição em massa por código confirmada com senha · código-alvo {codigo_alvo} · lote {lote} · {resumo}",
                tabela="itens",
            )

    return jsonify({
        "ok": True, "codigo_alvo": codigo_alvo, "total_encontrados": len(registros),
        "atualizados": atualizados, "ignorados": ignorados, "lote": lote
    })


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
    dados = request.get_json(silent=True) or {}
    ids = dados.get("ids") or []
    if not isinstance(ids, list) or not ids:
        return jsonify({"erro": "Nenhum item selecionado."}), 400
    try:
        excluidos, nao_encontrados = db.excluir_itens_em_lote_auditado(ids, session.get("username"))
    except Exception:
        app.logger.exception("Erro na exclusão em massa do Estoque")
        return jsonify({"erro": "Não foi possível excluir os itens selecionados. Tente novamente."}), 500
    return jsonify({"ok": True, "excluidos": len(excluidos), "nao_encontrados": nao_encontrados})


@app.route("/api/itens/<int:item_id>/movimentacoes")
@login_required
def api_movimentacoes(item_id):
    return jsonify(db.listar_movimentacoes(item_id, tabela="itens"))


@app.route("/export-pdf")
@role_required("admin", "gestor", "operador")
def exportar_estoque_pdf():
    buffer, nome = _gerar_pdf_equipamentos(
        db.listar_itens(),
        "Relatório de Estoque",
        "Cadastro completo do estoque, com dados operacionais, rastreabilidade e auditoria.",
        "estoque",
    )
    return send_file(buffer, as_attachment=True, download_name=nome, mimetype="application/pdf")


@app.route("/export")
@role_required("admin", "gestor", "operador")
def exportar_excel():
    itens = db.listar_itens()
    wb = Workbook()
    ws = wb.active
    ws.title = "Estoque"
    colunas = ["ID", "Codigo do item", "Descricao", "Qtde", "UF",
               "NF de entrada", "Data de entrada", "NF de saida",
               "Data de saida", "VD / referencia", "Filial destino", "Local",
               "Armazenamento", "Status", "Nro Imobilizado", "Nro Serie",
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
    "localizacao": ["localizacao", "uf", "estado"],
    "nf_entrada": ["nfdeentrada", "nfentrada", "notafiscaldeentrada", "nf"],
    "data_entrada": ["datadeentrada", "dataentrada"],
    "nf_saida": ["nfdesaida", "nfsaida", "notafiscaldesaida"],
    "data_saida": ["datadesaida", "datasaida"],
    "vd_loja": ["vddalojadestino", "vddaloja", "vdloja", "vd", "lojadestino"],
    "filial_destino": ["filialdestino", "filial", "codigofilial", "lojafilial", "destinofilial"],
    "local": ["local"],
    "armazenagem": ["armazenagem", "armazenamento", "localarmazenagem", "localdearmazenamento"],
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


@app.route("/api/itens/importar/validar", methods=["POST"])
@edit_required
def api_validar_importacao_itens():
    tabela_destino = request.form.get("tabela", "estoque")
    if tabela_destino not in ("estoque", "imobilizados"):
        tabela_destino = "estoque"
    modo = _normalizar_modo_importacao(request.form.get("modo"))
    arquivo = request.files.get("arquivo")
    if not arquivo or not arquivo.filename:
        return jsonify({"erro": "Nenhum arquivo enviado."}), 400
    try:
        wb = load_workbook(arquivo, read_only=True, data_only=True)
        ws = wb.active
        linhas = ws.iter_rows(values_only=True)
        try:
            cabecalho = next(linhas)
        except StopIteration:
            return jsonify({"erro": "A planilha está vazia."}), 400
        mapa = _mapear_colunas(cabecalho)
        if "codigo" not in mapa.values():
            return jsonify({"erro": "Não encontrei uma coluna de Código do item."}), 400

        # O upload novo também precisa trazer todos os campos obrigatórios.
        campos_presentes = set(mapa.values())
        colunas_faltantes = [rotulo for campo, rotulo in CAMPOS_OBRIGATORIOS_CADASTRO if campo not in campos_presentes]
        if colunas_faltantes:
            return jsonify({
                "erro": "Falta informação para liberar o cadastro para ser salvo. "
                        "A planilha precisa ter as colunas: " + ", ".join(colunas_faltantes) + "."
            }), 400

        total = validas = registros = 0
        erros = []
        amostra = []
        for n, linha in enumerate(linhas, start=2):
            if linha is None or all(v is None for v in linha):
                continue
            total += 1
            dados = {}
            for indice, campo in mapa.items():
                if indice < len(linha):
                    dados[campo] = _valor_para_texto(linha[indice])
            if dados.get("tipo_estoque"):
                dados["tipo_estoque"] = _canonicalizar_tipo_estoque(dados["tipo_estoque"])
            if dados.get("localizacao"):
                dados["localizacao"] = str(dados["localizacao"]).strip().upper()
            if not dados.get("codigo"):
                erros.append(f"Linha {n}: Código do item não informado.")
                continue
            erro_campos = _validar_campos_obrigatorios_cadastro(dados)
            if erro_campos:
                erros.append(f"Linha {n}: {erro_campos}")
                continue
            validas += 1
            qtd = int(float(str(dados.get("qtde")).replace(",", ".")))
            registros += qtd if tabela_destino == "estoque" else 1
            if len(amostra) < 8:
                amostra.append({
                    "codigo": dados.get("codigo"),
                    "descricao": dados.get("descricao", ""),
                    "qtde": qtd,
                    "uf": dados.get("localizacao", ""),
                    "tipo": dados.get("tipo_estoque", ""),
                })

        if erros:
            resumo = erros[:15]
            extra = max(0, len(erros) - len(resumo))
            msg = "Falta informação para liberar o cadastro para ser salvo. Corrija a planilha antes do upload. " + " | ".join(resumo)
            if extra:
                msg += f" | ... e mais {extra} linha(s) com erro."
            return jsonify({"erro": msg, "erros": erros[:50], "total_linhas": total, "validas": validas}), 400
        if not validas:
            return jsonify({"erro": "Nenhuma linha válida encontrada na planilha."}), 400

        existentes = len(db.listar_itens() if tabela_destino == "estoque" else db.listar_imobilizados())
        return jsonify({
            "ok": True, "arquivo": arquivo.filename, "tabela": tabela_destino, "modo": modo,
            "total_linhas": total, "validas": validas, "ignoradas": 0,
            "registros_previstos": registros, "existentes": existentes, "erros": [], "amostra": amostra,
        })
    except Exception as e:
        return jsonify({"erro": f"Erro ao validar a planilha: {e}"}), 500

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
    modo = _normalizar_modo_importacao(request.form.get("modo"))

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
            return jsonify({"erro": "Não encontrei uma coluna de 'Código do item' na planilha."}), 400
        campos_presentes = set(mapa_colunas.values())
        colunas_faltantes = [rotulo for campo, rotulo in CAMPOS_OBRIGATORIOS_CADASTRO if campo not in campos_presentes]
        if colunas_faltantes:
            return jsonify({
                "erro": "Falta informação para liberar o cadastro para ser salvo. "
                        "A planilha precisa ter as colunas: " + ", ".join(colunas_faltantes) + "."
            }), 400

        usuario = session.get("username")
        novos_itens = []
        erros = []
        linhas_validas_count = 0
        total_linhas_planilha = 0

        for n, linha in enumerate(linhas, start=2):
            if linha is None or all(v is None for v in linha):
                continue
            total_linhas_planilha += 1
            dados = {}
            for indice, campo in mapa_colunas.items():
                if indice < len(linha):
                    dados[campo] = _valor_para_texto(linha[indice])
            if dados.get("tipo_estoque"):
                dados["tipo_estoque"] = _canonicalizar_tipo_estoque(dados["tipo_estoque"])
            if dados.get("localizacao"):
                dados["localizacao"] = str(dados["localizacao"]).strip().upper()
            if not dados.get("codigo"):
                erros.append(f"Linha {n}: Código do item não informado.")
                continue
            erro_campos = _validar_campos_obrigatorios_cadastro(dados)
            if erro_campos:
                erros.append(f"Linha {n}: {erro_campos}")
                continue

            linhas_validas_count += 1
            dados["criado_por"] = usuario
            if not dados.get("data_entrada"):
                dados["data_entrada"] = datetime.now().strftime("%Y-%m-%d")
            qtd_linha = int(float(str(dados.get("qtde")).replace(",", ".")))

            if tabela_destino == "estoque":
                base = dict(dados)
                base["qtde"] = "1"
                novos_itens.extend(dict(base) for _ in range(qtd_linha))
            else:
                # Imobilizado mantém a quantidade da linha, como já ocorria no upload.
                dados["qtde"] = str(qtd_linha)
                novos_itens.append(dados)

        if erros:
            resumo = erros[:15]
            extra = max(0, len(erros) - len(resumo))
            msg = "Falta informação para liberar o cadastro para ser salvo. Corrija a planilha antes do upload. " + " | ".join(resumo)
            if extra:
                msg += f" | ... e mais {extra} linha(s) com erro."
            return jsonify({"erro": msg, "erros": erros[:50]}), 400
        if not novos_itens:
            return jsonify({"erro": "Nenhuma linha válida encontrada na planilha."}), 400

        observacao = f"Importado via planilha ({arquivo.filename}) · modo {modo}"
        removidos = 0
        if modo == "substituir":
            if tabela_destino == "estoque":
                resultado = db.substituir_itens_em_lote(novos_itens, usuario, observacao=observacao)
            else:
                resultado = db.substituir_imobilizados_em_lote(novos_itens, usuario, observacao=observacao)
            total = resultado.get("criados", 0)
            removidos = resultado.get("removidos", 0)
        else:
            if tabela_destino == "estoque":
                total = db.criar_itens_em_lote(novos_itens, usuario, observacao=observacao)
            else:
                total = db.criar_imobilizados_em_lote(novos_itens, usuario, observacao=observacao)

        try:
            db.registrar_importacao(
                tabela_destino, arquivo.filename, total_linhas=total_linhas_planilha,
                validas=linhas_validas_count, criadas=total, atualizadas=0, ignoradas=0,
                usuario=usuario, status="concluida",
                detalhes=(f"Modo: {modo}. Registros anteriores removidos: {removidos}." if modo == "substituir" else "Modo: adicionar aos existentes.")
            )
        except Exception as audit_err:
            print(f"[aviso] Falha ao registrar auditoria de importação: {audit_err}")
        return jsonify({
            "ok": True, "importados": total, "ignoradas": 0, "tabela": tabela_destino,
            "modo": modo, "removidos": removidos
        })

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
    email = (dados.get("email") or "").strip()
    whatsapp = (dados.get("whatsapp") or "").strip()

    if not username or not password:
        return jsonify({"erro": "Usuário e senha são obrigatórios."}), 400
    if len(password) < 6:
        return jsonify({"erro": "A senha precisa ter pelo menos 6 caracteres."}), 400
    if email and not re.match(r'^[^\s@]+@[^\s@]+\.[^\s@]+$',email):
        return jsonify({"erro": "E-mail inválido."}), 400
    if db.buscar_usuario_por_username(username):
        return jsonify({"erro": "Já existe um usuário com esse nome."}), 400

    db.criar_usuario(username, password, role, email=email, whatsapp=whatsapp)
    # A senha temporária é devolvida somente nesta resposta ao Administrador.
    # No banco permanece apenas o hash; não há recuperação posterior em texto aberto.
    return jsonify({"ok": True, "username": username, "senha_temporaria": password}), 201


@app.route("/api/usuarios/<int:user_id>/contato", methods=["PUT"])
@admin_required
def api_atualizar_contato_usuario(user_id):
    alvo=db.buscar_usuario_por_id(user_id)
    if not alvo:
        return jsonify({"erro":"Usuário não encontrado."}),404
    dados=request.get_json(silent=True) or {}
    email=(dados.get('email') or '').strip()
    whatsapp=(dados.get('whatsapp') or '').strip()
    if email and not re.match(r'^[^\s@]+@[^\s@]+\.[^\s@]+$',email):
        return jsonify({'erro':'E-mail inválido.'}),400
    db.atualizar_contato_usuario(user_id,email,whatsapp)
    db.registrar_movimentacao(0,'usuario_contato','1',session.get('username'),f"Contatos de {alvo.get('username')} atualizados.",tabela='sistema')
    return jsonify({'ok':True})



@app.route("/api/usuarios/<int:user_id>/enviar-resumo", methods=["POST"])
@admin_required
def api_enviar_resumo_usuario(user_id):
    return jsonify({'erro':'Envios por e-mail e WhatsApp foram desativados. Os dados de contato continuam disponíveis no cadastro do usuário.'}), 410


@app.route("/api/usuarios/<int:user_id>/forcar-troca-senha", methods=["POST"])
@admin_required
def api_forcar_troca_senha(user_id):
    alvo = db.buscar_usuario_por_id(user_id)
    if not alvo:
        return jsonify({"erro": "Usuário não encontrado."}), 404
    db.forcar_troca_senha(user_id)
    return jsonify({"ok": True})


@app.route("/api/usuarios/<int:user_id>/reset-mfa", methods=["POST"])
@admin_required
def api_reset_mfa_usuario(user_id):
    if not _csrf_ok():
        return jsonify({"erro": "Token de segurança inválido."}), 400
    alvo = db.buscar_usuario_por_id(user_id)
    if not alvo:
        return jsonify({"erro": "Usuário não encontrado."}), 404
    if alvo.get("mfa_enabled") != "1":
        return jsonify({"erro": "Este usuário não possui MFA ativo."}), 400
    db.desativar_mfa_usuario(user_id)
    db.registrar_evento_login(alvo.get("username"), _client_ip(), "mfa_reset_admin", f"MFA resetado pelo administrador {session.get('username')}")
    return jsonify({"ok": True, "mensagem": "MFA resetado. O usuário deverá configurar novamente no próximo acesso."})


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
# Agente IA — consultas seguras, somente leitura
# ---------------------------------------------------------------------

GEMINI_DEFAULT_MODEL = "gemini-3.5-flash-lite"
CLOUDFLARE_DEFAULT_MODEL = "@cf/google/gemma-4-26b-a4b-it"
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
AI_MAX_HISTORY = 10
AI_MAX_TOOL_ROUNDS = 6
AI_PROVIDER_TIMEOUT = 8
AI_TOTAL_TIMEOUT = 25


def _agente_role():
    role = session.get("role") or "user"
    return "operador" if role == "user" else role


def _agente_limite(valor, padrao=30, maximo=100):
    try:
        n = int(valor or padrao)
    except (TypeError, ValueError):
        n = padrao
    return max(1, min(n, maximo))


def _agente_json(valor):
    """Converte resultados do banco em estruturas serializáveis pela API de IA."""
    if isinstance(valor, Decimal):
        return format(valor, "f")
    if isinstance(valor, (datetime,)):
        return valor.isoformat(sep=" ")
    if isinstance(valor, dict):
        return {str(k): _agente_json(v) for k, v in valor.items()}
    if isinstance(valor, (list, tuple)):
        return [_agente_json(v) for v in valor]
    return valor


def _agente_texto_normalizado(valor):
    return _normalizar_exec(valor)


def _agente_resumo_executivo(role):
    base = db.obter_dashboard_compacto(12)
    visao = _calcular_visao_executiva(
        itens=base.get("itens") or [],
        kit=base.get("kit") or [],
        filiais=base.get("filiais") or [],
        meta=base.get("meta_lojas") or 10,
    )
    resultado = {
        "fonte": "Dashboard / banco atual",
        "estoque_total_unidades": base.get("estoque_total") or 0,
        "imobilizados_total_unidades": base.get("imobilizados_total") or 0,
        "produtos_cadastrados": base.get("produtos_total") or 0,
        "filiais_ativas": base.get("filiais_ativas") or 0,
        "meta_lojas": base.get("meta_lojas") or 10,
        "expansao": visao,
        "ultimo_backup": base.get("ultimo_backup"),
    }
    # Orçamento é uma área bloqueada para Consulta; não deve vazar pelo agente.
    if role != "consulta":
        orc = _calcular_orcamento_pepi()
        resultado["orcamento_resumo"] = {
            "pepi_consolidado": orc.get("pepi_consolidado"),
            "total_previsto": orc.get("total_previsto"),
            "saldo": orc.get("saldo"),
            "itens_para_comprar": orc.get("itens_para_comprar"),
            "total_unidades_pedido": orc.get("total_unidades_pedido"),
            "orcamento_suficiente": orc.get("orcamento_suficiente"),
        }
    return resultado


def _agente_consultar_estoque(args):
    termo = _agente_texto_normalizado(args.get("termo"))
    finalidade = _agente_texto_normalizado(args.get("finalidade"))
    limite = _agente_limite(args.get("limite"), 30, 80)
    itens = db.obter_dashboard_compacto(1).get("itens") or []
    filtrados = []
    for item in itens:
        alvo = _agente_texto_normalizado(f"{item.get('codigo','')} {item.get('descricao','')}")
        tipo = _agente_texto_normalizado(item.get("tipo_estoque"))
        if termo and termo not in alvo:
            continue
        if finalidade and finalidade not in tipo:
            continue
        filtrados.append({
            "codigo": item.get("codigo"),
            "descricao": item.get("descricao"),
            "finalidade": item.get("tipo_estoque"),
            "quantidade": int(item.get("qtde") or 0),
        })
    filtrados.sort(key=lambda x: (-int(x.get("quantidade") or 0), str(x.get("codigo") or "")))
    return {
        "fonte": "Estoque atual",
        "filtros": {"termo": args.get("termo") or "", "finalidade": args.get("finalidade") or ""},
        "grupos_encontrados": len(filtrados),
        "quantidade_total": sum(int(x.get("quantidade") or 0) for x in filtrados),
        "itens": filtrados[:limite],
        "resultado_limitado": len(filtrados) > limite,
    }


def _agente_consultar_imobilizados(args):
    termo = _agente_texto_normalizado(args.get("termo"))
    limite = _agente_limite(args.get("limite"), 30, 60)
    linhas = db.listar_imobilizados()
    achados = []
    for x in linhas:
        alvo = _agente_texto_normalizado(" ".join(str(x.get(k) or "") for k in (
            "codigo", "descricao", "nro_serie", "nro_patrimonio", "nro_imobilizado", "localizacao", "filial_destino"
        )))
        if termo and termo not in alvo:
            continue
        achados.append({
            "id": x.get("id"), "codigo": x.get("codigo"), "descricao": x.get("descricao"),
            "quantidade": x.get("qtde"), "localizacao": x.get("localizacao"),
            "serial": x.get("nro_serie"), "patrimonio": x.get("nro_patrimonio"),
            "filial_destino": x.get("filial_destino"), "status": x.get("status"),
        })
    return {
        "fonte": "Imobilizados atuais",
        "registros_encontrados": len(achados),
        "registros": achados[:limite],
        "resultado_limitado": len(achados) > limite,
    }


def _agente_consultar_produtos(args, role):
    termo = _agente_texto_normalizado(args.get("termo"))
    limite = _agente_limite(args.get("limite"), 30, 80)
    achados = []
    for p in db.listar_produtos():
        alvo = _agente_texto_normalizado(f"{p.get('codigo','')} {p.get('descricao','')}")
        if termo and termo not in alvo:
            continue
        item = {
            "id": p.get("id"), "codigo": p.get("codigo"), "descricao": p.get("descricao"),
            "quantidade_por_loja": p.get("qtde_por_loja"),
        }
        if role != "consulta":
            item["custo"] = p.get("custo")
        achados.append(item)
    return {
        "fonte": "Cadastro de Produtos",
        "produtos_encontrados": len(achados),
        "produtos": achados[:limite],
        "resultado_limitado": len(achados) > limite,
        "custos_visiveis": role != "consulta",
    }


def _agente_consultar_kit(args):
    termo = _agente_texto_normalizado(args.get("termo"))
    itens = []
    for k in db.listar_kit_padrao_loja():
        alvo = _agente_texto_normalizado(f"{k.get('codigo','')} {k.get('descricao','')}")
        if termo and termo not in alvo:
            continue
        itens.append({
            "codigo": k.get("codigo"), "descricao": k.get("descricao"),
            "quantidade_por_loja": int(k.get("quantidade") or 0),
        })
    return {"fonte": "Kit padrão de loja", "total_itens": len(itens), "itens": itens[:100]}


def _agente_consultar_filiais(args):
    status = _agente_texto_normalizado(args.get("status"))
    uf = str(args.get("uf") or "").strip().upper()[:2]
    termo = _agente_texto_normalizado(args.get("termo"))
    limite = _agente_limite(args.get("limite"), 40, 100)
    achados = []
    for f in db.listar_filiais(incluir_inativas=True):
        st = _status_filial_normalizado(f.get("ativo"))
        alvo = _agente_texto_normalizado(f"{f.get('codigo','')} {f.get('nome','')} {f.get('cidade','')} {f.get('uf','')}")
        if status and status not in _agente_texto_normalizado(st):
            continue
        if uf and str(f.get("uf") or "").strip().upper() != uf:
            continue
        if termo and termo not in alvo:
            continue
        achados.append({
            "id": f.get("id"), "codigo": f.get("codigo"), "nome": f.get("nome"),
            "cidade": f.get("cidade"), "uf": f.get("uf"), "bandeira": f.get("bandeira"),
            "status": st, "previsao_abertura": f.get("previsao_abertura"),
        })
    return {
        "fonte": "Cadastro de Filiais",
        "filiais_encontradas": len(achados), "filiais": achados[:limite],
        "resultado_limitado": len(achados) > limite,
    }


def _agente_consultar_projecao():
    dados = _dados_projecao_lojas()
    visao = _calcular_visao_executiva()
    return {
        "fonte": "Projeção de abertura de lojas",
        "totais": dados.get("totais"),
        "por_estado": dados.get("estados"),
        "capacidade_e_risco": visao,
    }


def _agente_consultar_acompanhamento(args):
    dados = _dados_acompanhamento_expansao()
    status = _agente_texto_normalizado(args.get("status"))
    uf = str(args.get("uf") or "").strip().upper()[:2]
    limite = _agente_limite(args.get("limite"), 40, 100)
    linhas = []
    for x in dados.get("linhas") or []:
        if status and status not in _agente_texto_normalizado(x.get("status_filial")):
            continue
        if uf and str(x.get("uf") or "").strip().upper() != uf:
            continue
        linhas.append({
            "id": x.get("id"), "filial": x.get("filial"), "bandeira": x.get("bandeira"),
            "uf": x.get("uf"), "projeto": x.get("projeto"), "status_filial": x.get("status_filial"),
            "term_obra": x.get("term_obra"), "entrada_ti": x.get("entrada_ti"),
            "inauguracao": x.get("inauguracao"), "situacao_cronograma": x.get("situacao_cronograma"),
        })
    return {
        "fonte": "Acompanhamento de Expansão",
        "resumo": dados.get("resumo"), "status": dados.get("status"), "ufs": dados.get("ufs"),
        "registros_encontrados": len(linhas), "registros": linhas[:limite],
        "resultado_limitado": len(linhas) > limite,
    }


def _agente_consultar_orcamento(role):
    if role == "consulta":
        return {"erro": "Orçamento é bloqueado para o perfil Consulta."}
    dados = _calcular_orcamento_pepi()
    return {
        "fonte": "Orçamento / pedido sugerido",
        "pepi_consolidado": dados.get("pepi_consolidado"),
        "total_previsto": dados.get("total_previsto"),
        "saldo": dados.get("saldo"),
        "percentual_comprometido": dados.get("percentual_comprometido"),
        "itens_sem_custo": dados.get("itens_sem_custo"),
        "itens_sem_cadastro": dados.get("itens_sem_cadastro"),
        "itens_para_comprar": dados.get("itens_para_comprar"),
        "total_unidades_pedido": dados.get("total_unidades_pedido"),
        "lojas_base": dados.get("lojas_base"),
        "lojas_consideradas": dados.get("lojas_consideradas"),
        "lojas_por_uf": dados.get("lojas_por_uf"),
        "orcamento_suficiente": dados.get("orcamento_suficiente"),
        "pedido_pronto": dados.get("pedido_pronto"),
        "pedido_linhas": (dados.get("pedido_linhas") or [])[:100],
    }


def _agente_consultar_movimentacoes(args, role):
    limite = _agente_limite(args.get("limite"), 20, 50)
    # Busca uma margem maior para que o filtro do perfil Consulta não reduza demais o retorno.
    linhas = db.listar_movimentacoes_recentes(min(100, limite * 3))
    if role == "consulta":
        filtradas = []
        for mov in linhas:
            texto = _agente_texto_normalizado(
                f"{mov.get('tipo','')} {mov.get('observacao','')} {mov.get('descricao','')}"
            )
            if "orcamento" in texto or "pepi" in texto:
                continue
            filtradas.append(mov)
        linhas = filtradas
    return {"fonte": "Histórico de movimentações", "movimentacoes": linhas[:limite]}


def _agente_tools(role):
    """Ferramentas no formato de tool calling compatível com Chat Completions."""
    funcoes = [
        {"name": "resumo_executivo", "description": "Obtém um resumo executivo atual do sistema, estoque e capacidade de expansão.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
        {"name": "consultar_estoque", "description": "Consulta o estoque atual agrupado por código, descrição e finalidade.", "parameters": {"type": "object", "properties": {"termo": {"type": "string", "description": "Código ou parte da descrição; vazio para todos."}, "finalidade": {"type": "string", "description": "Ex.: Expansão, Sustentação, Requalificação; vazio para todas."}, "limite": {"type": "integer", "minimum": 1, "maximum": 80}}, "additionalProperties": False}},
        {"name": "consultar_imobilizados", "description": "Pesquisa imobilizados por código, descrição, serial, patrimônio, localização ou filial.", "parameters": {"type": "object", "properties": {"termo": {"type": "string"}, "limite": {"type": "integer", "minimum": 1, "maximum": 60}}, "additionalProperties": False}},
        {"name": "consultar_produtos", "description": "Consulta o Cadastro de Produtos e quantidades por loja. Custos obedecem ao perfil do usuário.", "parameters": {"type": "object", "properties": {"termo": {"type": "string"}, "limite": {"type": "integer", "minimum": 1, "maximum": 80}}, "additionalProperties": False}},
        {"name": "consultar_kit_padrao", "description": "Consulta o kit padrão necessário para uma loja.", "parameters": {"type": "object", "properties": {"termo": {"type": "string"}}, "additionalProperties": False}},
        {"name": "consultar_filiais", "description": "Consulta filiais por status, estado ou texto.", "parameters": {"type": "object", "properties": {"status": {"type": "string", "description": "Ex.: ativa, inaugurar, pendente ou inativa."}, "uf": {"type": "string"}, "termo": {"type": "string"}, "limite": {"type": "integer", "minimum": 1, "maximum": 100}}, "additionalProperties": False}},
        {"name": "consultar_projecao", "description": "Obtém a projeção de lojas por estado e a capacidade/risco do estoque de expansão.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
        {"name": "consultar_acompanhamento_expansao", "description": "Consulta o acompanhamento e cronograma das lojas de expansão.", "parameters": {"type": "object", "properties": {"status": {"type": "string"}, "uf": {"type": "string"}, "limite": {"type": "integer", "minimum": 1, "maximum": 100}}, "additionalProperties": False}},
    ]
    if role != "consulta":
        funcoes.extend([
            {"name": "consultar_movimentacoes_recentes", "description": "Consulta as movimentações recentes do sistema/Relatórios.", "parameters": {"type": "object", "properties": {"limite": {"type": "integer", "minimum": 1, "maximum": 50}}, "additionalProperties": False}},
            {"name": "consultar_orcamento_pedido", "description": "Consulta a PEPI, o orçamento e a sugestão de pedido de compra calculada pelo sistema.", "parameters": {"type": "object", "properties": {}, "additionalProperties": False}},
        ])
    return [{"type": "function", "function": f} for f in funcoes]


def _agente_executar_tool(nome, args, role):
    args = args if isinstance(args, dict) else {}
    if nome == "resumo_executivo":
        return _agente_resumo_executivo(role)
    if nome == "consultar_estoque":
        return _agente_consultar_estoque(args)
    if nome == "consultar_imobilizados":
        return _agente_consultar_imobilizados(args)
    if nome == "consultar_produtos":
        return _agente_consultar_produtos(args, role)
    if nome == "consultar_kit_padrao":
        return _agente_consultar_kit(args)
    if nome == "consultar_filiais":
        return _agente_consultar_filiais(args)
    if nome == "consultar_projecao":
        return _agente_consultar_projecao()
    if nome == "consultar_acompanhamento_expansao":
        return _agente_consultar_acompanhamento(args)
    if nome == "consultar_movimentacoes_recentes":
        if role == "consulta":
            return {"erro": "Relatórios e histórico de movimentações são bloqueados para o perfil Consulta."}
        return _agente_consultar_movimentacoes(args, role)
    if nome == "consultar_orcamento_pedido":
        return _agente_consultar_orcamento(role)
    return {"erro": f"Ferramenta desconhecida: {nome}"}


def _agente_instrucoes(role):
    restricao = (
        "O perfil Consulta NÃO pode acessar Orçamento, Relatórios ou Gestão de Dados. "
        "Não revele custos, PEPI, pedido de compra nem qualquer dado dessas áreas."
        if role == "consulta" else
        "O usuário pode consultar Orçamento, inclusive PEPI, custos e pedido sugerido."
    )
    return f"""Você é o Agente IA do sistema Controle de Ativos / Estoque e Expansão.
Responda sempre em português do Brasil, de forma objetiva, profissional e operacional.
O usuário atual tem perfil: {role}.
{restricao}

Regras obrigatórias:
- Para perguntas sobre números, estoque, filiais, expansão, produtos, imobilizados, cronograma ou orçamento, consulte as ferramentas antes de responder.
- Os dados das ferramentas são a fonte de verdade. Não invente números nem complete dados ausentes por suposição.
- Este agente é SOMENTE LEITURA: nunca afirme que cadastrou, alterou, excluiu, aprovou ou enviou algo.
- Se o usuário pedir alteração, explique que a ação deve ser feita na aba correspondente do sistema.
- Ao falar de falta de estoque, diferencie quantidade disponível, necessidade por loja e quantidade faltante quando esses dados existirem.
- Ao falar de compra, use exclusivamente o cálculo de Orçamento/pedido sugerido do próprio sistema.
- Se um resultado vier limitado, diga que é uma amostra e ofereça um filtro mais específico.
- Não revele chaves, senhas, tokens, strings de conexão, segredos de MFA ou detalhes internos de segurança.
- Prefira respostas curtas com destaques e listas apenas quando ajudarem a leitura.
"""


def _agente_historico_seguro(historico):
    itens = []
    if not isinstance(historico, list):
        return itens
    for h in historico[-AI_MAX_HISTORY:]:
        if not isinstance(h, dict):
            continue
        role = h.get("role")
        if role not in ("user", "assistant"):
            continue
        texto = str(h.get("content") or "").strip()[:3000]
        if texto:
            itens.append({"role": role, "content": texto})
    return itens


@app.route("/api/agente-ia/status")
@login_required
def api_agente_ia_status():
    role = _agente_role()
    gemini_ok = bool(os.environ.get("GEMINI_API_KEY", "").strip())
    cf_token_ok = bool(os.environ.get("CLOUDFLARE_API_TOKEN", "").strip())
    cf_account_ok = bool(os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip())
    cloudflare_ok = cf_token_ok and cf_account_ok

    rota = []
    if gemini_ok:
        rota.append("Gemini")
    if cloudflare_ok:
        rota.append("Cloudflare")

    if gemini_ok:
        provedor, modelo = "Gemini", _gemini_model()
    elif cloudflare_ok:
        provedor, modelo = "Cloudflare", _cloudflare_model()
    else:
        provedor, modelo = "Não configurado", "-"

    return jsonify({
        "ok": True,
        "configurado": bool(rota),
        "modelo": modelo,
        "modelos": {
            "gemini": _gemini_model(),
            "cloudflare": _cloudflare_model(),
        },
        "provedor": provedor,
        "rota": rota,
        "gemini_configurado": gemini_ok,
        "cloudflare_configurado": cloudflare_ok,
        "cloudflare_token_configurado": cf_token_ok,
        "cloudflare_account_configurado": cf_account_ok,
        "fallback_ativo": len(rota) > 1,
        "modo": "somente leitura",
        "perfil": role,
        "orcamento_disponivel": role != "consulta",
    })



def _gemini_model():
    return os.environ.get("GEMINI_MODEL", GEMINI_DEFAULT_MODEL).strip() or GEMINI_DEFAULT_MODEL


def _cloudflare_model():
    return os.environ.get("CLOUDFLARE_MODEL", CLOUDFLARE_DEFAULT_MODEL).strip() or CLOUDFLARE_DEFAULT_MODEL


def _cloudflare_api_url():
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    return f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/chat/completions"


def _provider_chat(url, api_key, payload, provider_name, timeout=None):
    """Chamada HTTPS OpenAI-compatible para Gemini e Cloudflare.

    Cada provedor recebe um timeout curto para preservar tempo para o fallback.
    O limite global da conversa é controlado separadamente pela rota do agente.
    """
    corpo = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urlrequest.Request(
        url,
        data=corpo,
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "Controle-Estoque-Agente-IA/116",
        },
    )
    limite = AI_PROVIDER_TIMEOUT if timeout is None else max(1, float(timeout))
    with urlrequest.urlopen(req, timeout=limite) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _erro_http_detalhe(exc):
    try:
        bruto = exc.read().decode("utf-8", errors="replace")
        dado = json.loads(bruto)
        if isinstance(dado, dict):
            erro = dado.get("error")
            if isinstance(erro, dict):
                return str(erro.get("message") or erro.get("error") or "").strip()
            return str(erro or dado.get("message") or "").strip()
    except Exception:
        pass
    return ""


def _provider_erro_amigavel(provider, exc):
    codigo = getattr(exc, "code", None)
    detalhe = _erro_http_detalhe(exc)
    p = provider.lower()

    if codigo in (401, 403):
        if p == "cloudflare":
            return "A autenticação do Cloudflare falhou. Confira CLOUDFLARE_API_TOKEN, CLOUDFLARE_ACCOUNT_ID e a permissão Workers AI Read."
        if p == "gemini":
            return "A chave GEMINI_API_KEY não foi aceita. Gere ou copie novamente a chave no Google AI Studio."
        return f"A autenticação do {provider} falhou."

    if codigo == 429:
        return f"O limite gratuito do {provider} foi atingido temporariamente. O agente vai tentar o próximo provedor."

    if codigo in (400, 404) and ("model" in detalhe.lower() or codigo == 404):
        return f"O modelo configurado no {provider} não está disponível. Confira a variável de modelo desse provedor."

    if codigo and codigo >= 500:
        return f"O {provider} está temporariamente indisponível."

    if detalhe:
        return f"O {provider} não concluiu a consulta: {detalhe[:220]}"
    return f"Não foi possível consultar o {provider} agora."


def _agente_base_mensagens(pergunta, historico, role):
    mensagens = [{"role": "system", "content": _agente_instrucoes(role)}]
    mensagens.extend(historico)
    mensagens.append({"role": "user", "content": pergunta})
    return mensagens


def _agente_loop_openai_compat(provider, api_url, api_key, model, pergunta, historico, role, deadline=None):
    mensagens = _agente_base_mensagens(pergunta, historico, role)
    tools = _agente_tools(role)
    ferramentas_usadas = []

    for _ in range(AI_MAX_TOOL_ROUNDS):
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(f"Tempo total do Agente IA esgotado durante {provider}")
        payload = {
            "model": model,
            "messages": mensagens,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0.2,
        }
        # Gemini/Cloudflare aplicam seus limites padrão de saída para máxima compatibilidade.

        timeout_chamada = AI_PROVIDER_TIMEOUT
        if deadline is not None:
            restante = deadline - time.monotonic()
            if restante <= 1:
                raise TimeoutError(f"Sem tempo restante para consultar {provider}")
            timeout_chamada = min(AI_PROVIDER_TIMEOUT, restante)
        resposta = _provider_chat(api_url, api_key, payload, provider, timeout=timeout_chamada)
        escolhas = resposta.get("choices") or []
        if not escolhas:
            raise RuntimeError(f"{provider} não retornou escolhas")

        mensagem = (escolhas[0] or {}).get("message") or {}
        chamadas = mensagem.get("tool_calls") or []
        if not chamadas:
            texto = str(mensagem.get("content") or "").strip()
            if not texto:
                texto = "Não consegui gerar uma resposta conclusiva com os dados disponíveis."
            return {
                "ok": True,
                "resposta": texto,
                "modelo": model,
                "provedor": provider,
                "ferramentas": ferramentas_usadas,
                "fallback_usado": False,
            }

        mensagens.append({
            "role": "assistant",
            "content": mensagem.get("content"),
            "tool_calls": chamadas,
        })

        for chamada in chamadas:
            func = chamada.get("function") or {}
            nome = str(func.get("name") or "").strip()
            raw_args = func.get("arguments") or "{}"
            if isinstance(raw_args, dict):
                args = raw_args
            else:
                try:
                    args = json.loads(raw_args)
                except Exception:
                    args = {}
            if not isinstance(args, dict):
                args = {}
            resultado = _agente_executar_tool(nome, args, role)
            ferramentas_usadas.append(nome)
            mensagens.append({
                "role": "tool",
                "tool_call_id": chamada.get("id"),
                "name": nome,
                "content": json.dumps(_agente_json(resultado), ensure_ascii=False, default=str),
            })

    raise RuntimeError(f"O {provider} exigiu etapas demais para concluir a consulta")


def _provider_configs():
    configs = []

    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if gemini_key:
        configs.append({
            "nome": "Gemini",
            "url": GEMINI_API_URL,
            "key": gemini_key,
            "model": _gemini_model(),
        })

    cf_token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    cf_account = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    if cf_token and cf_account:
        configs.append({
            "nome": "Cloudflare",
            "url": _cloudflare_api_url(),
            "key": cf_token,
            "model": _cloudflare_model(),
        })

    return configs


@app.route("/api/agente-ia/chat", methods=["POST"])
@login_required
def api_agente_ia_chat():
    if not _csrf_ok():
        return jsonify({"erro": "Token de segurança inválido. Atualize a página e tente novamente."}), 400

    provedores = _provider_configs()
    if not provedores:
        return jsonify({
            "erro": "Agente IA ainda não está configurado. Configure GEMINI_API_KEY e/ou Cloudflare no Render."
        }), 503

    dados = request.get_json(silent=True) or {}
    pergunta = str(dados.get("mensagem") or "").strip()
    if not pergunta:
        return jsonify({"erro": "Digite uma pergunta para o Agente IA."}), 400
    if len(pergunta) > 4000:
        return jsonify({"erro": "A pergunta é muito longa. Limite: 4.000 caracteres."}), 400

    role = _agente_role()
    historico = _agente_historico_seguro(dados.get("historico"))
    falhas = []
    deadline = time.monotonic() + AI_TOTAL_TIMEOUT

    # Ordem fixa atual: Gemini -> Cloudflare.
    # O limite global impede que um provedor lento faça o Gunicorn encerrar
    # o worker antes que o fallback tenha chance de responder.
    for indice, cfg in enumerate(provedores):
        if time.monotonic() >= deadline:
            falhas.append("Tempo total do fallback esgotado antes do próximo provedor.")
            break
        try:
            resultado = _agente_loop_openai_compat(
                cfg["nome"], cfg["url"], cfg["key"], cfg["model"],
                pergunta, historico, role, deadline=deadline,
            )
            if indice > 0:
                resultado["fallback_usado"] = True
                anteriores = " → ".join(x["nome"] for x in provedores[:indice])
                resultado["aviso"] = f"Fallback automático: {anteriores} indisponível/limitado; resposta gerada por {cfg['nome']}."
            resultado["rota_configurada"] = [x["nome"] for x in provedores]
            return jsonify(resultado)
        except urlerror.HTTPError as e:
            msg = _provider_erro_amigavel(cfg["nome"], e)
            falhas.append(f"{cfg['nome']}: {msg}")
            print(f"[agente-ia] {cfg['nome']} HTTP {getattr(e, 'code', '?')}: {msg}")
        except (urlerror.URLError, TimeoutError) as e:
            msg = f"Não foi possível conectar ao {cfg['nome']} dentro do tempo esperado."
            falhas.append(f"{cfg['nome']}: {msg}")
            print(f"[agente-ia] {cfg['nome']} rede/timeout: {type(e).__name__}: {e}")
        except Exception as e:
            msg = f"O {cfg['nome']} não conseguiu concluir esta consulta."
            falhas.append(f"{cfg['nome']}: {msg}")
            print(f"[agente-ia] {cfg['nome']} falhou: {type(e).__name__}: {e}")

    resumo = " | ".join(falhas[-3:])
    return jsonify({
        "erro": "Todos os provedores configurados ficaram indisponíveis ou atingiram seus limites nesta consulta. " + resumo
    }), 502


# ---------------------------------------------------------------------
# Health check para provedores de hospedagem (não consulta o banco para
# evitar acordar o Neon somente por causa do monitoramento do serviço).
# ---------------------------------------------------------------------

@app.route("/health")
def health_check():
    return jsonify({"status": "ok", "build": APP_BUILD}), 200


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
