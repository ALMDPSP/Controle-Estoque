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
