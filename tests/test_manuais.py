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
        with base.exp.transacao(base.db) as (_,c):c.execute('DELETE FROM manuais_documentos')
        self.env=patch.dict(os.environ,{'SUPABASE_URL':'https://teste.supabase.co','SUPABASE_SECRET_KEY':'sb_secret_test','SUPABASE_BUCKET':'manuais-procedimentos'});self.env.start();self.addCleanup(self.env.stop)
    def upload(self,pdf=b'%PDF-1.4\n%%EOF',headers=True):
        return self.client.post('/api/manuais',data={'titulo':'Manual de rede','categoria':'Rede','versao':'1.0','arquivo':(io.BytesIO(pdf),'manual.pdf')},headers={'X-CSRF-Token':'token'} if headers else {})
    @patch('manuais.storage')
    def test_lifecycle(self,store):
        store.return_value={};r=self.upload();self.assertEqual(r.status_code,201);ident=r.json['id']
        sent=store.call_args;self.assertEqual(sent.args[0],'POST');self.assertTrue(sent.kwargs['pdf'])
        store.reset_mock();r=self.client.get('/api/manuais?busca=rede');self.assertEqual(r.json['total'],1);store.assert_not_called()
        store.return_value={'signedURL':'/object/sign/manuais-procedimentos/manual.pdf?token=test'}
        r=self.client.get('/api/manuais/'+ident);self.assertEqual(r.status_code,302);self.assertIn('teste.supabase.co/storage/v1/object/sign/',r.location);self.assertIn('no-store',r.headers['Cache-Control'])
        r=self.client.delete('/api/manuais/'+ident,headers={'X-CSRF-Token':'token'});self.assertEqual(r.status_code,200);self.assertEqual(self.client.get('/api/manuais').json['total'],0)
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
    def test_anonymous(self):
        with self.client.session_transaction() as s:s.clear()
        self.assertEqual(self.client.get('/api/manuais').status_code,302)
