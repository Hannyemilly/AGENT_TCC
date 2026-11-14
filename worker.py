# worker.py
import logging
import os
import requests
from requests.exceptions import RequestException
import dramatiq
from dramatiq.brokers.redis import RedisBroker

from services.orchestration.orchestrator import Orchestrator
from services.logging.execution_logger import execution_logger

# 1. Configuração do Broker do Dramatiq
# Aponta para o mesmo Redis que usávamos antes.
redis_broker = RedisBroker(url=os.getenv("REDIS_URL", "redis://localhost:6379/0"))
dramatiq.set_broker(redis_broker)

# Configuração do logger
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

orchestrator = Orchestrator()

# Sem novas tentativas automáticas: cada tentativa refazia a pergunta inteira, mandava
# outra mensagem de falha ao aluno, gravava a pergunta de novo e gastava a cota do Gemini.
@dramatiq.actor(max_retries=0, time_limit=600000) # Timeout de 10 minutos
def process_ai_request(job_payload: dict):
    """
    Esta é a tarefa assíncrona que o worker do Dramatiq executará.
    Dramatiq lida com retries automaticamente quando uma exceção é levantada.
    """
    task_id = job_payload.get("task_id", "N/A")
    logger.info(f"Iniciando processamento da tarefa: {task_id}")

    final_result = None
    status = "completed"
    try:
        final_result = orchestrator.process_task_sync(job_payload)

    except Exception as e:
        logger.exception(f"Erro CRÍTICO ao processar a tarefa {task_id}: {e}")
        status = "failed"
        # Registra a execução com status "failed" (antes ela se perdia e o painel
        # nunca mostrava falhas em Perguntas Não Respondidas)
        try:
            execution_logger.finalize_execution_log(job_payload.get("session_id"), status="failed")
        except Exception as log_error:
            logger.error(f"Não foi possível registrar a falha da tarefa {task_id}: {log_error}")
        raise e

    finally:

        webhook_url = job_payload.get("callback_details", {}).get("webhook_url")
        if webhook_url:
            # Monta o payload do callback
            callback_payload = {
                "task_id": task_id,
                "status": status,
                "addressing_info": job_payload.get("callback_details", {}).get("addressing_info")
            }
            if final_result:
                callback_payload["final_output"] = final_result.get("response", "Nenhuma resposta gerada.")
            else:
                callback_payload["final_output"] = "A tarefa falhou após todas as tentativas."
            
            try:
                logger.info(f"Enviando callback para a tarefa {task_id} para a URL: {webhook_url}")
                requests.post(webhook_url, json=callback_payload, timeout=15)
            except RequestException as re:
                logger.error(f"Falha CRÍTICA ao enviar o callback para a tarefa {task_id}: {re}")
        else:
            logger.warning(f"Nenhuma webhook_url encontrada para a tarefa {task_id}.")