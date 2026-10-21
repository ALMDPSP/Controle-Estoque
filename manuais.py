"""Manuais: metadados no banco existente e PDFs privados no Supabase."""
import http.client
import json
import os
import re
import uuid
from urllib.parse import quote, urlsplit
from flask import jsonify, request, session, render_template, redirect
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename
from expansao_gestao import transacao, agora

MAX_PDF = 50 * 1024 * 1024

class StorageError(Exception):
    pass

def config():
    url = os.environ.get('SUPABASE_URL', '').strip().rstrip('/')
    key = os.environ.get('SUPABASE_SECRET_KEY', '').strip()
    bucket = os.environ.get('SUPABASE_BUCKET', 'manuais-procedimentos').strip()
    if not re.fullmatch(r'https://[a-z0-9-]+\.supabase\.co', url) or not key.startswith('sb_secret_') or not re.fullmatch(r'[a-z0-9-]+', bucket):
        raise StorageError('Configure SUPABASE_URL, SUPABASE_SECRET_KEY e SUPABASE_BUCKET no servidor.')
    return url, key, bucket

def storage(method, path, body=None, size=None, pdf=False):
    url, key, _ = config()
    headers = {'apikey': key, 'Content-Type': 'application/pdf' if pdf else 'application/json'}
    if size is not None: headers['Content-Length'] = str(size)
    elif body is not None:
        body = json.dumps(body).encode(); headers['Content-Length'] = str(len(body))
    conn = http.client.HTTPSConnection(urlsplit(url).hostname, timeout=40)
    try:
        conn.request(method, '/storage/v1' + path, body=body, headers=headers)
        response = conn.getresponse()
        raw = response.read(1024 * 1024)
        if response.status >= 400:
            raise StorageError('Não foi possível acessar o armazenamento (HTTP %s). Confira a configuração e tente novamente.' % response.status)
        return json.loads(raw) if raw else {}
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise StorageError('Falha na comunicação com o armazenamento. Tente novamente.') from exc
    finally: conn.close()

def object_path(path):
    return '/' + quote(config()[2], safe='') + '/' + quote(path, safe='/')

def migrar(db):
    with transacao(db) as (_, c):
        c.execute('''CREATE TABLE IF NOT EXISTS manuais_documentos (
        id TEXT PRIMARY KEY, titulo TEXT NOT NULL, categoria TEXT NOT NULL,
        versao TEXT NOT NULL, nome TEXT NOT NULL, caminho TEXT NOT NULL,
        tamanho INTEGER NOT NULL, criado_por TEXT NOT NULL, criado_em TEXT NOT NULL,
        excluido INTEGER NOT NULL DEFAULT 0)''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_manuais_data ON manuais_documentos(excluido,criado_em)')

def registrar(app, db, login_required, site_required, csrf_ok):
    def acesso():
        if session.get('role') not in ('admin', 'gestor', 'operador'):
            return jsonify(erro='Seu perfil permite apenas consultar documentos.'), 403
        if not csrf_ok(): return jsonify(erro='Sessão expirada. Atualize a página.'), 400

    @app.errorhandler(StorageError)
    def storage_error(exc): return jsonify(erro=str(exc)), 502

    @app.route('/manuais-procedimentos')
    @login_required
    @site_required('expansao')
    def pagina_manuais():
        return render_template('manuais.html', role=session.get('role'), is_admin=session.get('role')=='admin', username=session.get('username'))

    @app.route('/api/manuais', methods=['GET','POST'])
    @login_required
    @site_required('expansao')
    def api_manuais():
        if request.method == 'GET':
            busca = request.args.get('busca','').strip()[:150]
            categoria = request.args.get('categoria','').strip()[:80]
            try: page=max(1,int(request.args.get('pagina',1)))
            except ValueError: page=1
            where='excluido=0'; params=[]
            if busca:
                where+=' AND (LOWER(titulo) LIKE ? OR LOWER(nome) LIKE ?)'; params += ['%'+busca.lower()+'%']*2
            if categoria: where+=' AND categoria=?'; params.append(categoria)
            with transacao(db) as (_,c):
                c.execute(db.q('SELECT COUNT(*) AS n FROM manuais_documentos WHERE '+where),tuple(params)); total=c.fetchone()['n']
                page=min(page,max(1,(total+19)//20))
                c.execute(db.q('SELECT id,titulo,categoria,versao,nome,tamanho,criado_por,criado_em FROM manuais_documentos WHERE '+where+' ORDER BY criado_em DESC,id LIMIT 20 OFFSET ?'),tuple(params+[(page-1)*20])); items=[dict(r) for r in c.fetchall()]
                c.execute('SELECT DISTINCT categoria FROM manuais_documentos WHERE excluido=0 ORDER BY categoria'); categorias=[r['categoria'] for r in c.fetchall()]
            try: config(); configurado=True
            except StorageError: configurado=False
            return jsonify(itens=items,total=total,pagina=page,paginas=max(1,(total+19)//20),categorias=categorias,configurado=configurado)
        # Limit applied before parsing multipart data; other app uploads are unaffected.
        request.max_content_length=MAX_PDF+1024*1024
        try:
            err=acesso()
            if err: return err
            f=request.files.get('arquivo')
            titulo=request.form.get('titulo','').strip()
            categoria=request.form.get('categoria','').strip() or 'Geral'
            versao=request.form.get('versao','').strip() or '1.0'
            if not titulo or len(titulo)>160 or len(categoria)>80 or len(versao)>40:
                return jsonify(erro='Informe título (até 160 caracteres), categoria (até 80) e versão (até 40).'),400
            if not f or not f.filename.lower().endswith('.pdf'):
                return jsonify(erro='Selecione um arquivo PDF.'),400
            f.stream.seek(0,2); size=f.stream.tell(); f.stream.seek(0)
            if not 5<=size<=MAX_PDF: return jsonify(erro='O PDF deve ter no máximo 50 MB e não pode estar vazio.'),400
            if f.stream.read(5)!=b'%PDF-': return jsonify(erro='O conteúdo do arquivo não é PDF.'),400
            f.stream.seek(0)
        except RequestEntityTooLarge:
            return jsonify(erro='O PDF deve ter no máximo 50 MB.'),413
        ident=uuid.uuid4().hex; nome=secure_filename(f.filename) or 'documento.pdf'; path=ident+'/'+nome
        storage('POST','/object'+object_path(path),f.stream,size,pdf=True)
        try:
            with transacao(db) as (_,c):
                c.execute(db.q('INSERT INTO manuais_documentos (id,titulo,categoria,versao,nome,caminho,tamanho,criado_por,criado_em) VALUES (?,?,?,?,?,?,?,?,?)'),(ident,titulo,categoria,versao,nome,path,size,session.get('username') or 'Usuário',agora()))
        except Exception:
            try: storage('DELETE','/object/'+quote(config()[2],safe=''),{'prefixes':[path]})
            except StorageError: app.logger.warning('Falha ao limpar upload de manual sem cadastro: %s',ident)
            raise
        return jsonify(ok=True,id=ident),201

    @app.route('/api/manuais/<ident>', methods=['GET','DELETE'])
    @login_required
    @site_required('expansao')
    def api_manual(ident):
        if request.method=='DELETE':
            err=acesso()
            if err:return err
        with transacao(db) as (_,c):
            c.execute(db.q('SELECT * FROM manuais_documentos WHERE id=? AND excluido=0'),(ident,)); r=c.fetchone()
        if not r:return jsonify(erro='Documento não encontrado.'),404
        if request.method=='DELETE':
            storage('DELETE','/object/'+quote(config()[2],safe=''),{'prefixes':[r['caminho']]})
            with transacao(db) as (_,c): c.execute(db.q('UPDATE manuais_documentos SET excluido=1 WHERE id=?'),(ident,))
            return jsonify(ok=True)
        signed=storage('POST','/object/sign'+object_path(r['caminho']),{'expiresIn':300})
        path=signed.get('signedURL') or signed.get('signedUrl')
        if not path or not path.startswith('/object/sign/'):
            raise StorageError('O armazenamento não retornou um link válido.')
        target=config()[0]+'/storage/v1'+path
        if request.args.get('baixar')=='1': target+='&download='+quote(r['nome'],safe='')
        response=redirect(target);response.headers['Cache-Control']='no-store';response.headers['Referrer-Policy']='no-referrer'
        return response
