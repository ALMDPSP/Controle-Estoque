"""Checklist transitório no navegador; somente os relatórios PDF vão ao Storage."""
import hashlib
import io
import re
import uuid
from pathlib import Path
from threading import Lock
from datetime import datetime
from html import escape
from urllib.parse import quote
from zoneinfo import ZoneInfo

from flask import jsonify, request, session, render_template, send_file, redirect
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename
import manuais
from expansao_gestao import transacao, norm

FONT_LOCK = Lock()

MODELO = {
    'pdv': ['Venda para Logix', 'Validar parâmetros do PDV', 'Validar base do PDV', 'Validar impressão do PDV'],
    'balcao': ['Validar impressão Lexmark', 'Validar impressão VivaSaúde', 'Validar scanner',
               'Validar sistema de loja e acesso de supervisor', 'Validar Pharmaconnection',
               'Validar VidaLink', 'Validar ícones e acessos', 'Validar pasta de notas People',
               'Validar pasta de notas DSS', 'Ajustar e validar impressão DSS'],
    'servidor': ['Validar instalação do Pharmaconnection', 'Validar instalação do Syncros',
                 'Validar serviços P2K', 'Validar base P2K',
                 r'Validar compartilhamento da pasta DSS: analisa\semc ou servidor\semc'],
    'pdv_extra': ['Validar venda básica', 'Validar TEF e pinpad', 'Validar DDG (desconto do gerente)',
                  'Validar venda PBM', 'Validar venda convênio', 'Validar pedido delivery'],
    'geral': ['Validar rede e acesso à internet', 'Validar data e hora dos equipamentos'],
}
RESULTADOS = {'pendente': 'Pendente', 'conforme': 'Conforme', 'falha': 'Não conforme', 'na': 'Não se aplica'}
ABERTOS = {'pendente', 'em andamento', 'obra nao iniciada', 'inaugurar', 'a inaugurar'}
ARQUIVO = re.compile(r'\d{8}-\d{6}-(parcial|final)-[a-f0-9]{12}\.pdf')

def pdvs_validos(valores):
    if not isinstance(valores,list) or not 1<=len(valores)<=30:
        raise ValueError('Informe de 1 a 30 PDVs.')
    result=[]
    for value in valores:
        if not isinstance(value,(str,int)) or not re.fullmatch(r'\d{1,2}',str(value)) or not 1<=int(value)<=99:
            raise ValueError('Identifique os PDVs com números de 1 a 99, separados por vírgula.')
        ident=f'{int(value):02}'
        if ident in result: raise ValueError('Não repita o número de um PDV.')
        result.append(ident)
    return result

def grupos(pdvs, extras):
    result=[]
    for pdv in pdvs:
        labels=MODELO['pdv']+(MODELO['pdv_extra'] if extras else [])
        result.append({'id':'pdv-'+pdv,'titulo':'PDV '+pdv,'itens':[{'id':f'pdv-{pdv}-{i}','titulo':label} for i,label in enumerate(labels)]})
    for key,title in [('balcao','Balcão'),('servidor','Servidor')]+([('geral','Validações complementares')] if extras else []):
        result.append({'id':key,'titulo':title,'itens':[{'id':f'{key}-{i}','titulo':label} for i,label in enumerate(MODELO[key])]})
    return result

def pasta(filial):
    return 'checklists/'+(secure_filename(filial)[:40] or 'loja')+'-'+hashlib.sha256(filial.encode()).hexdigest()[:12]

def texto(value, limite, campo, obrigatorio=False):
    if not isinstance(value,str):raise ValueError('Informe '+campo+'.')
    value=value.strip()
    if len(value)>limite or (obrigatorio and not value):raise ValueError('Informe '+campo+' (até '+str(limite)+' caracteres).')
    return value

def validar(data):
    if not isinstance(data,dict):raise ValueError('Dados de checklist inválidos.')
    filial=texto(data.get('filial',''),80,'a filial',True)
    responsavel=texto(data.get('responsavel',''),120,'o responsável pela execução',True)
    observacao=texto(data.get('observacao',''),2000,'a observação geral')
    pdvs=pdvs_validos(data.get('pdvs'))
    extras=data.get('complementares',False)
    if not isinstance(extras,bool):raise ValueError('Opção de testes complementares inválida.')
    modo=data.get('modo','parcial')
    if modo not in ('parcial','final'):raise ValueError('Tipo de relatório inválido.')
    valores=data.get('itens',{})
    if not isinstance(valores,dict):raise ValueError('Resultados inválidos.')
    sections=grupos(pdvs,extras);expected={item['id'] for group in sections for item in group['itens']}
    if set(valores)-expected:raise ValueError('Os itens não correspondem aos PDVs e ao modelo selecionado.')
    counts={k:0 for k in RESULTADOS}
    for group in sections:
        for item in group['itens']:
            value=valores.get(item['id'],{})
            if not isinstance(value,dict):raise ValueError('Resultado de item inválido.')
            result=value.get('resultado','pendente')
            if not isinstance(result,str) or result not in RESULTADOS:raise ValueError('Selecione um resultado válido.')
            note=texto(value.get('observacao',''),1000,'a observação do item')
            if result in ('falha','na') and not note:raise ValueError('Informe a justificativa dos itens não conformes ou não aplicáveis: '+group['titulo']+' - '+item['titulo'])
            item.update(resultado=result,observacao=note)
            counts[result]+=1
    if modo=='final' and (counts['pendente'] or counts['falha']):
        raise ValueError('O relatório final exige todos os itens conformes ou não aplicáveis, com justificativa. Use o relatório parcial para registrar pendências.')
    return dict(filial=filial,responsavel=responsavel,observacao=observacao,pdvs=pdvs,complementares=extras,modo=modo,grupos=sections,resumo=counts)

def gerar_pdf(data,loja,username,ident=None):
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    with FONT_LOCK:
        for name,filename in [('ALMSans','DejaVuSans.ttf'),('ALMSans-Bold','DejaVuSans-Bold.ttf')]:
            if name not in pdfmetrics.getRegisteredFontNames():
                pdfmetrics.registerFont(TTFont(name,str(Path(__file__).parent/'static'/'fonts'/filename)))
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, Flowable
    ident=ident or uuid.uuid4().hex[:12]
    emitted=datetime.now(ZoneInfo('America/Sao_Paulo'))
    blue=colors.HexColor('#FFFFFF');muted=colors.HexColor('#9DB3C8');line=colors.HexColor('#2A3645')
    bg=colors.HexColor('#0F1620');panel=colors.HexColor('#151D27');panel2=colors.HexColor('#182230')
    accent=colors.HexColor('#5AB4FF')
    page_width,page_height=landscape(A4)
    styles={
        'title':ParagraphStyle('title',fontName='ALMSans-Bold',fontSize=20,leading=25,textColor=blue,spaceAfter=9),
        'heading':ParagraphStyle('heading',fontName='ALMSans-Bold',fontSize=12,leading=17,textColor=blue,spaceBefore=15,spaceAfter=8,keepWithNext=True),
        'body':ParagraphStyle('body',fontName='ALMSans',fontSize=9,leading=13,textColor=blue,spaceAfter=5,wordWrap='LTR'),
        'cell':ParagraphStyle('cell',fontName='ALMSans',fontSize=8.5,leading=12,textColor=blue),
        'head':ParagraphStyle('head',fontName='ALMSans-Bold',fontSize=8,leading=11,textColor=colors.white),
    }
    def p(value,style='body'):
        value=str(value or '').replace('\u2011','-').replace('\u2013','-').replace('\u2014','-').encode('cp1252','replace').decode('cp1252')
        return Paragraph(escape(value).replace('\n','<br/>'),styles[style])
    out=io.BytesIO();width=page_width-84
    doc=SimpleDocTemplate(out,pagesize=landscape(A4),leftMargin=42,rightMargin=42,topMargin=62,bottomMargin=52,title='Checklist de inauguração - '+data['filial'],author=username)
    final=data['modo']=='final';summary=data['resumo'];total=sum(summary.values());checked=summary['conforme']+summary['na']
    story=[p('CHECKLIST DE INAUGURAÇÃO','title'),p(('RELATÓRIO FINAL' if final else 'RELATÓRIO PARCIAL - NÃO CONCLUÍDO')+' | '+emitted.strftime('%d/%m/%Y %H:%M')+' (Brasília)'),Spacer(1,8)]
    info=[['Filial',data['filial']+' - '+str(loja.get('descricao_filial') or '')],['Bandeira / UF',str(loja.get('bandeira') or '-')+' / '+str(loja.get('uf') or '-')],['Inauguração prevista',loja.get('inauguracao') or 'Não informada'],['Responsável pela execução',data['responsavel']],['Usuário emissor',username],['PDVs avaliados',', '.join(data['pdvs'])],['Identificação do relatório',ident]]
    table=Table([[p(k),p(v)] for k,v in info],colWidths=[145,width-145],hAlign=TA_LEFT)
    table.setStyle(TableStyle([('BACKGROUND',(0,0),(-1,-1),panel),('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),10),('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5)]));story+=[table,Spacer(1,12)]
    class Indicadores(Flowable):
        def __init__(self):
            Flowable.__init__(self);self.width=width;self.height=73
        def draw(self):
            gap=8;w=(width-3*gap)/4
            for index,(key,label,hexcolor) in enumerate([('conforme','CONFORMES','#52D69A'),('falha','NÃO CONFORMES','#FF7E88'),('pendente','PENDENTES','#FFBE55'),('na','NÃO APLICÁVEIS','#A78BFA')]):
                x=index*(w+gap);c=self.canv;c.setFillColor(panel2);c.setStrokeColor(colors.HexColor('#314255'));c.roundRect(x,8,w,57,9,stroke=1,fill=1)
                c.setFillColor(muted);c.setFont('ALMSans-Bold',7);c.drawString(x+11,49,label)
                c.setFillColor(colors.HexColor(hexcolor));c.setFont('ALMSans-Bold',18);c.drawString(x+11,23,str(summary[key]))
    story.append(Indicadores())
    story.append(p(f'Progresso da validação: {checked} de {total} itens ({round(100*checked/total)}%).'))
    if not final:story.append(p('Este relatório registra o andamento da validação e não representa a conclusão do checklist.'))
    if data['observacao']:story += [p('Observações gerais','heading'),p(data['observacao'])]
    story.append(PageBreak())
    for group in data['grupos']:
        story.append(p(group['titulo'],'heading'))
        rows=[[p('VALIDAÇÃO','head'),p('RESULTADO','head'),p('OBSERVAÇÃO / JUSTIFICATIVA','head')]]
        for item in group['itens']:rows.append([p(item['titulo'],'cell'),p(RESULTADOS[item['resultado']],'cell'),p(item['observacao'] or '-','cell')])
        table=Table(rows,colWidths=[width*.40,width*.19,width*.41],repeatRows=1,hAlign=TA_LEFT,splitInRow=1)
        commands=[('BACKGROUND',(0,0),(-1,0),colors.HexColor('#212D3C')),('VALIGN',(0,0),(-1,-1),'TOP'),('LEFTPADDING',(0,0),(-1,-1),8),('RIGHTPADDING',(0,0),(-1,-1),8),('TOPPADDING',(0,0),(-1,-1),8),('BOTTOMPADDING',(0,0),(-1,-1),8),('LINEBELOW',(0,0),(-1,-1),.5,line)]
        for i,item in enumerate(group['itens'],1):
            row_bg='#33232D' if item['resultado']=='falha' else '#302B22' if item['resultado']=='pendente' else '#151D27' if i%2 else '#182230'
            commands.append(('BACKGROUND',(0,i),(-1,i),colors.HexColor(row_bg)))
        table.setStyle(TableStyle(commands));story.append(table)
    story += [Spacer(1,16),p('Responsável pela execução: '+data['responsavel']),p('Emitido pelo usuário autenticado: '+username),p('As informações representam os resultados declarados pelo responsável na emissão deste relatório.')]
    def footer(canvas,document):
        canvas.saveState();canvas.setFillColor(bg);canvas.rect(0,0,page_width,page_height,stroke=0,fill=1)
        if document.page>1:
            canvas.setFillColor(colors.white);canvas.setFont('ALMSans-Bold',14)
            canvas.drawString(42,page_height-29,'Checklist de Inauguração - '+data['filial'][:36])
            canvas.setFillColor(muted);canvas.setFont('ALMSans',7.5)
            canvas.drawString(42,page_height-44,'Resultados por área e PDV | '+('Relatório final' if final else 'Relatório parcial'))
            canvas.drawRightString(page_width-42,page_height-29,emitted.strftime('Gerado em %d/%m/%Y às %H:%M (Brasília)'))
        canvas.setStrokeColor(line);canvas.line(42,34,page_width-42,34);canvas.setFillColor(colors.HexColor('#8398AD'));canvas.setFont('ALMSans',7)
        canvas.drawString(42,21,'© 2026 · Developed by ALM - Expansão de TI · Checklist de Inauguração')
        canvas.drawRightString(page_width-42,21,'Página '+str(document.page)+' | '+ident);canvas.restoreState()
    doc.build(story,onFirstPage=footer,onLaterPages=footer)
    out.seek(0);return out

def registrar(app,db,login_required,site_required,csrf_ok):
    def lojas():
        with transacao(db) as (_,c):
            c.execute('SELECT filial,descricao_filial,bandeira,uf,inauguracao,status_filial FROM acompanhamento_expansao ORDER BY filial')
            return [dict(r) for r in c.fetchall()]
    def filial_param():return texto(request.args.get('filial',''),80,'a filial',True)
    def autorizado():
        if session.get('role') not in ('admin','gestor','operador','user'):return jsonify(erro='Seu perfil permite somente consultar relatórios.'),403
        if not csrf_ok():return jsonify(erro='Sessão expirada. Atualize a página.'),400

    @app.route('/checklist-inauguracao')
    @login_required
    @site_required('expansao')
    def pagina_checklist():
        return render_template('checklists.html',role=session.get('role'),username=session.get('username'),is_admin=session.get('role')=='admin')

    @app.route('/api/checklists/lojas')
    @login_required
    @site_required('expansao')
    def api_checklist_lojas():
        response=jsonify(lojas=[dict(r,aberta=norm(r['status_filial']) in ABERTOS) for r in lojas()],modelo=MODELO,usuario=session.get('username') or 'Usuário')
        response.headers['Cache-Control']='no-store';return response

    @app.route('/api/checklists/pdf',methods=['POST'])
    @login_required
    @site_required('expansao')
    def api_checklist_pdf():
        request.max_content_length=512*1024
        try:
            err=autorizado()
            if err:return err
            data=validar(request.get_json(silent=True))
        except RequestEntityTooLarge:return jsonify(erro='O checklist excedeu o tamanho permitido.'),413
        except ValueError as exc:return jsonify(erro=str(exc)),400
        loja=next((r for r in lojas() if r['filial']==data['filial']),None)
        if not loja:return jsonify(erro='Filial não encontrada no Acompanhamento de Expansão.'),404
        username=session.get('username') or 'Usuário';ident=uuid.uuid4().hex[:12]
        pdf=gerar_pdf(data,loja,username,ident);saved=True
        filename=datetime.now(ZoneInfo('America/Sao_Paulo')).strftime('%Y%m%d-%H%M%S')+'-'+data['modo']+'-'+ident+'.pdf'
        path=pasta(data['filial'])+'/'+filename
        try:
            manuais.storage('POST','/object'+manuais.object_path(path),pdf,len(pdf.getbuffer()),pdf=True)
        except manuais.StorageError:
            saved=False;app.logger.warning('Cópia do relatório de checklist não salva no Storage. Relatório %s.',ident)
        pdf.seek(0)
        download_name='Checklist-'+(secure_filename(data['filial']) or 'loja')+'-'+filename
        response=send_file(pdf,mimetype='application/pdf',as_attachment=True,download_name=download_name)
        response.headers['Cache-Control']='no-store';response.headers['X-Checklist-Saved']='1' if saved else '0'
        response.headers['X-Checklist-Report']=filename
        return response

    @app.route('/api/checklists/relatorios',methods=['GET','DELETE'])
    @login_required
    @site_required('expansao')
    def api_checklist_relatorios():
        if request.method=='DELETE':
            err=autorizado()
            if err:return err
            try:filial=filial_param()
            except ValueError as exc:return jsonify(erro=str(exc)),400
            filename=request.args.get('arquivo','')
            if not ARQUIVO.fullmatch(filename):return jsonify(erro='Relatório inválido.'),400
            manuais.storage('DELETE','/object/'+quote(manuais.config()[2],safe=''),{'prefixes':[pasta(filial)+'/'+filename]})
            return jsonify(ok=True)
        try:
            filial=filial_param();page=max(1,min(10000,int(request.args.get('pagina',1))))
        except ValueError as exc:return jsonify(erro=str(exc) or 'Página inválida.'),400
        records=manuais.storage('POST','/object/list/'+quote(manuais.config()[2],safe=''),{'prefix':pasta(filial),'limit':21,'offset':(page-1)*20,'sortBy':{'column':'name','order':'desc'}})
        if not isinstance(records,list):raise manuais.StorageError('Não foi possível listar os relatórios.')
        items=[]
        for r in records[:20]:
            if not isinstance(r,dict):continue
            name=str(r.get('name',''))
            if name.startswith(pasta(filial)+'/'):name=name[len(pasta(filial))+1:]
            match=ARQUIVO.fullmatch(name)
            if match:items.append({'nome':name,'tipo':match.group(1),'criado_em':r.get('created_at'),'tamanho':(r.get('metadata') or {}).get('size',0)})
        response=jsonify(itens=items,pagina=page,proxima=len(records)>20)
        response.headers['Cache-Control']='no-store';return response

    @app.route('/api/checklists/relatorios/abrir')
    @login_required
    @site_required('expansao')
    def api_checklist_abrir():
        try:filial=filial_param()
        except ValueError as exc:return jsonify(erro=str(exc)),400
        filename=request.args.get('arquivo','')
        if not ARQUIVO.fullmatch(filename):return jsonify(erro='Relatório inválido.'),400
        signed=manuais.storage('POST','/object/sign'+manuais.object_path(pasta(filial)+'/'+filename),{'expiresIn':300})
        path=signed.get('signedURL') or signed.get('signedUrl')
        if not path or not path.startswith('/object/sign/'):raise manuais.StorageError('O armazenamento não retornou um link válido.')
        target=manuais.config()[0]+'/storage/v1'+path
        if request.args.get('baixar')=='1':target+='&download='+quote('Checklist-'+(secure_filename(filial) or 'loja')+'-'+filename,safe='')
        response=redirect(target);response.headers['Cache-Control']='no-store';response.headers['Referrer-Policy']='no-referrer';return response
