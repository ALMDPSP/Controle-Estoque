"""Planejamento operacional da Expansão; sem conexão externa na importação."""
import io
import json
import uuid
import unicodedata
from datetime import datetime, date
from zoneinfo import ZoneInfo
from contextlib import contextmanager
from flask import request, jsonify, session, send_file

ETAPAS = ('Rede', 'Equipamentos', 'Expedição', 'Instalação', 'Validação')
CAMPOS_HIST = ('filial','bandeira','descricao_filial','uf','projeto','status_filial','link_rede','mobiliario','enviada','em_separacao','equip_separado','term_obra','entrada_ti','inauguracao','observacao_ti')

def norm(v):
    return ''.join(c for c in unicodedata.normalize('NFD', str(v or '')) if not unicodedata.combining(c)).strip().lower()

def data(v):
    for fmt in ('%Y-%m-%d','%d/%m/%Y','%d-%m-%Y','%d/%m/%y','%d-%m-%y'):
        try: return datetime.strptime(str(v or '').strip(), fmt).date()
        except ValueError: pass
    return None

def hoje():
    return datetime.now(ZoneInfo('America/Sao_Paulo')).date()

def agora():
    return datetime.now(ZoneInfo('UTC')).isoformat(timespec='seconds').replace('+00:00','Z')

def qtd(v):
    try: return max(0, int(float(v or 0)))
    except (ValueError, TypeError): return 0

def aberto(r):
    return norm(r.get('status_filial')) in ('pendente','em andamento','obra nao iniciada')

def filtrar(linhas, filtros):
    result=[]
    for r in linhas:
        if any(filtros.get(k) and str(r.get(c) or '') != filtros[k] for k,c in [('status','status_filial'),('projeto','projeto'),('bandeira','bandeira'),('uf','uf')]): continue
        mes=filtros.get('mes',''); dt=data(r.get('inauguracao'))
        if mes and (dt.strftime('%Y-%m') if dt else 'sem-data') != mes: continue
        busca=norm(filtros.get('busca'))
        if busca and busca not in norm(' '.join(str(r.get(k) or '') for k in ('filial','descricao_filial','observacao_ti','projeto','status_filial'))): continue
        result.append(r)
    return result

@contextmanager
def transacao(db, lock=False):
    conn=db.get_conn(); cur=db.get_cursor(conn)
    try:
        if not db.IS_PG and lock: cur.execute('BEGIN IMMEDIATE')
        yield conn,cur
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally:
        cur.close(); conn.close()

def evento(db, cur, filial, tipo, antes, depois, usuario):
    cur.execute(db.q('INSERT INTO expansao_eventos (id,filial,tipo,antes,depois,usuario,criado_em) VALUES (?,?,?,?,?,?,?)'),
                (uuid.uuid4().hex,str(filial),tipo,json.dumps(antes,ensure_ascii=False),json.dumps(depois,ensure_ascii=False),usuario or 'sistema',agora()))

def migrar(db):
    with transacao(db) as (_,cur):
        cur.execute('CREATE TABLE IF NOT EXISTS expansao_planos (filial TEXT PRIMARY KEY, responsavel TEXT NOT NULL DEFAULT \'\', prazo_envio TEXT NOT NULL DEFAULT \'\', tarefas TEXT NOT NULL DEFAULT \'[]\', versao INTEGER NOT NULL DEFAULT 0, atualizado_por TEXT, atualizado_em TEXT)')
        cur.execute('CREATE TABLE IF NOT EXISTS expansao_eventos (id TEXT PRIMARY KEY, filial TEXT NOT NULL, tipo TEXT NOT NULL, antes TEXT, depois TEXT, usuario TEXT, criado_em TEXT)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_exp_eventos_filial ON expansao_eventos(filial,criado_em)')
        cur.execute('CREATE TABLE IF NOT EXISTS expansao_reservas (item_id INTEGER PRIMARY KEY, filial TEXT NOT NULL, quantidade INTEGER NOT NULL, criado_por TEXT, criado_em TEXT)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_exp_reservas_filial ON expansao_reservas(filial)')
        # Protege todas as rotas de alteração, importação e exclusão do estoque.
        if db.IS_PG:
            cur.execute("""CREATE OR REPLACE FUNCTION expansao_proteger_reserva() RETURNS trigger AS $$
            DECLARE destino TEXT;
            BEGIN
              SELECT filial INTO destino FROM expansao_reservas WHERE item_id=OLD.id;
              IF destino IS NULL THEN IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF; END IF;
              IF TG_OP='DELETE' THEN RAISE EXCEPTION 'Equipamento reservado. Libere a reserva no Acompanhamento antes de excluir.'; END IF;
              IF lower(trim(COALESCE(NEW.status,'')))='enviado' AND trim(COALESCE(NEW.filial_destino,''))=destino AND NEW.codigo IS NOT DISTINCT FROM OLD.codigo AND NEW.tipo_estoque IS NOT DISTINCT FROM OLD.tipo_estoque AND NEW.site_area IS NOT DISTINCT FROM OLD.site_area THEN
                DELETE FROM expansao_reservas WHERE item_id=OLD.id;
                INSERT INTO expansao_eventos VALUES(md5(random()::text||clock_timestamp()::text),destino,'reserva_consumida',row_to_json(OLD)::text,row_to_json(NEW)::text,COALESCE(NULLIF(current_setting('app.audit_actor',true),''),NEW.atualizado_por,'sistema'),to_char(clock_timestamp() AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"'));
                RETURN NEW;
              END IF;
              IF NEW.qtde IS DISTINCT FROM OLD.qtde OR NEW.codigo IS DISTINCT FROM OLD.codigo OR NEW.descricao IS DISTINCT FROM OLD.descricao OR NEW.tipo_estoque IS DISTINCT FROM OLD.tipo_estoque OR NEW.site_area IS DISTINCT FROM OLD.site_area OR NEW.status IS DISTINCT FROM OLD.status OR NEW.filial_destino IS DISTINCT FROM OLD.filial_destino THEN
                RAISE EXCEPTION 'Equipamento reservado. Libere a reserva antes de alterar saldo, produto, status ou destino.';
              END IF;
              RETURN NEW;
            END; $$ LANGUAGE plpgsql""")
            cur.execute('DROP TRIGGER IF EXISTS expansao_reserva_guard ON itens')
            cur.execute('CREATE TRIGGER expansao_reserva_guard BEFORE UPDATE OR DELETE ON itens FOR EACH ROW EXECUTE FUNCTION expansao_proteger_reserva()')
            cur.execute("""CREATE OR REPLACE FUNCTION expansao_auditar() RETURNS trigger AS $$
            BEGIN
              IF TG_OP='INSERT' THEN
                INSERT INTO expansao_eventos VALUES(md5(random()::text||clock_timestamp()::text),NEW.filial,'cadastro','{}',row_to_json(NEW)::text,COALESCE(NULLIF(current_setting('app.audit_actor',true),''),NEW.atualizado_por,'sistema'),to_char(clock_timestamp() AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"'));
                RETURN NEW;
              ELSIF TG_OP='DELETE' THEN
                INSERT INTO expansao_eventos VALUES(md5(random()::text||clock_timestamp()::text),OLD.filial,'exclusao',row_to_json(OLD)::text,'{}',COALESCE(NULLIF(current_setting('app.audit_actor',true),''),OLD.atualizado_por,'sistema'),to_char(clock_timestamp() AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"'));
                DELETE FROM expansao_reservas WHERE filial=OLD.filial;
                RETURN OLD;
              ELSE
                INSERT INTO expansao_eventos VALUES(md5(random()::text||clock_timestamp()::text),NEW.filial,'alteracao',row_to_json(OLD)::text,row_to_json(NEW)::text,COALESCE(NULLIF(current_setting('app.audit_actor',true),''),NEW.atualizado_por,'sistema'),to_char(clock_timestamp() AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"'));
                IF NEW.filial IS DISTINCT FROM OLD.filial THEN
                  UPDATE expansao_reservas SET filial=NEW.filial WHERE filial=OLD.filial;
                  UPDATE expansao_planos SET filial=NEW.filial WHERE filial=OLD.filial;
                  UPDATE expansao_eventos SET filial=NEW.filial WHERE filial=OLD.filial;
                END IF;
                RETURN NEW;
              END IF;
            END; $$ LANGUAGE plpgsql""")
            cur.execute('DROP TRIGGER IF EXISTS expansao_audit ON acompanhamento_expansao')
            cur.execute('CREATE TRIGGER expansao_audit AFTER INSERT OR UPDATE OR DELETE ON acompanhamento_expansao FOR EACH ROW EXECUTE FUNCTION expansao_auditar()')
        else:
            cur.execute("""CREATE TRIGGER IF NOT EXISTS expansao_reserva_delete BEFORE DELETE ON itens
            WHEN EXISTS(SELECT 1 FROM expansao_reservas WHERE item_id=OLD.id)
            BEGIN SELECT RAISE(ABORT,'Equipamento reservado. Libere a reserva antes de excluir.'); END""")
            cur.execute("""CREATE TRIGGER IF NOT EXISTS expansao_reserva_update BEFORE UPDATE ON itens
            WHEN EXISTS(SELECT 1 FROM expansao_reservas WHERE item_id=OLD.id)
            AND NOT(lower(trim(COALESCE(NEW.status,'')))='enviado' AND trim(COALESCE(NEW.filial_destino,''))=(SELECT filial FROM expansao_reservas WHERE item_id=OLD.id) AND NEW.codigo IS OLD.codigo AND NEW.tipo_estoque IS OLD.tipo_estoque AND NEW.site_area IS OLD.site_area)
            AND (NEW.qtde IS NOT OLD.qtde OR NEW.codigo IS NOT OLD.codigo OR NEW.descricao IS NOT OLD.descricao OR NEW.tipo_estoque IS NOT OLD.tipo_estoque OR NEW.site_area IS NOT OLD.site_area OR NEW.status IS NOT OLD.status OR NEW.filial_destino IS NOT OLD.filial_destino)
            BEGIN SELECT RAISE(ABORT,'Equipamento reservado. Libere a reserva antes de alterar saldo, produto, status ou destino.'); END""")
            cur.execute("""CREATE TRIGGER IF NOT EXISTS expansao_reserva_consumir AFTER UPDATE ON itens
            WHEN lower(trim(COALESCE(NEW.status,'')))='enviado' AND EXISTS(SELECT 1 FROM expansao_reservas WHERE item_id=NEW.id AND filial=NEW.filial_destino)
            BEGIN
              INSERT INTO expansao_eventos VALUES(lower(hex(randomblob(16))),NEW.filial_destino,'reserva_consumida',json_object('item_id',OLD.id,'quantidade',OLD.qtde),json_object('item_id',NEW.id,'status',NEW.status),expansao_actor(),strftime('%Y-%m-%dT%H:%M:%SZ','now'));
              DELETE FROM expansao_reservas WHERE item_id=NEW.id;
            END""")
            for op,kind in [('INSERT','cadastro'),('UPDATE','alteracao'),('DELETE','exclusao')]:
                old="json_object("+','.join("'%s',OLD.%s"%(c,c) for c in CAMPOS_HIST)+")" if op!='INSERT' else "'{}'"
                new="json_object("+','.join("'%s',NEW.%s"%(c,c) for c in CAMPOS_HIST)+")" if op!='DELETE' else "'{}'"
                ref='OLD' if op=='DELETE' else 'NEW'
                extra="DELETE FROM expansao_reservas WHERE filial=OLD.filial;" if op=='DELETE' else ''
                if op=='UPDATE':
                    extra=' '.join(f'UPDATE {t} SET filial=NEW.filial WHERE filial=OLD.filial AND NEW.filial!=OLD.filial;' for t in ('expansao_reservas','expansao_planos','expansao_eventos'))
                cur.execute(f"CREATE TRIGGER IF NOT EXISTS expansao_audit_{op.lower()} AFTER {op} ON acompanhamento_expansao BEGIN INSERT INTO expansao_eventos VALUES(lower(hex(randomblob(16))),{ref}.filial,'{kind}',{old},{new},expansao_actor(),strftime('%Y-%m-%dT%H:%M:%SZ','now')); {extra} END")

        # Mantém alterações de kit e quantidades enviadas no histórico da filial.
        for tabela, filial_col, campos in (
            ('kit_filial_personalizado','filial',('filial','codigo','descricao','quantidade')),
            ('itens','filial_destino',('id','codigo','descricao','qtde','status','filial_destino')),
        ):
            if db.IS_PG:
                area_guard = "IF TG_OP='DELETE' THEN IF OLD.site_area!='expansao' THEN RETURN OLD; END IF; ELSE IF NEW.site_area!='expansao' THEN RETURN NEW; END IF; END IF;" if tabela=='itens' else ''
                cur.execute(f"""CREATE OR REPLACE FUNCTION expansao_audit_{tabela}() RETURNS trigger AS $$
                DECLARE destino TEXT; a TEXT; b TEXT;
                BEGIN
                  {area_guard}
                  IF TG_OP='INSERT' THEN destino=NEW.{filial_col}; a='{{}}'; b=row_to_json(NEW)::text;
                  ELSIF TG_OP='DELETE' THEN destino=OLD.{filial_col}; a=row_to_json(OLD)::text; b='{{}}';
                  ELSE destino=COALESCE(NULLIF(NEW.{filial_col},''),OLD.{filial_col}); a=row_to_json(OLD)::text; b=row_to_json(NEW)::text; END IF;
                  IF COALESCE(destino,'')!='' THEN
                    INSERT INTO expansao_eventos VALUES(md5(random()::text||clock_timestamp()::text),destino,'{tabela}_'||lower(TG_OP),a,b,COALESCE(NULLIF(current_setting('app.audit_actor',true),''),'sistema'),to_char(clock_timestamp() AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS"Z"'));
                  END IF;
                  IF TG_OP='DELETE' THEN RETURN OLD; ELSE RETURN NEW; END IF;
                END; $$ LANGUAGE plpgsql""")
                cur.execute(f'DROP TRIGGER IF EXISTS expansao_hist_{tabela} ON {tabela}')
                cur.execute(f'CREATE TRIGGER expansao_hist_{tabela} AFTER INSERT OR UPDATE OR DELETE ON {tabela} FOR EACH ROW EXECUTE FUNCTION expansao_audit_{tabela}()')
            else:
                for op in ('INSERT','UPDATE','DELETE'):
                    old="json_object("+','.join("'%s',OLD.%s"%(c,c) for c in campos)+")" if op!='INSERT' else "'{}'"
                    new="json_object("+','.join("'%s',NEW.%s"%(c,c) for c in campos)+")" if op!='DELETE' else "'{}'"
                    filial=f'OLD.{filial_col}' if op=='DELETE' else f'NEW.{filial_col}' if op=='INSERT' else f"COALESCE(NULLIF(NEW.{filial_col},''),OLD.{filial_col})"
                    ref = 'OLD' if op=='DELETE' else 'NEW'
                    area_guard = f" AND {ref}.site_area='expansao'" if tabela=='itens' else ''
                    cur.execute(f"CREATE TRIGGER IF NOT EXISTS expansao_hist_{tabela}_{op.lower()} AFTER {op} ON {tabela} WHEN COALESCE({filial},'')!=''{area_guard} BEGIN INSERT INTO expansao_eventos VALUES(lower(hex(randomblob(16))),{filial},'{tabela}_{op.lower()}',{old},{new},expansao_actor(),strftime('%Y-%m-%dT%H:%M:%SZ','now')); END")


def backup_dados(db):
    saida = {"versao": 1, "gerado_em": agora()}
    with transacao(db) as (_, cur):
        for tabela in ("acompanhamento_expansao", "expansao_planos", "expansao_reservas", "expansao_eventos"):
            cur.execute("SELECT * FROM " + tabela)
            saida[tabela] = [dict(r) for r in cur.fetchall()]
    return saida


def planos_reservas(db):
    with transacao(db) as (_,cur):
        cur.execute('SELECT * FROM expansao_planos'); planos={str(r['filial']):dict(r) for r in cur.fetchall()}
        cur.execute('SELECT * FROM expansao_reservas'); reservas=[dict(r) for r in cur.fetchall()]
    for p in planos.values(): p['tarefas']=json.loads(p['tarefas'])
    return planos,reservas

def plano_vazio(filial):
    return dict(filial=filial,responsavel='',prazo_envio='',versao=0,tarefas=[dict(etapa=e,status='Pendente',responsavel='',prazo='',observacao='') for e in ETAPAS])

def validar_plano(d):
    if not isinstance(d,dict): raise ValueError('Dados inválidos.')
    tarefas=d.get('tarefas')
    if not isinstance(tarefas,list) or len(tarefas)!=len(ETAPAS): raise ValueError('Preencha as cinco etapas do checklist.')
    result=[]
    for etapa,t in zip(ETAPAS,tarefas):
        if not isinstance(t,dict) or t.get('etapa')!=etapa or t.get('status') not in ('Pendente','Em andamento','Concluído','Não se aplica'): raise ValueError('Etapa ou status inválido.')
        prazo=str(t.get('prazo') or '')
        if prazo and (not data(prazo) or data(prazo).isoformat()!=prazo): raise ValueError('Prazo inválido.')
        result.append(dict(etapa=etapa,status=t['status'],prazo=prazo,responsavel=str(t.get('responsavel') or '').strip()[:120],observacao=str(t.get('observacao') or '').strip()[:1000]))
    prazo=str(d.get('prazo_envio') or '')
    if prazo and (not data(prazo) or data(prazo).isoformat()!=prazo): raise ValueError('Data de envio inválida.')
    return dict(responsavel=str(d.get('responsavel') or '').strip()[:120],prazo_envio=prazo,tarefas=result)

def salvar_plano(db, filial, d, usuario):
    novo=validar_plano(d)
    with transacao(db,True) as (_,cur):
        cur.execute(db.q("INSERT INTO expansao_planos(filial) VALUES(?) ON CONFLICT(filial) DO NOTHING"),(filial,))
        cur.execute(db.q('SELECT * FROM expansao_planos WHERE filial=?'+(' FOR UPDATE' if db.IS_PG else '')),(filial,))
        anterior=dict(cur.fetchone())
        if d.get('versao')!=anterior['versao']: raise ValueError('Outra pessoa alterou este plano. Feche e abra a gestão da filial para atualizar.')
        anterior['tarefas']=json.loads(anterior['tarefas'])
        cur.execute(db.q('UPDATE expansao_planos SET responsavel=?,prazo_envio=?,tarefas=?,versao=versao+1,atualizado_por=?,atualizado_em=? WHERE filial=?'),(novo['responsavel'],novo['prazo_envio'],json.dumps(novo['tarefas'],ensure_ascii=False),usuario,agora(),filial))
        evento(db,cur,filial,'checklist',anterior,novo,usuario)
    return novo


def prever(linhas, itens, kits, planos, reservas, combina, today=None):
    """Aloca saldo uma única vez, no conjunto global, antes de filtrar a tela."""
    today=today or hoje()
    owners={int(r['item_id']):str(r['filial']) for r in reservas}
    stock={int(i['id']):i for i in itens if norm(i.get('tipo_estoque'))=='expansao' and norm(i.get('status'))!='enviado' and qtd(i.get('qtde'))>0}
    livres=set(stock)-set(owners)
    result={}
    for r in sorted(linhas,key=lambda r:(data(r.get('entrada_ti')) or data(r.get('inauguracao')) or date.max,str(r['filial']))):
        filial=str(r['filial']); plano=planos.get(filial) or plano_vazio(filial)
        usados=set(); equipamentos=[]; sugeridos=[]
        enviado=[i for i in itens if norm(i.get('status'))=='enviado' and str(i.get('filial_destino') or '').strip()==filial]
        for k in kits.get(filial,[]):
            necessario=qtd(k.get('quantidade')); sent=0; reservado=0; previsto=0
            for i in enviado:
                if int(i['id']) not in usados and combina(i,k):
                    sent+=max(1,qtd(i.get('qtde'))); usados.add(int(i['id']))
            for item_id,i in stock.items():
                if owners.get(item_id)==filial and item_id not in usados and combina(i,k):
                    reservado+=qtd(i['qtde']); usados.add(item_id)
            faltam=max(0,necessario-sent-reservado)
            if aberto(r):
                for item_id in sorted(list(livres),key=lambda n:(qtd(stock[n]['qtde']),n)):
                    if previsto>=faltam: break
                    i=stock[item_id]
                    if combina(i,k):
                        previsto+=qtd(i['qtde']); livres.remove(item_id); sugeridos.append(item_id)
            equipamentos.append(dict(codigo=k.get('codigo',''),descricao=k.get('descricao',''),necessario=necessario,enviado=sent,reservado=reservado,previsto=previsto,falta=max(0,faltam-previsto)))
        motivos=[]; atrasada=False
        tarefas=plano['tarefas'] or plano_vazio(filial)['tarefas']
        pendencias=[t for t in tarefas if t['status'] not in ('Concluído','Não se aplica')]
        vencidas=[t for t in pendencias if data(t.get('prazo')) and data(t['prazo'])<today]
        inaug=data(r.get('inauguracao')); ti=data(r.get('entrada_ti')); envio=data(plano.get('prazo_envio'))
        if aberto(r):
            if inaug and inaug<today: motivos.append('Data de inauguração vencida sem status Inaugurada'); atrasada=True
            if vencidas: motivos.append('Checklist com prazo vencido: '+', '.join(t['etapa'] for t in vencidas)); atrasada=True
            if envio and envio<today and norm(r.get('enviada'))!='sim': motivos.append('Prazo de envio vencido'); atrasada=True
            if not inaug: motivos.append('Inauguração sem data definida')
            if not ti: motivos.append('Entrada de TI sem data definida')
            if ti and (ti-today).days<=7 and pendencias: motivos.append('Entrada de TI em até 7 dias com checklist pendente')
            if not plano.get('responsavel'): motivos.append('Responsável pela filial não definido')
            if any(not t.get('prazo') or not t.get('responsavel') for t in pendencias): motivos.append('Há ações sem responsável ou prazo')
            if not equipamentos: motivos.append('Kit da filial não cadastrado')
            if any(e['falta'] for e in equipamentos): motivos.append('Estoque insuficiente após reservas e prioridade de Entrada TI')
            if ti and inaug and ti>inaug: motivos.append('Entrada TI posterior à inauguração')
        situacao=('Atrasada' if atrasada else 'Em risco' if motivos else 'No prazo') if aberto(r) else ('Concluída' if norm(r.get('status_filial'))=='inaugurada' else 'Fora do planejamento')
        result[filial]=dict(plano=plano,equipamentos=equipamentos,sugeridos=sugeridos,reservas=[x for x in reservas if str(x['filial'])==filial],situacao=situacao,motivos=motivos,pendencias=pendencias,faltantes=sum(x['falta'] for x in equipamentos),concluidas=sum(t['status'] in ('Concluído','Não se aplica') for t in tarefas))
    return result

def reservar(db, filial, ids, usuario):
    if not ids or len(ids)>2000: raise ValueError('Não há equipamentos previstos para reservar.')
    ids=sorted(set(int(x) for x in ids))
    with transacao(db,True) as (_,cur):
        cur.execute(db.q('SELECT status_filial FROM acompanhamento_expansao WHERE filial=?'+(' FOR UPDATE' if db.IS_PG else '')),(filial,))
        r=cur.fetchone()
        if not r or not aberto(dict(r)): raise ValueError('A filial não está aberta para planejamento.')
        ph=','.join('?' for _ in ids)
        cur.execute(db.q(f'SELECT * FROM itens WHERE id IN ({ph}) ORDER BY id'+(' FOR UPDATE' if db.IS_PG else '')),ids)
        encontrados={int(x['id']):dict(x) for x in cur.fetchall()}
        for item_id in ids:
            i=encontrados.get(item_id)
            if not i or i.get('site_area')!='expansao' or norm(i.get('tipo_estoque'))!='expansao' or norm(i.get('status'))=='enviado' or qtd(i.get('qtde'))<=0: raise ValueError('O estoque mudou. Atualize a página antes de reservar.')
            cur.execute(db.q('SELECT filial FROM expansao_reservas WHERE item_id=?'),(item_id,))
            if cur.fetchone(): raise ValueError('Um equipamento já foi reservado. Atualize a página.')
            cur.execute(db.q('INSERT INTO expansao_reservas VALUES(?,?,?,?,?)'),(item_id,filial,qtd(i['qtde']),usuario,agora()))
        evento(db,cur,filial,'reserva',{},[dict(item_id=i,quantidade=qtd(encontrados[i]['qtde'])) for i in ids],usuario)

def liberar(db,filial,usuario):
    with transacao(db,True) as (_,cur):
        cur.execute(db.q('SELECT * FROM expansao_reservas WHERE filial=?'),(filial,))
        antes=[dict(r) for r in cur.fetchall()]
        cur.execute(db.q('DELETE FROM expansao_reservas WHERE filial=?'),(filial,))
        if antes: evento(db,cur,filial,'liberacao_reserva',antes,[],usuario)

def historico(db,filial):
    with transacao(db) as (_,cur):
        cur.execute(db.q('SELECT * FROM expansao_eventos WHERE filial=? ORDER BY criado_em DESC,id DESC LIMIT 200'),(filial,))
        result=[dict(x) for x in cur.fetchall()]
    for r in result:
        r['antes']=json.loads(r['antes'] or '{}');r['depois']=json.loads(r['depois'] or '{}')
    return result

def relatorio(dados, gestao, filtros):
    from xml.sax.saxutils import escape
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.units import mm
    styles=getSampleStyleSheet()
    styles['Heading2'].keepWithNext=True
    styles.add(ParagraphStyle('CellExp',fontName='Helvetica',fontSize=8,leading=11,textColor=colors.HexColor('#23354b')))
    styles.add(ParagraphStyle('HeadExp',parent=styles['CellExp'],fontName='Helvetica-Bold',textColor=colors.white))
    buf=io.BytesIO(); doc=SimpleDocTemplate(buf,pagesize=landscape(A4),leftMargin=15*mm,rightMargin=15*mm,topMargin=17*mm,bottomMargin=17*mm)
    story=[Paragraph('Expansão | Resumo executivo',styles['Title']),Paragraph('Emitido em '+datetime.now(ZoneInfo('America/Sao_Paulo')).strftime('%d/%m/%Y %H:%M')+' (Brasília)',styles['Normal']),Spacer(1,8)]
    scope=' · '.join(f'{k}: {v}' for k,v in filtros.items() if v) or 'Todos os registros'
    story += [Paragraph(escape(scope),styles['Normal']),Spacer(1,12)]
    linhas=dados['linhas']; gs=[gestao[str(r['filial'])] for r in linhas]
    nums=[('Lojas',len(linhas)),('Em risco',sum(g['situacao']=='Em risco' for g in gs)),('Atrasadas',sum(g['situacao']=='Atrasada' for g in gs)),('Unidades faltantes',sum(g['faltantes'] for g in gs if g['situacao'] not in ('Concluída','Fora do planejamento')))]
    story += [Paragraph(' | '.join(f'{k}: <b>{v}</b>' for k,v in nums),styles['Normal']),Spacer(1,14)]
    def table(headers, rows, widths):
        vals=[[Paragraph(escape(str(x)),styles['HeadExp']) for x in headers]]+[[Paragraph(escape(str('-' if x is None or x == '' else x)).replace('\n','<br/>'),styles['CellExp']) for x in row] for row in rows]
        t=Table(vals,colWidths=widths,repeatRows=1,hAlign='LEFT')
        t.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,0),colors.HexColor('#173854')),('ROWBACKGROUNDS',(0,1),(-1,-1),[colors.white,colors.HexColor('#eef3f7')]),('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),7),('RIGHTPADDING',(0,0),(-1,-1),7),('TOPPADDING',(0,0),(-1,-1),7),('BOTTOMPADDING',(0,0),(-1,-1),7)]))
        return t
    rows=[];acoes=[];faltas=[]
    for r in sorted(linhas,key=lambda r:(data(r.get('entrada_ti')) or date.max,str(r['filial']))):
        g=gestao[str(r['filial'])];p=g['plano']
        rows.append([r['filial'],r.get('descricao_filial'),r.get('entrada_ti'),r.get('inauguracao'),g['situacao'],p.get('responsavel'),'; '.join(g['motivos']) or 'Sem alertas pelas regras atuais'])
        if aberto(r):
            for t in g['pendencias']: acoes.append([r['filial'],t['etapa'],t.get('responsavel') or 'Não definido',t.get('prazo') or 'Não definido',t['status'],t.get('observacao')])
            for e in g['equipamentos']:
                if e['falta']: faltas.append([r['filial'],e['codigo'],e['descricao'],e['necessario'],e['reservado'],e['previsto'],e['falta']])
    if rows: story.append(table(['Filial','Descrição','Entrada TI','Inauguração','Situação','Responsável','Motivos'],rows,[42,118,65,65,65,85,217]))
    else: story.append(Paragraph('Nenhuma filial encontrada para os filtros selecionados.',styles['Normal']))
    story += [Spacer(1,14),Paragraph('Ações pendentes',styles['Heading2'])]
    if acoes: story.append(table(['Filial','Etapa','Responsável','Prazo','Status','Observação'],acoes,[42,85,110,75,80,265]))
    else: story.append(Paragraph('Sem ações pendentes no período.',styles['Normal']))
    story += [Spacer(1,14),Paragraph('Equipamentos faltantes',styles['Heading2'])]
    if faltas: story.append(table(['Filial','Código','Equipamento','Kit','Reservado','Previsto','Falta'],faltas,[42,90,305,50,60,60,50]))
    else: story.append(Paragraph('Sem falta calculada de equipamentos no período.',styles['Normal']))
    story += [Spacer(1,12),Paragraph('Previsão global por Entrada de TI, após as reservas confirmadas. Previsão não confirma reserva. Registros de estoque são reservados integralmente; lotes podem exceder o kit. Histórico disponível a partir desta atualização.',styles['CellExp'])]
    def footer(canvas,doc):
        canvas.setFont('Helvetica',8);canvas.setFillColor(colors.HexColor('#61778b'));canvas.drawString(15*mm,10*mm,'DPSP | Expansão');canvas.drawRightString(282*mm,10*mm,f'Página {doc.page}')
    doc.build(story,onFirstPage=footer,onLaterPages=footer);buf.seek(0);return buf


def registrar(app,db,login_required,edit_required,site_required,csrf_ok,kit_efetivo,combina,resumir):
    def conflito_reserva(exc):
        if 'reservad' in str(exc).lower():
            return jsonify(erro='Equipamento reservado para uma filial. Libere a reserva no Acompanhamento antes de excluir ou alterar saldo, produto, status ou destino.'),409
        app.logger.exception('Erro no banco de dados')
        return jsonify(erro='Não foi possível concluir a operação no banco de dados.'),500
    if db.IS_PG:
        import psycopg2
        app.register_error_handler(psycopg2.Error, conflito_reserva)
    else:
        import sqlite3
        app.register_error_handler(sqlite3.DatabaseError, conflito_reserva)
    def estado():
        linhas=db.listar_acompanhamento_expansao(); itens=db.listar_itens();planos,reservas=planos_reservas(db)
        padrao=db.listar_kit_padrao_loja(); personalizados=db.listar_todos_kits_filiais_personalizados()
        kits={str(r['filial']):personalizados.get(str(r['filial']),[]) or padrao for r in linhas}
        gestao=prever(linhas,itens,kits,planos,reservas,combina)
        return linhas,gestao
    def registro(registro_id):
        return db.buscar_acompanhamento_expansao_por_id(registro_id)
    @app.route('/api/acompanhamento-expansao/gestao')
    @login_required
    @site_required('expansao')
    def expansao_gestao_estado():
        linhas,gestao=estado();saida=resumir(filtrar(linhas,request.args));saida['gestao']=gestao
        return jsonify(saida)
    @app.route('/api/acompanhamento-expansao/<int:registro_id>/gestao',methods=['GET','PUT'])
    @login_required
    @site_required('expansao')
    def expansao_gestao_filial(registro_id):
        r=registro(registro_id)
        if not r: return jsonify(erro='Filial não encontrada.'),404
        if request.method=='PUT':
            if session.get('role') not in ('admin','gestor','operador'): return jsonify(erro='Sem permissão para editar.'),403
            if not csrf_ok(): return jsonify(erro='Sessão expirada. Atualize a página.'),400
            try: salvar_plano(db,str(r['filial']),request.get_json(silent=True),session.get('username'))
            except ValueError as exc: return jsonify(erro=str(exc)),409
        planos,reservas=planos_reservas(db)
        p=planos.get(str(r['filial'])) or plano_vazio(str(r['filial']))
        if not p['tarefas']: p['tarefas']=plano_vazio(str(r['filial']))['tarefas']
        return jsonify(plano=p,historico=historico(db,str(r['filial'])),reservas=[x for x in reservas if x['filial']==str(r['filial'])])
    @app.route('/api/acompanhamento-expansao/<int:registro_id>/reservas',methods=['POST','DELETE'])
    @edit_required
    @site_required('expansao')
    def expansao_gestao_reservar(registro_id):
        if not csrf_ok(): return jsonify(erro='Sessão expirada. Atualize a página.'),400
        r=registro(registro_id)
        if not r: return jsonify(erro='Filial não encontrada.'),404
        try:
            if request.method=='DELETE': liberar(db,str(r['filial']),session.get('username'))
            else:
                _,gestao=estado(); g=gestao[str(r['filial'])]
                ids=(request.get_json(silent=True) or {}).get('ids')
                if not isinstance(ids,list) or not ids or any(type(x)!=int for x in ids) or set(ids)!=set(g['sugeridos']): raise ValueError('A previsão mudou. Atualize a página e revise os equipamentos antes de reservar.')
                reservar(db,str(r['filial']),ids,session.get('username'))
            return jsonify(ok=True)
        except ValueError as exc: return jsonify(erro=str(exc)),409
    @app.route('/api/acompanhamento-expansao/resumo-executivo.pdf')
    @login_required
    @site_required('expansao')
    def expansao_gestao_pdf():
        linhas,gestao=estado();filtros={k:request.args.get(k,'') for k in ('mes','busca','status','projeto','bandeira','uf')}
        d=resumir(filtrar(linhas,filtros))
        return send_file(relatorio(d,gestao,filtros),mimetype='application/pdf',as_attachment=True,download_name='expansao-resumo-executivo.pdf')
    return estado
