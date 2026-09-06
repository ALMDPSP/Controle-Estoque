# Controle de Ativos — Koyeb + Neon (v47)

Esta versão foi preparada para retirar a aplicação do Render e usar:

- **Koyeb**: aplicação Flask/Gunicorn
- **Neon**: banco PostgreSQL persistente
- **GitHub**: código-fonte e deploy automático

## Fluxo recomendado para não perder dados

1. **Não desligue o Render ainda.**
2. Crie uma conta/projeto no **Neon**.
3. Migre o PostgreSQL atual do Render para o Neon.
4. Valide os dados no Neon.
5. Publique esta versão no GitHub.
6. Crie o serviço no Koyeb e configure as variáveis.
7. Teste login, MFA, estoque, filiais, expansão, relatórios e backup.
8. Somente depois desative o Render.

---

## 1. Criar o banco no Neon

No painel do Neon, crie um projeto PostgreSQL. Na tela **Connect**, copie preferencialmente a **Pooled connection string**. Ela deve se parecer com:

```text
postgresql://usuario:senha@ep-xxxxx-pooler.regiao.aws.neon.tech/neondb?sslmode=require
```

Guarde essa URL como `TARGET_DATABASE_URL`. Nunca publique a senha no GitHub.

---

## 2. Migrar o banco atual do Render

### Opção recomendada — Neon Import Data Assistant

No Render, abra o PostgreSQL atual e copie a **External Database URL**. No Neon, use **Import Database / Import Data Assistant**, cole a URL externa do Render e execute a importação.

Esse método preserva tabelas e dados, incluindo usuários, hashes de senha, configurações de MFA, estoque, imobilizados, filiais, histórico e acompanhamento de expansão.

### Opção alternativa — pg_dump / pg_restore

Foram incluídos dois scripts:

- `scripts/migrar_render_para_neon.ps1` — Windows PowerShell
- `scripts/migrar_render_para_neon.sh` — Linux/macOS

Windows:

```powershell
.\scripts\migrar_render_para_neon.ps1 `
  -SourceDatabaseUrl "URL_EXTERNA_DO_RENDER" `
  -TargetDatabaseUrl "URL_DO_NEON"
```

É necessário ter as ferramentas do PostgreSQL (`pg_dump`, `pg_restore`, `psql`) instaladas.

---

## 3. Validar o Neon

Antes de migrar a aplicação, confirme no Neon que existem as tabelas principais:

```sql
SELECT COUNT(*) FROM usuarios;
SELECT COUNT(*) FROM itens;
SELECT COUNT(*) FROM imobilizados;
SELECT COUNT(*) FROM filiais;
SELECT COUNT(*) FROM acompanhamento_expansao;
```

O ideal é comparar as quantidades com o banco do Render.

---

## 4. Subir esta versão para o GitHub

O arquivo `Procfile` já está preparado para o Koyeb:

```text
web: gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120 app:app
```

O Koyeb define `PORT` automaticamente.

---

## 5. Criar o serviço no Koyeb

No Koyeb:

1. **Create App / Web Service**.
2. Escolha **GitHub**.
3. Selecione o repositório e a branch `main`.
4. Builder: **Buildpack**.
5. Build command (se o painel pedir):

```text
pip install -r requirements.txt
```

6. Run command pode ficar pelo `Procfile`; se precisar preencher manualmente:

```text
gunicorn --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120 app:app
```

7. Exponha a porta HTTP definida pelo Koyeb e a rota `/`.
8. Health check opcional: `/health`.

---

## 6. Variáveis de ambiente no Koyeb

Cadastre como secrets/variáveis:

| Variável | Valor |
|---|---|
| `DATABASE_URL` | Pooled connection string do Neon |
| `SECRET_KEY` | Chave aleatória longa |
| `SESSION_COOKIE_SECURE` | `1` |
| `REQUIRE_DATABASE_URL` | `1` |
| `ADMIN_USER` | `admin` ou outro usuário inicial |
| `ADMIN_PASS` | Senha temporária forte; necessária somente se o banco estiver vazio |

Para gerar uma `SECRET_KEY` segura:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

**Importante:** se você migrar o banco do Render, os usuários já existentes serão preservados. `ADMIN_USER` e `ADMIN_PASS` não substituem usuários existentes; são usados somente se a tabela de usuários estiver vazia.

---

## 7. Testes antes de desligar o Render

Teste no endereço `.koyeb.app`:

- Login
- Troca de senha
- MFA Microsoft/Google Authenticator
- Dashboard
- Estoque
- Imobilizados
- Cadastro de produtos
- Filiais
- Projeção de abertura
- Acompanhamento de Expansão
- Upload/Download Excel
- Relatórios PDF
- Gestão de Dados
- Backup

O endpoint `/health` deve responder algo como:

```json
{"status":"ok","build":"2026-09-06-koyeb-neon-v47"}
```

---

## 8. Corte final

Quando tudo estiver validado no Koyeb + Neon:

1. Evite alterações no sistema antigo durante o corte.
2. Se necessário, faça uma última migração do Render para o Neon.
3. Confirme os totais.
4. Passe o novo endereço do Koyeb para o time.
5. Só então desative o serviço/banco antigo do Render.

---

## Segurança aplicada nesta versão

- O Koyeb não pode iniciar silenciosamente com SQLite se `DATABASE_URL` estiver ausente.
- URLs Neon recebem `sslmode=require` caso ele não esteja presente.
- Cookie de sessão seguro é ativado automaticamente em hospedagem HTTPS.
- Banco PostgreSQL vazio exige `ADMIN_PASS`; não usa `admin123` em produção.
- `/health` não consulta o banco, evitando manter o compute do Neon ativo somente por health checks.

