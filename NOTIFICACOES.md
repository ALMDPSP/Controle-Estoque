# Notificações automáticas — Resumo semanal

## 1. Frequência
O sistema envia **um resumo semanal consolidado**, por padrão **segunda-feira às 08:00 (horário de São Paulo / UTC-3)**.

O conteúdo inclui:
- pendências vencidas;
- pendências que vencem hoje ou nos próximos 7 dias;
- pendências de prioridade crítica;
- lojas com status **PENDENTE** no Acompanhamento de Expansão;
- para cada loja: Filial, nome, UF, **Entrada de TI** e **Data de Inauguração**.

Variáveis da agenda:
- `PENDENCIA_ALERT_DAYS=7`
- `PENDENCIA_WEEKLY_WEEKDAY=0` — 0=segunda, 1=terça ... 6=domingo
- `PENDENCIA_WEEKLY_HOUR=8`
- `PENDENCIA_WEEKLY_UTC_OFFSET=-3`

## 2. Contatos
Em **Gestão de Usuários**, informe E-mail e WhatsApp de cada responsável. O responsável recebe as pendências atribuídas a ele, junto com a visão de lojas pendentes.

Destinatários gerais podem ser definidos com:
- `PENDENCIA_ALERT_EMAILS`
- `PENDENCIA_ALERT_WHATSAPP`

Destinatários gerais recebem todas as pendências elegíveis e todas as lojas pendentes.

## 3. E-mail
- `SMTP_HOST`
- `SMTP_PORT=587`
- `SMTP_FROM`
- `SMTP_USER`
- `SMTP_PASSWORD`
- `SMTP_USE_TLS=1`
- `SMTP_USE_SSL=0`

## 4. WhatsApp
### Opção A — Webhook/integrador
- `WHATSAPP_WEBHOOK_URL`
- `WHATSAPP_WEBHOOK_TOKEN` (opcional)

### Opção B — API de WhatsApp
- `WHATSAPP_API_URL`
- `WHATSAPP_TOKEN`
- `WHATSAPP_TEMPLATE_NAME` (opcional)
- `WHATSAPP_TEMPLATE_LANG=pt_BR` (opcional)

Quando o resumo ultrapassa o limite de uma mensagem, o sistema divide o WhatsApp em partes numeradas.

## 5. Automação
O web service verifica periodicamente se chegou a janela semanal:
- `PENDENCIA_CHECK_INTERVAL_SECONDS=3600`

Para maior garantia, configure um Cron Job para segunda-feira às **11:00 UTC** (08:00 em São Paulo), executando:

`python notify_pendencias.py`

Alternativa HTTP:
- `NOTIFICATION_CRON_TOKEN`
- endpoint `/tasks/notificar-pendencias`
- header `X-Notification-Token`

O fingerprint semanal impede duplicidade: cada canal/destinatário recebe no máximo um resumo normal por semana.

## 6. Auditoria
Os resumos enviados ficam registrados em `pendencia_notificacoes` com o gatilho **RESUMO SEMANAL** e aparecem nos relatórios da Central.

## 7. Envio manual por usuário
Na aba **Gestão de Usuários**, o Administrador pode usar o botão **Enviar resumo** na linha de cada usuário.

O envio pode ser feito por:
- E-mail
- WhatsApp
- Ambos

O resumo manual usa os contatos cadastrados do próprio usuário, inclui as pendências em que ele está definido como responsável que estejam vencidas, críticas ou dentro da janela de prazo, e também inclui a relação geral de lojas PENDENTES do Acompanhamento de Expansão com datas de Entrada de TI e Inauguração.

Esse disparo é independente da agenda semanal e é registrado para auditoria em `pendencia_notificacoes` e no histórico do sistema.
