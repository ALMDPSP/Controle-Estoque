# Controle de Estoque — v99

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


## Agente IA com fallback gratuito (v75 - Groq → Gemini → Cloudflare)

- Aba **Agente IA** integrada ao menu do sistema.
- Ordem automática: **Groq → Gemini → Cloudflare Workers AI**.
- Quando um provedor atinge limite, falha ou fica indisponível, o sistema tenta o próximo.
- Não depende de Ollama, servidor próprio nem computador ligado.
- O agente permanece somente leitura e respeita as mesmas permissões por perfil.
- A interface informa a rota configurada, o provedor que respondeu e quando houve fallback.

### Variáveis de ambiente

- `GROQ_API_KEY` (principal)
- `GEMINI_API_KEY` (fallback 1)
- `CLOUDFLARE_ACCOUNT_ID` + `CLOUDFLARE_API_TOKEN` (fallback 2)
- `GROQ_MODEL`, `GEMINI_MODEL` e `CLOUDFLARE_MODEL` são opcionais.

Consulte `AGENTE_IA_CONFIGURACAO.txt` para o passo a passo.

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
