"""Manuais: metadados no banco existente e PDFs privados no Supabase."""
import http.client
import json
import os
import re
import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
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

CATEGORIAS = ('PDV Orion', 'Infraestrutura', 'Redes', 'Equipamentos', 'Sistemas', 'Segurança', 'Operação de Loja', 'Geral')

class Conflito(Exception):
    pass

def migrar(db):
    with transacao(db) as (_, c):
        c.execute("""CREATE TABLE IF NOT EXISTS manuais_documentos (
        id TEXT PRIMARY KEY, titulo TEXT NOT NULL, categoria TEXT NOT NULL,
        versao TEXT NOT NULL, nome TEXT NOT NULL, caminho TEXT NOT NULL,
        tamanho INTEGER NOT NULL, criado_por TEXT NOT NULL, criado_em TEXT NOT NULL,
        excluido INTEGER NOT NULL DEFAULT 0)""")
        additions = {'descricao': "TEXT NOT NULL DEFAULT ''", 'status': "TEXT NOT NULL DEFAULT 'vigente'",
                     'revisao': 'INTEGER NOT NULL DEFAULT 1', 'atualizado_em': "TEXT NOT NULL DEFAULT ''",
                     'atualizado_por': "TEXT NOT NULL DEFAULT ''", 'arquivo_id': "TEXT NOT NULL DEFAULT ''"}
        if not db.IS_PG:
            c.execute('PRAGMA table_info(manuais_documentos)'); cols={r['name'] for r in c.fetchall()}
        for name, definition in additions.items():
            if db.IS_PG: c.execute('ALTER TABLE manuais_documentos ADD COLUMN IF NOT EXISTS '+name+' '+definition)
            elif name not in cols: c.execute('ALTER TABLE manuais_documentos ADD COLUMN '+name+' '+definition)
        c.execute("""CREATE TABLE IF NOT EXISTS manuais_versoes (
          id TEXT PRIMARY KEY, documento_id TEXT NOT NULL, versao TEXT NOT NULL, nome TEXT NOT NULL,
          caminho TEXT NOT NULL, tamanho INTEGER NOT NULL, criado_por TEXT NOT NULL, criado_em TEXT NOT NULL)""")
        c.execute("""INSERT INTO manuais_versoes (id,documento_id,versao,nome,caminho,tamanho,criado_por,criado_em)
          SELECT d.id,d.id,d.versao,d.nome,d.caminho,d.tamanho,d.criado_por,d.criado_em FROM manuais_documentos d
          WHERE NOT EXISTS (SELECT 1 FROM manuais_versoes v WHERE v.id=d.id) AND d.arquivo_id=''""")
        c.execute("UPDATE manuais_documentos SET arquivo_id=id WHERE arquivo_id=''")
        c.execute("""CREATE TABLE IF NOT EXISTS manuais_favoritos (
          usuario TEXT NOT NULL, documento_id TEXT NOT NULL, PRIMARY KEY(usuario,documento_id))""")
        c.execute('CREATE INDEX IF NOT EXISTS idx_manuais_data ON manuais_documentos(excluido,criado_em)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_manuais_status ON manuais_documentos(excluido,status,criado_em)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_manuais_versoes ON manuais_versoes(documento_id,criado_em)')

def registrar(app, db, login_required, site_required, csrf_ok):
    def acesso(editar=True):
        if editar and session.get('role') not in ('admin', 'gestor', 'operador'):
            return jsonify(erro='Seu perfil permite apenas consultar documentos.'), 403
        if not csrf_ok(): return jsonify(erro='Sessão expirada. Atualize a página.'), 400

    def usuario(): return str(session['user_id'])
    def autor(): return session.get('username') or 'Usuário'
    def categorias(c):
        c.execute('SELECT DISTINCT categoria FROM manuais_documentos WHERE excluido=0')
        return sorted(set(CATEGORIAS) | {r['categoria'] for r in c.fetchall()})
    def ler(c, ident, lock=False):
        sql='SELECT * FROM manuais_documentos WHERE id=? AND excluido=0'
        if lock and db.IS_PG: sql+=' FOR UPDATE'
        c.execute(db.q(sql),(ident,)); r=c.fetchone()
        return dict(r) if r else None
    def conferir(r, valor):
        try: valid=int(valor)==r['revisao']
        except (ValueError,TypeError): valid=False
        if not valid: raise Conflito('O documento foi alterado por outra pessoa. Atualize a lista e tente novamente.')
    def campos(data, c):
        values=[str(data.get(k,'')).strip() for k in ('titulo','categoria','versao','descricao','status')]
        titulo, categoria, versao, descricao, status=values
        categoria=categoria or 'Geral'; versao=versao or '1.0'; status=status or 'vigente'
        if not titulo or len(titulo)>160 or len(versao)>40 or len(descricao)>2000:
            raise ValueError('Informe título (até 160 caracteres), versão (até 40) e descrição (até 2.000).')
        if categoria not in categorias(c): raise ValueError('Selecione uma categoria da lista.')
        if status not in ('vigente','arquivado'): raise ValueError('Selecione um status válido.')
        return titulo,categoria,versao,descricao,status
    def pdf():
        f=request.files.get('arquivo')
        if not f or not f.filename.lower().endswith('.pdf'): raise ValueError('Selecione um arquivo PDF.')
        f.stream.seek(0,2); size=f.stream.tell(); f.stream.seek(0)
        if not 5<=size<=MAX_PDF: raise ValueError('O PDF deve ter no máximo 50 MB e não pode estar vazio.')
        if f.stream.read(5)!=b'%PDF-': raise ValueError('O conteúdo do arquivo não é PDF.')
        f.stream.seek(0)
        return f,size,secure_filename(f.filename) or 'documento.pdf'
    def limpar(path):
        try: storage('DELETE','/object/'+quote(config()[2],safe=''),{'prefixes':[path]})
        except StorageError: app.logger.warning('Falha ao limpar upload de manual sem cadastro.')
    def inserir_versao(c, vid, ident, versao, nome, path, size, when):
        c.execute(db.q('INSERT INTO manuais_versoes (id,documento_id,versao,nome,caminho,tamanho,criado_por,criado_em) VALUES (?,?,?,?,?,?,?,?)'),(vid,ident,versao,nome,path,size,autor(),when))
    def pagina():
        try: return max(1,int(request.args.get('pagina',1)))
        except ValueError: return 1
    def duplicada(c, ident, versao, arquivo_id=''):
        c.execute(db.q('SELECT id FROM manuais_versoes WHERE documento_id=? AND LOWER(versao)=? AND id<>?'),(ident,versao.lower(),arquivo_id))
        if c.fetchone(): raise ValueError('Essa versão já existe. Informe uma versão diferente.')

    @app.errorhandler(StorageError)
    def storage_error(exc): return jsonify(erro=str(exc)), 502
    @app.errorhandler(Conflito)
    def conflict_error(exc): return jsonify(erro=str(exc)),409

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
            where=['d.excluido=0']; params=[usuario()]
            busca=request.args.get('busca','').strip()[:150]
            if busca:
                where.append('(LOWER(d.titulo) LIKE ? OR LOWER(d.nome) LIKE ? OR LOWER(d.descricao) LIKE ?)'); params+=['%'+busca.lower()+'%']*3
            if request.args.get('categoria'): where.append('d.categoria=?'); params.append(request.args['categoria'][:80])
            st=request.args.get('status','vigente')
            if st not in ('vigente','arquivado','todos'): return jsonify(erro='Status inválido.'),400
            if st!='todos': where.append('d.status=?'); params.append(st)
            try:
                for arg, op in [('de','>='),('ate','<')]:
                    value=request.args.get(arg,'')
                    if value:
                        dt=date.fromisoformat(value)
                        if arg=='ate': dt+=timedelta(days=1)
                        boundary=datetime.combine(dt,time.min,ZoneInfo('America/Sao_Paulo')).astimezone(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
                        where.append('d.criado_em'+op+'?'); params.append(boundary)
                if request.args.get('de') and request.args.get('ate') and request.args['de']>request.args['ate']: raise ValueError()
            except (ValueError,OverflowError): return jsonify(erro='Informe um período de publicação válido.'),400
            if request.args.get('favoritos')=='1': where.append('f.documento_id IS NOT NULL')
            orders={'recentes':'d.criado_em DESC,d.id','antigos':'d.criado_em ASC,d.id','titulo':'LOWER(d.titulo),d.id','atualizados':'d.atualizado_em DESC,d.id'}
            order=orders.get(request.args.get('ordem','recentes'))
            if not order: return jsonify(erro='Ordenação inválida.'),400
            source=' FROM manuais_documentos d LEFT JOIN manuais_favoritos f ON f.documento_id=d.id AND f.usuario=? WHERE '+' AND '.join(where)
            with transacao(db) as (_,c):
                c.execute(db.q('SELECT COUNT(*) AS n'+source),tuple(params)); total=c.fetchone()['n']
                pg=min(pagina(),max(1,(total+19)//20))
                c.execute(db.q('SELECT d.id,d.titulo,d.categoria,d.versao,d.descricao,d.status,d.revisao,d.nome,d.tamanho,d.criado_por,d.criado_em,d.atualizado_por,d.atualizado_em,CASE WHEN f.documento_id IS NULL THEN 0 ELSE 1 END AS favorito'+source+' ORDER BY '+order+' LIMIT 20 OFFSET ?'),tuple(params+[(pg-1)*20])); items=[dict(r) for r in c.fetchall()]
                cats=categorias(c)
            try: config(); configurado=True
            except StorageError: configurado=False
            return jsonify(itens=items,total=total,pagina=pg,paginas=max(1,(total+19)//20),categorias=cats,configurado=configurado)
        request.max_content_length=MAX_PDF+1024*1024
        try:
            err=acesso()
            if err:return err
            with transacao(db) as (_,c): titulo,categoria,versao,descricao,st=campos(request.form,c)
            f,size,nome=pdf()
        except ValueError as exc:return jsonify(erro=str(exc)),400
        except RequestEntityTooLarge:return jsonify(erro='O PDF deve ter no máximo 50 MB.'),413
        ident=uuid.uuid4().hex; vid=uuid.uuid4().hex; path=ident+'/'+vid+'/'+nome; when=agora()
        storage('POST','/object'+object_path(path),f.stream,size,pdf=True)
        try:
            with transacao(db) as (_,c):
                c.execute(db.q('INSERT INTO manuais_documentos (id,titulo,categoria,versao,nome,caminho,tamanho,criado_por,criado_em,descricao,status,arquivo_id,atualizado_por,atualizado_em) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)'),(ident,titulo,categoria,versao,nome,path,size,autor(),when,descricao,st,vid,autor(),when))
                inserir_versao(c,vid,ident,versao,nome,path,size,when)
        except Exception:
            limpar(path);raise
        return jsonify(ok=True,id=ident),201

    @app.route('/api/manuais/<ident>', methods=['GET','PATCH','DELETE'])
    @login_required
    @site_required('expansao')
    def api_manual(ident):
        if request.method=='GET': return abrir(ident)
        err=acesso()
        if err:return err
        data=request.get_json(silent=True) or {}
        try:
            with transacao(db,True) as (_,c):
                r=ler(c,ident,True)
                if not r:return jsonify(erro='Documento não encontrado.'),404
                conferir(r,data.get('revisao'))
                if request.method=='DELETE':
                    c.execute(db.q('SELECT caminho FROM manuais_versoes WHERE documento_id=?'),(ident,)); paths={v['caminho'] for v in c.fetchall()}|{r['caminho']}
                    storage('DELETE','/object/'+quote(config()[2],safe=''),{'prefixes':sorted(paths)})
                    c.execute(db.q('UPDATE manuais_documentos SET excluido=1,revisao=revisao+1 WHERE id=?'),(ident,))
                    c.execute(db.q('DELETE FROM manuais_favoritos WHERE documento_id=?'),(ident,))
                else:
                    titulo,categoria,versao,descricao,st=campos(data,c)
                    duplicada(c,ident,versao,r['arquivo_id'])
                    c.execute(db.q('UPDATE manuais_documentos SET titulo=?,categoria=?,versao=?,descricao=?,status=?,atualizado_por=?,atualizado_em=?,revisao=revisao+1 WHERE id=?'),(titulo,categoria,versao,descricao,st,autor(),agora(),ident))
                    c.execute(db.q('UPDATE manuais_versoes SET versao=? WHERE id=?'),(versao,r['arquivo_id']))
        except ValueError as exc:return jsonify(erro=str(exc)),400
        return jsonify(ok=True)

    @app.route('/api/manuais/<ident>/favorito',methods=['PUT','DELETE'])
    @login_required
    @site_required('expansao')
    def api_manual_favorito(ident):
        err=acesso(False)
        if err:return err
        with transacao(db,True) as (_,c):
            if not ler(c,ident,True):return jsonify(erro='Documento não encontrado.'),404
            if request.method=='PUT':
                c.execute(db.q('INSERT INTO manuais_favoritos(usuario,documento_id) VALUES(?,?) ON CONFLICT(usuario,documento_id) DO NOTHING'),(usuario(),ident))
            else:c.execute(db.q('DELETE FROM manuais_favoritos WHERE usuario=? AND documento_id=?'),(usuario(),ident))
        return jsonify(ok=True)

    @app.route('/api/manuais/<ident>/versoes',methods=['GET','POST'])
    @login_required
    @site_required('expansao')
    def api_manual_versoes(ident):
        if request.method=='GET':
            with transacao(db) as (_,c):
                r=ler(c,ident)
                if not r:return jsonify(erro='Documento não encontrado.'),404
                c.execute(db.q('SELECT COUNT(*) AS n FROM manuais_versoes WHERE documento_id=?'),(ident,)); total=c.fetchone()['n']; pg=min(pagina(),max(1,(total+19)//20))
                c.execute(db.q('SELECT id,versao,nome,tamanho,criado_por,criado_em FROM manuais_versoes WHERE documento_id=? ORDER BY criado_em DESC,id LIMIT 20 OFFSET ?'),(ident,(pg-1)*20)); items=[dict(v) for v in c.fetchall()]
            return jsonify(itens=items,arquivo_atual=r['arquivo_id'],status=r['status'],total=total,pagina=pg,paginas=max(1,(total+19)//20))
        request.max_content_length=MAX_PDF+1024*1024
        try:
            err=acesso()
            if err:return err
            f,size,nome=pdf(); versao=request.form.get('versao','').strip(); revision=request.form.get('revisao')
            if not versao or len(versao)>40:raise ValueError('Informe uma versão de até 40 caracteres.')
            with transacao(db) as (_,c):
                r=ler(c,ident)
                if not r:return jsonify(erro='Documento não encontrado.'),404
                conferir(r,revision);duplicada(c,ident,versao)
        except ValueError as exc:return jsonify(erro=str(exc)),400
        except RequestEntityTooLarge:return jsonify(erro='O PDF deve ter no máximo 50 MB.'),413
        vid=uuid.uuid4().hex; path=ident+'/'+vid+'/'+nome; when=agora()
        storage('POST','/object'+object_path(path),f.stream,size,pdf=True)
        try:
            with transacao(db,True) as (_,c):
                r=ler(c,ident,True)
                if not r:raise Conflito('O documento foi excluído. Atualize a lista.')
                conferir(r,revision);duplicada(c,ident,versao)
                inserir_versao(c,vid,ident,versao,nome,path,size,when)
                c.execute(db.q('UPDATE manuais_documentos SET versao=?,nome=?,caminho=?,tamanho=?,criado_por=?,criado_em=?,arquivo_id=?,atualizado_por=?,atualizado_em=?,revisao=revisao+1 WHERE id=?'),(versao,nome,path,size,autor(),when,vid,autor(),when,ident))
        except Exception:
            limpar(path);raise
        return jsonify(ok=True,id=vid),201

    def abrir(ident, vid=None):
        with transacao(db) as (_,c):
            r=ler(c,ident)
            if not r:return jsonify(erro='Documento não encontrado.'),404
            if vid:
                c.execute(db.q('SELECT * FROM manuais_versoes WHERE id=? AND documento_id=?'),(vid,ident)); r=c.fetchone()
                if not r:return jsonify(erro='Versão não encontrada.'),404
        signed=storage('POST','/object/sign'+object_path(r['caminho']),{'expiresIn':300})
        path=signed.get('signedURL') or signed.get('signedUrl')
        if not path or not path.startswith('/object/sign/'):raise StorageError('O armazenamento não retornou um link válido.')
        target=config()[0]+'/storage/v1'+path
        if request.args.get('baixar')=='1':target+='&download='+quote(r['nome'],safe='')
        response=redirect(target);response.headers['Cache-Control']='no-store';response.headers['Referrer-Policy']='no-referrer'
        return response

    @app.route('/api/manuais/<ident>/versoes/<vid>')
    @login_required
    @site_required('expansao')
    def api_manual_versao(ident,vid):return abrir(ident,vid)
