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
