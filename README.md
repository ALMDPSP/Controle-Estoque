# Controle de Estoque — v109

Atualização: Loja Virtual 3D com **Gaveta - Horizontal**, **Display tela cliente** e **Teclado Automação TC55 teclas Gertec PS2** movidos do Balcão de atendimento para a **Frente de PDV**.

Mantém a melhoria da v98: aba **Gestão de Usuários** com data e horário do último login concluído de cada usuário.

# Controle-Estoque
## Recursos de gestão adicionados

- Dashboard executivo em `/dashboard` com total de estoque, imobilizados, capacidade de abertura de lojas, alertas e movimentações recentes.
- Pesquisa global por código, descrição, serial, patrimônio, localização, pedido e chamado.
- Histórico geral de auditoria em `/historico`, com filtros por origem, tipo e período.
- Central de relatórios em `/relatorios`, incluindo estoque, imobilizados, consolidado, movimentações por período e backup.
- Backup em ZIP com planilha consolidada e, quando local/SQLite, cópia do banco `estoque.db`.
- Perfis de acesso:
  - `admin`: acesso total e gestão de usuários.
  - `gestor`: movimentação de estoque e gestão de cadastro mestre/Kit padrão.
  - `operador`: entrada, saída e manutenção operacional de estoque e imobilizados.
  - `consulta`: somente leitura.
  - `user`: mantido por compatibilidade e tratado como operador.
- Loja Virtual 3D integrada ao menu, com foco por área e modo para destacar faltantes.

## Acesso pelo celular

A interface é responsiva e o servidor local é iniciado em `0.0.0.0:5000`, permitindo acesso de dispositivos na mesma rede. Ao executar `python app.py`, o terminal informa o IP local. No celular, conectado ao mesmo Wi-Fi, abra `http://IP-DO-PC:5000`.

O menu do sistema inclui a página **Celular**, que mostra o endereço de acesso e instruções para instalar o sistema na tela inicial como PWA. Em hospedagem pública (por exemplo, Render), basta usar no celular a mesma URL HTTPS do computador.


- Perfis de acesso:
  - `admin`: acesso total, gestão de usuários e dados.
  - `gestor`: movimentação de estoque e gestão de cadastro mestre/Kit padrão; pode alterar PEPI e custos usados no Orçamento.
  - `operador`: entrada, saída e manutenção operacional de estoque e imobilizados; Orçamento em visualização.
  - `consulta`: somente leitura nas áreas operacionais; Orçamento, Relatórios e Gestão de Dados bloqueados.
  - `user`: mantido por compatibilidade e tratado como operador.
- Orçamento:
  - acesso liberado para Administrador, Gestor e Operador;
  - perfil Consulta bloqueado; Administrador, Gestor e Operador podem consultar/editar conforme as permissões atuais.

## Permissões v69
- Administrador: acesso total, incluindo Gestão de Dados, Orçamento e Relatórios.
- Gestor: acesso operacional completo e relatórios; Gestão de Dados bloqueada.
- Operador: acesso operacional completo e relatórios; Gestão de Dados bloqueada.
- Consulta: somente leitura nas áreas operacionais; Orçamento, Relatórios, downloads/exports/backup e Gestão de Dados bloqueados.


## Agente IA — Gemini → Cloudflare (v115)

- Aba **Agente IA** integrada ao menu do sistema.
- Ordem automática: **Gemini → Cloudflare Workers AI**.
- O Groq foi retirado do fluxo para evitar interrupções frequentes por limite/depreciação de modelo.
- Quando o Gemini falha, atinge limite ou fica indisponível, o sistema tenta o Cloudflare.
- Não depende de Ollama, servidor próprio nem computador ligado.
- O agente permanece somente leitura e respeita as mesmas permissões por perfil.
- A interface informa a rota configurada, o provedor que respondeu e quando houve fallback.

### Variáveis de ambiente

- `GEMINI_API_KEY` (principal)
- `CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_API_TOKEN` (fallback)
- `GEMINI_MODEL` e `CLOUDFLARE_MODEL` são opcionais.
- `GROQ_API_KEY` e `GROQ_MODEL` não são mais utilizados e podem ser removidos do Render.

## v100 — Cockpit de Implantação
- Nova aba Cockpit de Implantação com readiness por loja pendente.
- Readiness calculado por marcos: obra, envio, separação, equipamentos separados, Entrada de TI e inauguração.
- Cruzamento do Kit Padrão com Estoque de Expansão por ordem cronológica das lojas para apontar faltas por filial.
- Indicadores de lojas Prontas, Atenção e Críticas, bloqueios, unidades/itens faltantes e impacto financeiro estimado.
- Filtros por filial, situação, projeto e UF, com atalho para abrir a filial no Acompanhamento de Expansão.
- Relatórios completos Excel e PDF disponíveis no Cockpit e na Central de Relatórios.

## v101 — Central de Pendências e Ações
- Nova aba Central de Pendências e Ações integrada ao Cockpit de Implantação.
- Cadastro de ação por filial com responsável, prazo, prioridade e status.
- Comentários cronológicos e evidências com referência e arquivo (até 5 MB), persistidos no banco.
- Alertas automáticos para vencidas, vencendo hoje, próximas de 3 dias, críticas e sem responsável.
- Cockpit exibe ações abertas/vencidas por filial e atalho para criar ação vinculada à loja.
- Relatórios completos em Excel (Resumo, Pendências, Comentários, Evidências e Alertas) e PDF executivo no padrão do sistema.

## v104 — Resumo semanal por E-mail e WhatsApp
A Central envia um **resumo semanal consolidado** (padrão: segunda-feira às 08:00, UTC-3) com pendências vencidas, críticas e que vencem nos próximos 7 dias. O mesmo resumo inclui as **lojas PENDENTES do Acompanhamento de Expansão**, com **Entrada de TI** e **Data de Inauguração**.

Configuração principal: `PENDENCIA_ALERT_DAYS=7`, `PENDENCIA_WEEKLY_WEEKDAY=0`, `PENDENCIA_WEEKLY_HOUR=8`, `PENDENCIA_WEEKLY_UTC_OFFSET=-3`, além das credenciais SMTP/WhatsApp descritas em `NOTIFICACOES.md`. O sistema registra o envio e evita duplicidade na mesma semana.

## v103
- Padronização visual das abas Acompanhamento de Expansão e Cockpit de Implantação com o layout geral do sistema.
- Créditos padronizados para “Developed by ALM - Expansão de TI” em telas e relatórios.

## v105 — envio manual na Gestão de Usuários
- Botão **Enviar resumo** por usuário.
- Seleção de canal: E-mail, WhatsApp ou Ambos.
- Usa os contatos cadastrados em Gestão de Usuários.
- Mantém no resumo as pendências do responsável e as lojas pendentes com Entrada de TI/Inauguração.
- Envio manual auditado sem alterar o agendamento semanal.


## v106 — Correção do envio manual 'Ambos'
- Na Gestão de Usuários, o botão **Ambos** agora executa dois envios independentes: um por E-mail e outro por WhatsApp, reutilizando os mesmos fluxos individuais.
- Um canal não bloqueia o outro em caso de falha e a interface informa sucesso/falha por canal.
- O botão **Ambos** só fica habilitado quando o usuário possui E-mail e WhatsApp cadastrados.


## v108 — Remoção da aba Central de Pendências
- A aba **Central de Pendências e Ações** foi removida do menu e da Central de Relatórios.
- O **Cockpit de Implantação** não exibe mais contadores/atalhos da Central; mantém acesso direto ao Acompanhamento de Expansão.
- A URL antiga `/central-pendencias` redireciona para o Cockpit.
- As tabelas e registros históricos de pendências **não são apagados do banco**, permitindo recuperação futura sem perda de dados.
- O backend de notificações e o histórico existente foram preservados para evitar exclusão destrutiva de dados/configurações.


## v109 — Desativação de E-mail e WhatsApp
- Removidos os botões e fluxos de envio manual por E-mail/WhatsApp da Gestão de Usuários.
- Desativados os processamentos automáticos, resumo semanal, endpoints e Cron de notificações externas.
- Os campos **E-mail** e **WhatsApp** continuam disponíveis para cadastro e edição na Gestão de Usuários.
- Os dados de contato e o histórico antigo de notificações permanecem preservados no banco.

## Atualização v110
No Cockpit de Implantação, a data de Entrada TI continua visível, mas o contador de dias foi removido apenas desse campo. O contador de Inauguração permanece inalterado.


## v111 — Bloqueios focados na inauguração
- No Cockpit de Implantação, a coluna de bloqueios mostra somente riscos que podem comprometer a data de inauguração.
- Considera data de inauguração ausente/vencida, obra, Entrada TI, equipamentos e estoque quando representarem risco à inauguração.


## v112 — Padronização visual e de ações
- Layout de cabeçalho padronizado nas abas operacionais.
- Barra de ações única para Relatório Excel, Relatório PDF e Importar Excel.
- Estoque, Imobilizados, Filiais, Projeção, Acompanhamento, Cockpit e Orçamento alinhados ao mesmo padrão.
- Modais de importação usam os mesmos botões e nomenclatura.

## v113 — Baixa de estoque e consumo por filial
- No Estoque, a ação **Baixa de estoque** transforma a unidade em **Enviado** e zera a quantidade do saldo.
- A baixa exige **NF de saída, Data de saída, Filial/destino, Nº imobilizado, Nº série e Nº patrimônio**.
- O envio em massa para status Enviado é bloqueado para preservar os identificadores únicos de cada unidade.
- A aba **Filiais > Abrir** cruza os itens enviados com o **Kit padrão da loja** e mostra status Completo, Parcial ou Pendente.
- A ficha da filial passa a exibir também o espelho completo dos dados do grid de Estoque vinculados à filial.
- O registro baixado permanece no banco para histórico e auditoria, mas deixa de compor o saldo disponível do Estoque.


## v115 — Parque de Filiais e consumo na inauguração
- Na aba **Filiais**, filiais inativas passam a exibir o **Kit padrão = OK** por regra de negócio, mantendo as quantidades reais para rastreabilidade.
- A visão de Filiais mostra **Equipamentos no parque**, **Tipos de equipamento** e quantidade unitária por filial.
- Em **Abrir filial**, foi adicionado o resumo do parque por código/equipamento.
- Quando uma loja passa para **INAUGURADA** no Acompanhamento, o sistema consome somente a diferença do Kit ainda não baixada, usando Estoque Expansão disponível.
- A baixa de inauguração é idempotente: itens já enviados à filial não são descontados novamente. Se faltar estoque, a inauguração é mantida e a falta é informada/auditada.
