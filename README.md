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

## v102 — Notificações automáticas da Central de Pendências

A Central de Pendências pode enviar alertas de **vencidas**, **próximas do prazo** e **prioridade crítica** por e-mail e WhatsApp. O responsável recebe nos contatos cadastrados em **Gestão de Usuários**. Também podem ser definidos destinatários gerais de contingência.

### E-mail (SMTP)
Configure no Render/Koyeb:
- `SMTP_HOST`
- `SMTP_PORT` (padrão 587)
- `SMTP_FROM`
- `SMTP_USER` (quando exigido)
- `SMTP_PASSWORD` (quando exigido)
- `SMTP_USE_TLS=1` (padrão) ou `SMTP_USE_SSL=1` para SMTP SSL
- `PENDENCIA_ALERT_EMAILS` (opcional, lista separada por vírgula/ponto e vírgula)

### WhatsApp
Há duas formas suportadas:
1. Webhook/integrador: `WHATSAPP_WEBHOOK_URL` e opcional `WHATSAPP_WEBHOOK_TOKEN`.
2. API de WhatsApp: `WHATSAPP_API_URL`, `WHATSAPP_TOKEN` e opcionalmente `WHATSAPP_TEMPLATE_NAME` / `WHATSAPP_TEMPLATE_LANG`.

Use `PENDENCIA_ALERT_WHATSAPP` (opcional) para números gerais de contingência, com DDI+DDD+número.

### Regras e agendamento
- `PENDENCIA_ALERT_DAYS=3`: quantidade de dias para considerar “próxima do prazo”.
- `PENDENCIA_CHECK_INTERVAL_SECONDS=3600`: checagem oportunista enquanto o web service está ativo.
- O sistema limita automaticamente a uma notificação por pendência/canal/destinatário/dia, evitando spam.
- Para execução garantida mesmo sem tráfego, crie um Cron Job usando `python notify_pendencias.py` com as mesmas variáveis do web service.
- Alternativamente, configure `NOTIFICATION_CRON_TOKEN` e chame `/tasks/notificar-pendencias` enviando o token em `X-Notification-Token`.
- `APP_PUBLIC_URL` (opcional) inclui o link da Central no corpo das mensagens.

O histórico de notificações é persistido em `pendencia_notificacoes`, aparece nos detalhes da pendência e também no relatório Excel da Central.


## v103
- Padronização visual das abas Acompanhamento de Expansão e Cockpit de Implantação com o layout geral do sistema.
- Créditos padronizados para “Developed by ALM - Expansão de TI” em telas e relatórios.
