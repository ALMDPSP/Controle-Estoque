"""Checklist sem persistência de preenchimento; Storage simulado, nunca produção."""
import copy
import os
import unittest
from unittest.mock import patch
import test_expansao_gestao as base
import checklists
import manuais

class ChecklistTest(unittest.TestCase):
    def setUp(self):
        self.client=base.sistema.app.test_client()
        with self.client.session_transaction() as s:
            s.update(user_id=1,username='usuario-autenticado',role='admin',site_area='expansao',ambiente_acesso='ambos',ambiente_selecionado=True,_csrf_token='token')
        self.headers={'X-CSRF-Token':'token'}
        with base.exp.transacao(base.db) as (_,c):
            c.execute('DELETE FROM acompanhamento_expansao')
            for filial,status in [('CK001','PENDENTE'),('CK002','INAUGURADA')]:
                c.execute(base.db.q('INSERT INTO acompanhamento_expansao(filial,descricao_filial,bandeira,uf,status_filial,inauguracao) VALUES(?,?,?,?,?,?)'),(filial,'Loja de teste','DSP','SP',status,'30/10/2026'))
        self.env=patch.dict(os.environ,{'SUPABASE_URL':'https://teste.supabase.co','SUPABASE_SECRET_KEY':'sb_secret_test','SUPABASE_BUCKET':'manuais-procedimentos'});self.env.start();self.addCleanup(self.env.stop)

    def data(self,final=False):
        d={'filial':'CK001','responsavel':'Equipe Field','pdvs':['01','02'],'complementares':False,'modo':'final' if final else 'parcial','observacao':'Validação da inauguração','itens':{}}
        if final:
            for g in checklists.grupos(['01','02'],False):
                for item in g['itens']:d['itens'][item['id']]={'resultado':'conforme','observacao':'Validado em bancada'}
        return d

    @patch('manuais.storage')
    def test_page_model_and_no_remote_load(self,store):
        r=self.client.get('/checklist-inauguracao');self.assertEqual(r.status_code,200)
        self.assertIn(b'ALM - Expans',r.data)
        d=self.client.get('/api/checklists/lojas').json
        self.assertTrue(d['lojas'][0]['aberta']);self.assertFalse(d['lojas'][1]['aberta'])
        self.assertEqual(len(d['modelo']['pdv']),4);self.assertEqual(len(d['modelo']['balcao']),10);self.assertEqual(len(d['modelo']['servidor']),5)
        store.assert_not_called()

    @patch('manuais.storage')
    def test_pdf_upload_and_download_without_database_writes(self,store):
        before={t:None for t in ('acompanhamento_expansao','manuais_documentos','manuais_versoes')}
        with base.exp.transacao(base.db) as (_,c):
            for t in before:c.execute('SELECT COUNT(*) AS n FROM '+t);before[t]=c.fetchone()['n']
            c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name LIKE '%checklist%'");self.assertEqual(c.fetchall(),[])
        captured=[]
        def upload(method,path,body,*args,**kwargs):
            self.assertEqual(method,'POST');self.assertIn('/checklists/CK001-',path);self.assertTrue(kwargs['pdf'])
            captured.append(body.read());return {}
        store.side_effect=upload
        r=self.client.post('/api/checklists/pdf',json=self.data(True),headers=self.headers)
        self.assertEqual(r.status_code,200);self.assertEqual(r.headers['X-Checklist-Saved'],'1');self.assertTrue(r.data.startswith(b'%PDF-'))
        self.assertEqual(captured[0],r.data);self.assertIn('final',r.headers['Content-Disposition']);r.close()
        with base.exp.transacao(base.db) as (_,c):
            for t,n in before.items():c.execute('SELECT COUNT(*) AS n FROM '+t);self.assertEqual(c.fetchone()['n'],n)

    @patch('manuais.storage',side_effect=manuais.StorageError('Falha simulada'))
    def test_storage_failure_still_returns_pdf(self,store):
        with self.client.post('/api/checklists/pdf',json=self.data(),headers=self.headers) as r:
            self.assertEqual(r.status_code,200);self.assertEqual(r.headers['X-Checklist-Saved'],'0');self.assertTrue(r.data.startswith(b'%PDF-'))

    @patch('manuais.storage')
    def test_final_and_justification_validation(self,store):
        d=self.data();d['modo']='final'
        self.assertEqual(self.client.post('/api/checklists/pdf',json=d,headers=self.headers).status_code,400)
        d=self.data(True);d['itens']['pdv-01-0']={'resultado':'falha','observacao':'Integração não respondeu'}
        self.assertEqual(self.client.post('/api/checklists/pdf',json=d,headers=self.headers).status_code,400)
        d['modo']='parcial';d['itens']['pdv-01-0']['observacao']=''
        self.assertEqual(self.client.post('/api/checklists/pdf',json=d,headers=self.headers).status_code,400)
        d['itens']['pdv-01-0']={'resultado':'na','observacao':''}
        self.assertEqual(self.client.post('/api/checklists/pdf',json=d,headers=self.headers).status_code,400)
        store.assert_not_called()
        d['itens']['pdv-01-0']['observacao']='PDV sem esta função'
        with self.client.post('/api/checklists/pdf',json=d,headers=self.headers) as r:self.assertEqual(r.status_code,200)

    @patch('manuais.storage')
    def test_invalid_schema_unknown_items_and_store(self,store):
        for updates in ({'pdvs':['01','1']},{'pdvs':['0']},{'pdvs':['../']},{'responsavel':''},{'complementares':'true'},{'itens':{'nao-existe':{}}},{'itens':{'pdv-01-0':{'resultado':[]}}}):
            d=self.data();d.update(updates)
            self.assertEqual(self.client.post('/api/checklists/pdf',json=d,headers=self.headers).status_code,400)
        d=self.data();d['filial']='inexistente'
        self.assertEqual(self.client.post('/api/checklists/pdf',json=d,headers=self.headers).status_code,404)
        store.assert_not_called()

    @patch('manuais.storage')
    def test_permissions_csrf_and_site(self,store):
        self.assertEqual(self.client.post('/api/checklists/pdf',json=self.data()).status_code,400)
        with self.client.session_transaction() as s:s['role']='consulta'
        self.assertEqual(self.client.post('/api/checklists/pdf',json=self.data(),headers=self.headers).status_code,403)
        self.assertNotIn(b'id="ck-form"',self.client.get('/checklist-inauguracao').data)
        with self.client.session_transaction() as s:s['role']='admin';s['site_area']='estoque_cd'
        self.assertEqual(self.client.get('/api/checklists/lojas').status_code,403)
        self.assertEqual(self.client.get('/api/checklists/relatorios?filial=CK001').status_code,403)
        with self.client.session_transaction() as s:s.clear()
        self.assertEqual(self.client.get('/api/checklists/lojas').status_code,302)
        store.assert_not_called()

    @patch('manuais.storage')
    def test_reports_listing_and_signed_link_are_confined(self,store):
        filename='20261024-120000-final-123456abcdef.pdf'
        store.return_value=[{'name':checklists.pasta('CK001')+'/'+filename,'created_at':'2026-10-24T15:00:00Z','metadata':{'size':1000}},{'name':'../../manual.pdf'}]
        r=self.client.get('/api/checklists/relatorios?filial=CK001');self.assertEqual(r.status_code,200)
        self.assertEqual(len(r.json['itens']),1);self.assertEqual(r.json['itens'][0]['tipo'],'final')
        self.assertEqual(store.call_args.args[2]['prefix'],checklists.pasta('CK001'))
        store.return_value={'signedURL':'/object/sign/manuais-procedimentos/checklists/a.pdf?token=t'}
        r=self.client.get('/api/checklists/relatorios/abrir',query_string={'filial':'CK001','arquivo':filename,'baixar':'1'})
        self.assertEqual(r.status_code,302);self.assertIn('download=Checklist-',r.location)
        self.assertIn(checklists.pasta('CK001'),store.call_args.args[1]);self.assertIn('no-store',r.headers['Cache-Control'])
        store.reset_mock()
        for name in ('../../manual.pdf','a.pdf','/checklists/'+filename):
            self.assertEqual(self.client.get('/api/checklists/relatorios/abrir',query_string={'filial':'CK001','arquivo':name}).status_code,400)
        store.assert_not_called()

    @patch('manuais.storage')
    def test_reports_pagination(self,store):
        store.return_value=[{'name':f'20261024-120000-parcial-{n:012x}.pdf'} for n in range(21)]
        r=self.client.get('/api/checklists/relatorios?filial=CK001&pagina=2')
        self.assertEqual(len(r.json['itens']),20);self.assertTrue(r.json['proxima']);self.assertEqual(store.call_args.args[2]['offset'],20)

    def test_template_counts_and_optional_tests(self):
        default=checklists.grupos(['01'],False);extended=checklists.grupos(['01'],True)
        self.assertEqual(sum(len(g['itens']) for g in default),19)
        self.assertEqual(sum(len(g['itens']) for g in extended),27)
        self.assertEqual(len(checklists.grupos(['01','02'],False)[1]['itens']),4)

    @patch('manuais.storage')
    def test_delete_report_requires_role_csrf_and_scoped_filename(self,store):
        filename='20261024-120000-final-123456abcdef.pdf'
        path='/api/checklists/relatorios?filial=CK001&arquivo='+filename
        self.assertEqual(self.client.delete(path).status_code,400)
        for role in ('consulta',):
            with self.client.session_transaction() as s:s['role']=role
            self.assertEqual(self.client.delete(path,headers=self.headers).status_code,403)
        with self.client.session_transaction() as s:s['role']='admin'
        self.assertEqual(self.client.delete('/api/checklists/relatorios?filial=CK001&arquivo=../../manual.pdf',headers=self.headers).status_code,400)
        store.assert_not_called()
        for role in ('admin','gestor','operador'):
            with self.client.session_transaction() as s:s['role']=role
            self.assertEqual(self.client.delete(path,headers=self.headers).status_code,200)
            self.assertEqual(store.call_args.args,('DELETE','/object/manuais-procedimentos',{'prefixes':[checklists.pasta('CK001')+'/'+filename]}))
        with self.client.session_transaction() as s:s['site_area']='estoque_cd'
        store.reset_mock();self.assertEqual(self.client.delete(path,headers=self.headers).status_code,403);store.assert_not_called()

    @patch('manuais.storage',side_effect=manuais.StorageError('Falha simulada'))
    def test_delete_failure_is_not_reported_as_success(self,store):
        r=self.client.delete('/api/checklists/relatorios?filial=CK001&arquivo=20261024-120000-final-123456abcdef.pdf',headers=self.headers)
        self.assertEqual(r.status_code,502);self.assertIn('erro',r.json)
