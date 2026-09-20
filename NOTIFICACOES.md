# Comunicações externas — desativadas na v109

Os envios automáticos e manuais por **e-mail** e **WhatsApp** foram desativados.

## O que foi mantido
- Campos **E-mail** e **WhatsApp** na Gestão de Usuários.
- Dados já cadastrados no banco.
- Histórico/tabelas antigas de notificações, sem exclusão de registros.

## O que não é mais executado
- Envio manual pela Gestão de Usuários.
- Resumo semanal.
- SMTP.
- Webhook/API de WhatsApp.
- Cron Job de notificações.

As antigas variáveis SMTP/WhatsApp no Render podem ser removidas posteriormente; a aplicação não as utiliza para novos envios.
