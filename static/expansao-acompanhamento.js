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
  const query=new URLSearchParams(Object.entries(filtrosExpansao()).filter(([,v])=>v)).toString();
  document.querySelectorAll('a[href^="/export-acompanhamento-expansao"],a[href^="/pdf-acompanhamento-expansao"]').forEach(a=>{a.href=a.getAttribute('href').split('?')[0]+(query?'?'+query:'');});
  const month=document.getElementById('f-mes-inauguracao');
  document.getElementById('exp-escopo').textContent=`${linhas.length} de ${dados.length} filiais · ${month.options[month.selectedIndex]?.textContent||'Todos os meses'}`;
}
document.getElementById('exp-limpar').addEventListener('click',()=>{document.querySelectorAll('.exp-filtros input,.exp-filtros select').forEach(el=>el.value='');selecionadas.clear();renderTable();});
