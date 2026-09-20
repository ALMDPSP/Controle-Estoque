"""Execução agendada do resumo semanal da Central de Pendências.
Recomendado: segunda-feira às 11:00 UTC (08:00 em São Paulo).
Use as mesmas variáveis de ambiente da aplicação.
"""
import json
from app import _processar_notificacoes_pendencias

if __name__ == '__main__':
    resultado = _processar_notificacoes_pendencias()
    print(json.dumps(resultado, ensure_ascii=False))
