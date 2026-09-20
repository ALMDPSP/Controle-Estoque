"""Execução agendada dos alertas da Central de Pendências.
Use em um Cron Job com as mesmas variáveis de ambiente da aplicação.
"""
import json
from app import _processar_notificacoes_pendencias

if __name__ == '__main__':
    resultado = _processar_notificacoes_pendencias()
    print(json.dumps(resultado, ensure_ascii=False))
