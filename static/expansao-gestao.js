// Extensão do Acompanhamento: utiliza os mesmos registros e filtros da tabela.
function filtrosExpansao(){return Object.fromEntries([['busca','f-busca'],['status','f-status'],['projeto','f-projeto'],['bandeira','f-bandeira'],['uf','f-uf'],['mes','f-mes-inauguracao']].map(([k,id])=>[k,document.getElementById(id).value]));}
function resumoExpansao(linhas){
  const d={status:{},projetos:{},bandeiras:{},ufs:{}};
  const s={total:linhas.length,inauguradas:0,pendentes:0,pendentes_com_inauguracao_definida:0,etapas:{obra:{qtd:0},ti:{qtd:0},inauguracao:{qtd:0}},ultima_atualizacao:''};
  for(const r of linhas){
    for(const [k,c] of [['status','status_filial'],['projetos','projeto'],['bandeiras','bandeira'],['ufs','uf']]){const v=String(r[c]||'SEM '+c).toUpperCase();d[k][v]=(d[k][v]||0)+1;}
    const concluida=norm(r.status_filial)==='inaugurada',aberta=statusEmAcompanhamento(r.status_filial);
    if(concluida)s.inauguradas++;if(aberta)s.pendentes++;
    if(aberta&&dataDefinida(r.inauguracao))s.pendentes_com_inauguracao_definida++;
    for(const [k,c] of [['obra','term_obra'],['ti','entrada_ti'],['inauguracao','inauguracao']])if(concluida||dataDefinida(r[c]))s.etapas[k].qtd++;
    if((r.atualizado_em||'')>s.ultima_atualizacao)s.ultima_atualizacao=r.atualizado_em;
  }
  const base=Math.max(1,s.total);s.ufs=new Set(linhas.map(r=>r.uf).filter(Boolean)).size;s.entrega_realizada_pct=s.inauguradas/base*100;s.alcance_projetado=s.etapas.inauguracao.qtd;s.alcance_projetado_pct=s.alcance_projetado/base*100;s.pendentes_sem_inauguracao_definida=s.pendentes-s.pendentes_com_inauguracao_definida;
  for(const e of Object.values(s.etapas))e.pct=e.qtd/base*100;
  d.ufs=Object.fromEntries(Object.entries(d.ufs).sort((a,b)=>b[1]-a[1]));d.resumo=s;return d;
}
function atualizarPainelExpansao(linhas){
  pintarResumo(resumoExpansao(linhas),linhas);
  if(typeof opRender==='function')opRender(linhas);
  const filtros=filtrosExpansao(),query=new URLSearchParams(Object.entries(filtros).filter(([,v])=>v)).toString();
  document.querySelectorAll('a[href^="/export-acompanhamento-expansao"],a[href^="/pdf-acompanhamento-expansao"]').forEach(a=>{a.href=a.getAttribute('href').split('?')[0]+(query?'?'+query:'');});
  document.getElementById('exp-pdf').href='/api/acompanhamento-expansao/resumo-executivo.pdf'+(query?'?'+query:'');
  const month=document.getElementById('f-mes-inauguracao');document.getElementById('exp-escopo').textContent=`${linhas.length} de ${dados.length} filiais · ${month.options[month.selectedIndex]?.textContent||'Todos os meses'}`;
  const abertas=linhas.filter(r=>statusEmAcompanhamento(r.status_filial)),gs=abertas.map(r=>gestao[String(r.filial)]).filter(Boolean);
  document.getElementById('exp-metricas').innerHTML=[['No prazo',gs.filter(g=>g.situacao==='No prazo').length],['Em risco',gs.filter(g=>g.situacao==='Em risco').length],['Atrasadas',gs.filter(g=>g.situacao==='Atrasada').length],['Unidades faltantes',gs.reduce((v,g)=>v+g.faltantes,0)]].map(([k,v])=>`<div class="exp-metrica"><b>${v}</b><span>${k}</span></div>`).join('');
  const prioridade=r=>parseDataBR(r.entrada_ti)??parseDataBR(r.inauguracao)??Infinity;
  const ordenadas=[...linhas].sort((a,b)=>prioridade(a)-prioridade(b)||String(a.filial).localeCompare(String(b.filial)));
  const alertas=ordenadas.filter(r=>['Atrasada','Em risco'].includes(gestao[String(r.filial)]?.situacao));
  document.getElementById('exp-alertas').innerHTML=alertas.map(r=>{const g=gestao[String(r.filial)],p=g.plano;return `<article class="exp-alerta ${g.situacao==='Atrasada'?'atrasada':''}"><div><h3>${esc(r.filial)} · ${esc(r.descricao_filial)}</h3><small>${esc(g.situacao)} · <span data-op-approval="${esc(r.filial)}">TI: ${esc(typeof opDados!=='undefined'?opDados[String(r.filial)]?.situacao_aprovacao||'Pendente':'Pendente')}</span> · Entrada TI: ${esc(dataLegivel(r.entrada_ti))}</small></div><div><p>${g.motivos.map(esc).join(' · ')}</p><small>Responsável: ${esc(p.responsavel||'Não definido')} · Envio: ${esc(dataLegivel(p.prazo_envio))}</small></div><button type="button" class="ui-btn ui-btn-secondary" data-gestao-id="${r.id}">Gerenciar</button></article>`;}).join('')||'<p class="exp-vazio">Nenhum alerta para os filtros selecionados.</p>';
  document.getElementById('exp-cronograma').innerHTML=ordenadas.filter(r=>statusEmAcompanhamento(r.status_filial)).map(r=>`<div class="exp-crono-row"><div><button type="button" class="edit-btn" data-gestao-id="${r.id}">${esc(r.filial)}</button><p class="panel-sub">${esc(r.descricao_filial)}</p></div><div class="exp-marcos">${[['Mobiliário','mobiliario'],['Término de obra','term_obra'],['Entrada de TI','entrada_ti'],['Inauguração','inauguracao']].map(([nome,k])=>`<div class="exp-marco ${dataDefinida(r[k])?'':'sem-data'}"><b>${nome}</b><span>${esc(dataLegivel(r[k]))}</span></div>`).join('')}</div></div>`).join('')||'<p class="exp-vazio">Nenhuma loja a inaugurar para os filtros selecionados.</p>';
}
const expDialog=document.getElementById('exp-dialog');let expAtual=null,expPlano=null,expBusy=false,expAbertura=0,expOrigem=null;
function expMensagem(text,erro=false){const el=document.getElementById('exp-dialog-status');el.textContent=text;el.className=erro?'exp-status-error':'exp-status-ok';}
async function expRequest(url,options={}){const r=await fetch(url,{...options,headers:{'Content-Type':'application/json','X-CSRF-Token':EDIT_CSRF,...options.headers}});const d=await respostaJsonSegura(r);if(!r.ok)throw new Error(d.erro||'Não foi possível concluir a operação.');return d;}
function textoHistorico(v){if(v===null||v===undefined||v==='')return 'Não definido';if(Array.isArray(v))return v.map(x=>x.etapa?`${x.etapa}: ${x.status} | ${x.responsavel||'sem responsável'} | ${x.prazo||'sem prazo'} | ${x.observacao||''}`:`Item ${x.item_id}: ${x.quantidade} unidade(s)`).join('; ');return typeof v==='object'?JSON.stringify(v):String(v);}
function pintarHistorico(eventos){
 const nomes={cadastro:'Cadastro da filial',alteracao:'Alteração da filial',exclusao:'Exclusão da filial',checklist:'Checklist e responsáveis',reserva:'Reserva de estoque',liberacao_reserva:'Liberação de reserva',reserva_consumida:'Envio de equipamento reservado',kit_filial_personalizado_insert:'Item incluído no kit',kit_filial_personalizado_update:'Item do kit alterado',kit_filial_personalizado_delete:'Item removido do kit',itens_insert:'Equipamento vinculado',itens_update:'Equipamento atualizado',itens_delete:'Equipamento removido'};
 const campos={status_filial:'Status',term_obra:'Término de obra',entrada_ti:'Entrada de TI',inauguracao:'Inauguração',mobiliario:'Mobiliário',responsavel:'Responsável',prazo_envio:'Prazo de envio',tarefas:'Etapas do checklist',observacao_ti:'Observação TI',equip_separado:'Equipamentos separados',em_separacao:'Em separação',link_rede:'Rede',qtde:'Quantidade'};
 document.getElementById('exp-historico').innerHTML=eventos.map(e=>{let items='';const a=e.antes||{},b=e.depois||{};
 if(Array.isArray(a)||Array.isArray(b))items=`<li>${esc(textoHistorico(a))} → ${esc(textoHistorico(b))}</li>`;
 else items=[...new Set([...Object.keys(a),...Object.keys(b)])].filter(k=>!['atualizado_em','atualizado_por','versao','id','site_area'].includes(k)&&JSON.stringify(a[k])!==JSON.stringify(b[k])).map(k=>`<li><b>${esc(campos[k]||k.replaceAll('_',' '))}:</b> ${esc(textoHistorico(a[k]))} → ${esc(textoHistorico(b[k]))}</li>`).join('');
 return `<details class="exp-history"><summary>${esc(nomes[e.tipo]||e.tipo)} · ${esc(e.usuario||'sistema')} · ${esc(new Date(e.criado_em).toLocaleString('pt-BR',{timeZone:'America/Sao_Paulo'}))}</summary><ul>${items||'<li>Registro atualizado.</li>'}</ul></details>`;}).join('')||'<p class="exp-vazio">Ainda não há alterações registradas para esta filial.</p>';
}
function pintarGestao(d){
 expPlano=d.plano;const g=gestao[String(expAtual.filial)]||{};
 document.getElementById('exp-responsavel').value=expPlano.responsavel||'';document.getElementById('exp-prazo').value=expPlano.prazo_envio||'';
 document.getElementById('exp-tarefas').innerHTML=expPlano.tarefas.map((t,i)=>`<div class="exp-tarefa" data-etapa="${i}"><strong>${esc(t.etapa)}</strong><label>Status<select data-campo="status">${['Pendente','Em andamento','Concluído','Não se aplica'].map(v=>`<option ${v===t.status?'selected':''}>${v}</option>`).join('')}</select></label><label>Responsável<input data-campo="responsavel" maxlength="120" value="${esc(t.responsavel)}"></label><label>Prazo<input type="date" data-campo="prazo" value="${esc(t.prazo)}"></label><label>Observação<textarea data-campo="observacao" maxlength="1000">${esc(t.observacao)}</textarea></label></div>`).join('');
 document.querySelectorAll('#exp-plano-form input,#exp-plano-form select,#exp-plano-form textarea').forEach(el=>el.disabled=!PODE_EDITAR);
 document.getElementById('exp-estoque').innerHTML=`<div class="exp-overflow"><table><thead><tr>${['Equipamento','Kit','Enviado','Reservado','Previsto','Falta'].map(x=>`<th>${x}</th>`).join('')}</tr></thead><tbody>${(g.equipamentos||[]).map(e=>`<tr><td>${esc(e.codigo)} · ${esc(e.descricao)}</td>${['necessario','enviado','reservado','previsto','falta'].map(k=>`<td>${e[k]}</td>`).join('')}</tr>`).join('')||'<tr><td colspan="6">Kit não cadastrado.</td></tr>'}</tbody></table></div>`;
 document.getElementById('exp-reservas-info').textContent=`Reservas confirmadas: ${(d.reservas||[]).map(r=>`ID ${r.item_id} (${r.quantidade} un.)`).join(', ')||'nenhuma'}. Registros previstos para reservar: ${(g.sugeridos||[]).join(', ')||'nenhum'}.`;
 if(PODE_EDITAR){document.getElementById('exp-reservar').disabled=!statusEmAcompanhamento(expAtual.status_filial)||!g.sugeridos?.length;document.getElementById('exp-liberar').disabled=!d.reservas?.length;}
 pintarHistorico(d.historico||[]);
}
async function abrirGestao(id){
 const row=dados.find(x=>Number(x.id)===Number(id));if(!row)return;
 expAtual=row;const seq=++expAbertura;expOrigem=document.activeElement;
 document.getElementById('exp-dialog-titulo').textContent=`${row.filial} · ${row.descricao_filial||'Filial'}`;
 document.getElementById('exp-plano-form').hidden=true;document.querySelectorAll('#exp-dialog .exp-dialog-section').forEach(e=>e.hidden=true);
 expMensagem('Carregando gestão da filial...');if(!expDialog.open)expDialog.showModal();
 try{const d=await expRequest(`/api/acompanhamento-expansao/${row.id}/gestao`);if(seq!==expAbertura)return;pintarGestao(d);await opAbrir(row.id,seq);if(seq!==expAbertura)return;document.getElementById('exp-plano-form').hidden=false;document.querySelectorAll('#exp-dialog .exp-dialog-section').forEach(e=>e.hidden=false);expMensagem('');}catch(e){expMensagem(e.message,true);}
}
function fecharGestao(){if(expBusy)return;expAbertura++;expDialog.close();expOrigem?.focus();}
document.addEventListener('click',e=>{const b=e.target.closest('[data-gestao-id]');if(b)abrirGestao(b.dataset.gestaoId);});
document.getElementById('exp-fechar').addEventListener('click',fecharGestao);expDialog.addEventListener('cancel',e=>{e.preventDefault();fecharGestao();});
document.getElementById('exp-limpar').addEventListener('click',()=>{document.querySelectorAll('.exp-filtros input,.exp-filtros select').forEach(el=>el.value='');selecionadas.clear();renderTable();});
async function executarGestao(fn,msg){if(expBusy)return;expBusy=true;document.querySelectorAll('#exp-dialog button').forEach(b=>b.disabled=true);try{await fn();await init();await opCarregar();await abrirGestao(expAtual.id);expMensagem(msg);}catch(e){expMensagem(e.message,true);}finally{expBusy=false;document.getElementById('exp-fechar').disabled=false;document.getElementById('exp-salvar')?.removeAttribute('disabled');document.querySelectorAll('#op-dialog-section button').forEach(b=>b.disabled=false);const g=gestao[String(expAtual.filial)];if(PODE_EDITAR){document.getElementById('exp-reservar').disabled=!g?.sugeridos?.length;document.getElementById('exp-liberar').disabled=!g?.reservas?.length;}}}
document.getElementById('exp-plano-form').addEventListener('submit',e=>{e.preventDefault();if(!PODE_EDITAR)return;const payload={versao:expPlano.versao,responsavel:document.getElementById('exp-responsavel').value,prazo_envio:document.getElementById('exp-prazo').value,tarefas:expPlano.tarefas.map((t,i)=>{const out={etapa:t.etapa};document.querySelectorAll(`[data-etapa="${i}"] [data-campo]`).forEach(el=>out[el.dataset.campo]=el.value);return out;})};executarGestao(()=>expRequest(`/api/acompanhamento-expansao/${expAtual.id}/gestao`,{method:'PUT',body:JSON.stringify(payload)}),'Checklist salvo.');});
document.getElementById('exp-reservar')?.addEventListener('click',()=>{const ids=gestao[String(expAtual.filial)]?.sugeridos||[];if(!ids.length||!confirm(`Confirmar reserva de ${ids.length} registro(s) de estoque para a filial ${expAtual.filial}?`))return;executarGestao(()=>expRequest(`/api/acompanhamento-expansao/${expAtual.id}/reservas`,{method:'POST',body:JSON.stringify({ids})}),'Reserva confirmada.');});
document.getElementById('exp-liberar')?.addEventListener('click',()=>{if(!confirm(`Liberar todas as reservas da filial ${expAtual.filial}?`))return;executarGestao(()=>expRequest(`/api/acompanhamento-expansao/${expAtual.id}/reservas`,{method:'DELETE'}),'Reservas liberadas.');});
