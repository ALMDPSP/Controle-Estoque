"""Testes isolados do código real de MFA, sem acesso ao banco de produção."""
import ast
import base64
import hashlib
import hmac
import io
import json
from pathlib import Path
import secrets
import struct
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse
from flask import Flask, request, session, redirect, url_for, render_template
import qrcode
from cryptography.fernet import Fernet

ROOT = Path(__file__).resolve().parents[1]
NAMES = {'_base32_secret', '_totp_code', '_verify_totp', '_mfa_uri', '_qr_data_uri',
         '_gerar_codigos_recuperacao', '_hash_recovery_code', 'mfa_configurar', '_mfa_no_cache'}

class MFASetupTest(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__, template_folder=str(ROOT / 'templates'))
        self.app.secret_key = 'isolated-test-key'
        self.app.config['TESTING'] = True
        self.user = dict(id=7, username='teste', role='operador', mfa_enabled='0', ambiente_acesso='expansao')
        self.db = SimpleNamespace(buscar_usuario_por_id=lambda _: self.user,
                                  registrar_evento_login=Mock(), salvar_mfa_usuario=Mock())
        self.cipher = Fernet(Fernet.generate_key())
        self.ns = dict(globals(), app=self.app, db=self.db, MFA_ISSUER='Teste',
                       _csrf_ok=lambda: request.form.get('csrf_token') == 'test-csrf',
                       _client_ip=lambda: '127.0.0.1', _mfa_wait_seconds=lambda _: 0,
                       _mfa_register_failure=Mock(), _mfa_clear_failures=Mock(),
                       _protect_mfa_secret=lambda s: self.cipher.encrypt(s.encode()).decode())
        tree = ast.parse((ROOT / 'app.py').read_text())
        selected = ast.Module(body=[n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in NAMES], type_ignores=[])
        exec(compile(selected, 'app.py', 'exec'), self.ns)
        for endpoint in ('login', 'mfa_verificar', 'pagina_seguranca', 'mfa_codigos_recuperacao'):
            self.app.add_url_rule('/' + endpoint, endpoint, lambda: '')
        self.app.context_processor(lambda: dict(csrf_token='test-csrf'))
        self.client = self.app.test_client()
        with self.client.session_transaction() as s:
            s['pending_user_id'] = 7
        self.client.get('/mfa/configurar')
        with self.client.session_transaction() as s:
            self.secret = s['mfa_setup_secret_7']
        self.payload = dict(csrf_token='test-csrf', setup_id=hashlib.sha256(self.secret.encode()).hexdigest())

    def post(self, code, **extra):
        return self.client.post('/mfa/configurar', data=dict(self.payload, codigo=code, **extra))

    def test_rfc6238_vectors(self):
        key = base64.b32encode(b'12345678901234567890').decode()
        for timestamp, expected in [(59,'94287082'),(1111111109,'07081804'),(1111111111,'14050471'),(1234567890,'89005924'),(2000000000,'69279037'),(20000000000,'65353130')]:
            self.assertEqual(self.ns['_totp_code'](key,timestamp,digits=8), expected)

    def test_activation_and_persisted_secret(self):
        code = self.ns['_totp_code'](self.secret)
        response = self.post(code[:3] + ' ' + code[3:])
        self.assertEqual(response.status_code, 302)
        args = self.db.salvar_mfa_usuario.call_args.args
        self.assertEqual(self.cipher.decrypt(args[1].encode()).decode(), self.secret)
        self.assertEqual(len(json.loads(args[2])), 8)

    def test_invalid_code_keeps_qr(self):
        self.assertEqual(self.post('x').status_code, 400)
        self.db.salvar_mfa_usuario.assert_not_called()
        with self.client.session_transaction() as s:
            self.assertEqual(s['mfa_setup_secret_7'], self.secret)

    def test_restart_rejects_old_form(self):
        self.client.post('/mfa/configurar', data=dict(csrf_token='test-csrf', acao='reiniciar'))
        self.assertEqual(self.post(self.ns['_totp_code'](self.secret)).status_code, 409)
        self.db.salvar_mfa_usuario.assert_not_called()

    def test_csrf(self):
        self.payload['csrf_token'] = 'wrong'
        self.assertEqual(self.post(self.ns['_totp_code'](self.secret)).status_code, 400)
        self.db.salvar_mfa_usuario.assert_not_called()

    def test_attempt_limit(self):
        self.ns['_mfa_wait_seconds'] = lambda _: 60
        self.assertEqual(self.post(self.ns['_totp_code'](self.secret)).status_code, 429)
        self.db.salvar_mfa_usuario.assert_not_called()

    def test_uri_and_no_cache(self):
        uri = self.ns['_mfa_uri']('teste', self.secret)
        self.assertEqual(parse_qs(urlparse(uri).query)['secret'], [self.secret])
        self.assertEqual(self.client.get('/mfa/configurar').headers['Cache-Control'], 'no-store')

if __name__ == '__main__':
    unittest.main()
