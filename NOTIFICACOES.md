# Notificações automáticas — Central de Pendências

## 1. Contatos dos responsáveis
Em **Gestão de Usuários**, informe E-mail e WhatsApp de cada usuário. Quando o nome desse usuário for definido como Responsável de uma pendência, esses contatos serão usados nos alertas.

Também é possível definir destinatários gerais com:
- `PENDENCIA_ALERT_EMAILS`
- `PENDENCIA_ALERT_WHATSAPP`

## 2. E-mail
Variáveis de ambiente:
- `SMTP_HOST`
- `SMTP_PORT=587`
- `SMTP_FROM`
- `SMTP_USER`
- `SMTP_PASSWORD`
- `SMTP_USE_TLS=1`
- `SMTP_USE_SSL=0`

## 3. WhatsApp
### Opção A — Webhook/integrador
- `WHATSAPP_WEBHOOK_URL`
- `WHATSAPP_WEBHOOK_TOKEN` (opcional)

O sistema envia JSON com: `to`, `message`, `pendencia_id` e `gatilhos`.

### Opção B — API de WhatsApp
- `WHATSAPP_API_URL`
- `WHATSAPP_TOKEN`
- `WHATSAPP_TEMPLATE_NAME` (opcional)
- `WHATSAPP_TEMPLATE_LANG=pt_BR` (opcional)

Se sua conta/provedor exigir template para mensagens iniciadas pela empresa, configure um template compatível ou utilize um webhook/integrador que faça esse tratamento.

## 4. Regras
- `PENDENCIA_ALERT_DAYS=3` — alerta de proximidade do prazo.
- Vencida: prazo anterior à data atual e status ainda aberto.
- Próxima do prazo: vence hoje ou dentro de `PENDENCIA_ALERT_DAYS`.
- Crítica: prioridade `CRITICA` enquanto não estiver concluída/cancelada.
- Limite normal: no máximo 1 envio por pendência/canal/destinatário por dia.

## 5. Automação
O web service faz checagem oportunista em segundo plano enquanto estiver ativo:
- `PENDENCIA_CHECK_INTERVAL_SECONDS=3600`

Para execução garantida mesmo sem tráfego, crie um Cron Job com o comando:

`python notify_pendencias.py`

O Cron deve usar as mesmas variáveis de ambiente do web service.

Como alternativa HTTP:
- configure `NOTIFICATION_CRON_TOKEN`
- chame `/tasks/notificar-pendencias`
- envie o token no header `X-Notification-Token`

## 6. Auditoria
Os envios bem-sucedidos ficam gravados na tabela `pendencia_notificacoes`, aparecem nos detalhes da pendência e nos relatórios Excel/PDF da Central.
