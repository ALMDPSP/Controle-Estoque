import io
import os
import unittest
from unittest.mock import patch
import test_expansao_gestao as base
import manuais

class ManuaisTest(unittest.TestCase):
    def setUp(self):
        self.client=base.sistema.app.test_client()
        with self.client.session_transaction() as s:
            s.update(user_id=1,username='teste',role='admin',site_area='expansao',ambiente_acesso='ambos',ambiente_selecionado=True,_csrf_token='token')
        with base.exp.transacao(base.db) as (_,c):
            for table in ('manuais_favoritos','manuais_versoes','manuais_documentos'): c.execute('DELETE FROM '+table)
        self.env=patch.dict(os.environ,{'SUPABASE_URL':'https://teste.supabase.co','SUPABASE_SECRET_KEY':'sb_secret_test','SUPABASE_BUCKET':'manuais-procedimentos'});self.env.start();self.addCleanup(self.env.stop)
    def upload(self,pdf=b'%PDF-1.4\n%%EOF',headers=True):
        return self.client.post('/api/manuais',data={'titulo':'Manual de rede','categoria':'Redes','versao':'1.0','arquivo':(io.BytesIO(pdf),'manual.pdf')},headers={'X-CSRF-Token':'token'} if headers else {})
    @patch('manuais.storage')
    def test_lifecycle(self,store):
        store.return_value={};r=self.upload();self.assertEqual(r.status_code,201);ident=r.json['id']
        sent=store.call_args;self.assertEqual(sent.args[0],'POST');self.assertTrue(sent.kwargs['pdf'])
        store.reset_mock();r=self.client.get('/api/manuais?busca=rede');self.assertEqual(r.json['total'],1);store.assert_not_called()
        store.return_value={'signedURL':'/object/sign/manuais-procedimentos/manual.pdf?token=test'}
        r=self.client.get('/api/manuais/'+ident);self.assertEqual(r.status_code,302);self.assertIn('teste.supabase.co/storage/v1/object/sign/',r.location);self.assertIn('no-store',r.headers['Cache-Control'])
        r=self.client.delete('/api/manuais/'+ident,json={'revisao':1},headers={'X-CSRF-Token':'token'});self.assertEqual(r.status_code,200);self.assertEqual(self.client.get('/api/manuais').json['total'],0)
    @patch('manuais.storage')
    def test_invalid_pdf_and_csrf(self,store):
        self.assertEqual(self.upload(b'not pdf').status_code,400);self.assertEqual(self.upload(headers=False).status_code,400);store.assert_not_called()
    @patch('manuais.storage')
    def test_size_limit(self,store):
        with patch('manuais.MAX_PDF',10): self.assertEqual(self.upload(b'%PDF-'+b'x'*10).status_code,400)
        store.assert_not_called()
    @patch('manuais.storage')
    def test_permissions(self,store):
        with self.client.session_transaction() as s:s['role']='consulta'
        self.assertEqual(self.upload().status_code,403)
        self.assertEqual(self.client.delete('/api/manuais/x',headers={'X-CSRF-Token':'token'}).status_code,403)
        self.assertNotIn(b'id="doc-form"',self.client.get('/manuais-procedimentos').data)
        with self.client.session_transaction() as s:s['site_area']='estoque_cd';s['role']='admin'
        self.assertEqual(self.client.get('/api/manuais').status_code,403);store.assert_not_called()
    @patch('manuais.storage',side_effect=manuais.StorageError('Falha simulada'))
    def test_failure_no_metadata(self,store):
        self.assertEqual(self.upload().status_code,502);self.assertEqual(self.client.get('/api/manuais').json['total'],0)
    @patch('manuais.storage')
    def test_pagination(self,store):
        with base.exp.transacao(base.db) as (_,c):
            for i in range(21):c.execute(base.db.q('INSERT INTO manuais_documentos(id,titulo,categoria,versao,nome,caminho,tamanho,criado_por,criado_em) VALUES(?,?,?,?,?,?,?,?,?)'),(str(i),'Manual','TI','1','a.pdf','a',10,'teste','2026-10-21T00:00:00Z'))
        self.assertEqual(len(self.client.get('/api/manuais').json['itens']),20);self.assertEqual(len(self.client.get('/api/manuais?pagina=2').json['itens']),1);store.assert_not_called()
    def test_page_and_stylesheet(self):
        for role in ('admin','gestor','operador','consulta'):
            with self.client.session_transaction() as sess: sess['role']=role
            response=self.client.get('/manuais-procedimentos')
            self.assertEqual(response.status_code,200)
            self.assertIn(b'manuais.css', response.data)
        with self.client.get('/static/manuais.css') as response: self.assertEqual(response.status_code,200)

    def test_anonymous(self):
        with self.client.session_transaction() as s:s.clear()
        self.assertEqual(self.client.get('/api/manuais').status_code,302)

    def edit(self, ident, revision=1, **updates):
        data=dict(titulo='Manual atualizado',categoria='Redes',versao='1.0',descricao='Procedimento de configuração da loja',status='vigente',revisao=revision)
        data.update(updates)
        return self.client.patch('/api/manuais/'+ident,json=data,headers={'X-CSRF-Token':'token'})

    def replace(self, ident, revision=1, version='2.0'):
        return self.client.post('/api/manuais/'+ident+'/versoes',data={'versao':version,'revisao':str(revision),'arquivo':(io.BytesIO(b'%PDF-1.4\nnovo'),'nova-versao.pdf')},headers={'X-CSRF-Token':'token'})

    @patch('manuais.storage')
    def test_edit_archive_and_revision_conflict(self,store):
        ident=self.upload().json['id'];store.reset_mock()
        self.assertEqual(self.edit(ident,status='arquivado').status_code,200)
        self.assertEqual(self.client.get('/api/manuais').json['total'],0)
        item=self.client.get('/api/manuais?status=arquivado').json['itens'][0]
        self.assertEqual(item['descricao'],'Procedimento de configuração da loja')
        self.assertEqual(item['revisao'],2)
        self.assertEqual(self.edit(ident).status_code,409)
        self.assertEqual(self.edit(ident,2,status='vigente').status_code,200)
        self.assertEqual(self.edit(ident,3,categoria='categoria arbitrária').status_code,400)
        self.assertEqual(self.edit(ident,3,descricao='a'*2001).status_code,400)
        self.assertEqual(self.client.patch('/api/manuais/'+ident,json={}).status_code,400)
        store.assert_not_called()

    @patch('manuais.storage')
    def test_version_history_replacement_and_delete_all(self,store):
        ident=self.upload().json['id']; old=self.client.get('/api/manuais/'+ident+'/versoes').json['itens'][0]
        r=self.replace(ident);self.assertEqual(r.status_code,201);new=r.json['id']
        d=self.client.get('/api/manuais/'+ident+'/versoes').json
        self.assertEqual(d['total'],2);self.assertEqual(d['arquivo_atual'],new)
        self.assertEqual(self.client.get('/api/manuais').json['itens'][0]['versao'],'2.0')
        store.reset_mock()
        self.assertEqual(self.replace(ident,2,'2.0').status_code,400)
        self.assertEqual(self.replace(ident,1,'3.0').status_code,409)
        self.assertEqual(self.edit(ident,2,versao='1.0').status_code,400)
        store.assert_not_called()
        store.return_value={'signedURL':'/object/sign/manuais-procedimentos/a.pdf?token=x'}
        self.assertEqual(self.client.get('/api/manuais/'+ident+'/versoes/'+old['id']).status_code,302)
        old_path=store.call_args.args[1];self.assertIn(old['id'],old_path)
        self.assertEqual(self.client.get('/api/manuais/other/versoes/'+old['id']).status_code,404)
        self.assertEqual(self.client.get('/api/manuais/'+ident+'/versoes/not-real').status_code,404)
        self.assertEqual(self.client.delete('/api/manuais/'+ident,json={'revisao':1},headers={'X-CSRF-Token':'token'}).status_code,409)
        r=self.client.delete('/api/manuais/'+ident,json={'revisao':2},headers={'X-CSRF-Token':'token'})
        self.assertEqual(r.status_code,200);self.assertEqual(len(store.call_args.args[2]['prefixes']),2)
        self.assertEqual(self.client.get('/api/manuais/'+ident+'/versoes/'+old['id']).status_code,404)

    @patch('manuais.storage')
    def test_replacement_race_cleans_orphan(self,store):
        ident=self.upload().json['id']
        def race(method,path,*args,**kwargs):
            if method=='POST':
                with base.exp.transacao(base.db) as (_,c):c.execute(base.db.q('UPDATE manuais_documentos SET revisao=revisao+1 WHERE id=?'),(ident,))
            return {}
        store.reset_mock();store.side_effect=race
        self.assertEqual(self.replace(ident).status_code,409)
        self.assertEqual(store.call_count,2);self.assertEqual(store.call_args.args[0],'DELETE')
        self.assertEqual(self.client.get('/api/manuais/'+ident+'/versoes').json['total'],1)
        self.assertEqual(self.client.get('/api/manuais').json['itens'][0]['versao'],'1.0')

    @patch('manuais.storage')
    def test_favorites_are_private_and_consulta_can_use_them(self,store):
        ident=self.upload().json['id'];store.reset_mock()
        with self.client.session_transaction() as s:s['role']='consulta'
        path='/api/manuais/'+ident+'/favorito'
        self.assertEqual(self.client.put(path).status_code,400)
        for _ in range(2):self.assertEqual(self.client.put(path,headers={'X-CSRF-Token':'token'}).status_code,200)
        self.assertEqual(self.client.get('/api/manuais?favoritos=1').json['total'],1)
        self.assertEqual(self.edit(ident).status_code,403)
        self.assertEqual(self.replace(ident).status_code,403)
        with self.client.session_transaction() as s:s['user_id']=2
        self.assertEqual(self.client.get('/api/manuais?favoritos=1').json['total'],0)
        with self.client.session_transaction() as s:s['user_id']=1
        self.assertEqual(self.client.delete(path,headers={'X-CSRF-Token':'token'}).status_code,200)
        self.assertEqual(self.client.get('/api/manuais?favoritos=1').json['total'],0)
        store.assert_not_called()

    @patch('manuais.storage')
    def test_filters_dates_description_sort_and_status(self,store):
        first=self.upload().json['id'];second=self.upload().json['id']
        self.assertEqual(self.edit(first,titulo='Zeta',descricao='Configuração exclusiva',status='arquivado').status_code,200)
        self.assertEqual(self.edit(second,titulo='Alfa').status_code,200)
        with base.exp.transacao(base.db) as (_,c):
            c.execute(base.db.q('UPDATE manuais_documentos SET criado_em=? WHERE id=?'),('2026-10-01T23:59:00Z',first))
            c.execute(base.db.q('UPDATE manuais_documentos SET criado_em=? WHERE id=?'),('2026-10-22T10:00:00Z',second))
        store.reset_mock()
        self.assertEqual(self.client.get('/api/manuais?busca=exclusiva&status=todos').json['total'],1)
        self.assertEqual(self.client.get('/api/manuais?status=todos&de=2026-10-01&ate=2026-10-01').json['itens'][0]['id'],first)
        self.assertEqual(self.client.get('/api/manuais?status=todos&ordem=titulo').json['itens'][0]['id'],second)
        self.assertEqual(self.client.get('/api/manuais?status=todos&ordem=antigos').json['itens'][0]['id'],first)
        for query in ('de=invalid','de=2026-10-22&ate=2026-10-01','ordem=sql','status=invalid'):
            self.assertEqual(self.client.get('/api/manuais?'+query).status_code,400)
        store.assert_not_called()

    def test_migration_preserves_existing_files_and_is_idempotent(self):
        import sqlite3,tempfile
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as folder:
            def conn():
                cn=sqlite3.connect(folder+'/legacy.db');cn.row_factory=sqlite3.Row;return cn
            legacy=SimpleNamespace(IS_PG=False,get_conn=conn,get_cursor=lambda cn:cn.cursor(),q=lambda sql:sql)
            with conn() as cn:
                cn.execute('CREATE TABLE manuais_documentos(id TEXT PRIMARY KEY,titulo TEXT NOT NULL,categoria TEXT NOT NULL,versao TEXT NOT NULL,nome TEXT NOT NULL,caminho TEXT NOT NULL,tamanho INTEGER NOT NULL,criado_por TEXT NOT NULL,criado_em TEXT NOT NULL,excluido INTEGER NOT NULL DEFAULT 0)')
                cn.execute("INSERT INTO manuais_documentos VALUES('old','Manual antigo','Personalizada','1.0','a.pdf','original/a.pdf',10,'autor','2026-10-01T00:00:00Z',0)")
            manuais.migrar(legacy);manuais.migrar(legacy)
            with conn() as cn:
                r=cn.execute('SELECT * FROM manuais_documentos').fetchone()
                self.assertEqual(r['caminho'],'original/a.pdf');self.assertEqual(r['status'],'vigente');self.assertEqual(r['arquivo_id'],'old')
                self.assertEqual(cn.execute('SELECT COUNT(*) FROM manuais_versoes').fetchone()[0],1)
                self.assertEqual(cn.execute('SELECT caminho FROM manuais_versoes').fetchone()[0],'original/a.pdf')

    @patch('manuais.storage')
    def test_oversized_multipart_returns_json(self,store):
        ident=self.upload().json['id'];store.reset_mock()
        with patch('manuais.MAX_PDF',10):
            response=self.upload(b'%PDF-'+b'x'*(2*1024*1024))
            self.assertEqual(response.status_code,413);self.assertIn('erro',response.json)
            response=self.client.post('/api/manuais/'+ident+'/versoes',data={'versao':'2.0','revisao':'1','arquivo':(io.BytesIO(b'%PDF-'+b'x'*(2*1024*1024)),'large.pdf')},headers={'X-CSRF-Token':'token'})
            self.assertEqual(response.status_code,413);self.assertIn('erro',response.json)
        store.assert_not_called()
