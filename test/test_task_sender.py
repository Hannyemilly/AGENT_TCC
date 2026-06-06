# test_task_sender.py
import uuid
from worker import process_ai_request

def send_test_task():
    """
    Simula um "Gateway de Chat" enviando uma tarefa para o AI Agent.
    """
    print("Preparando para enviar uma tarefa de teste...")

    # Cole a sua URL única do webhook.site aqui!
    # A resposta do AI Agent aparecerá na página daquele site.
    callback_url = "https://webhook.site/81c02bd1-7946-4abc-814c-3b0537ed1e4a"

    # Este é o payload da tarefa, imitando o que um Gateway real enviaria.
    job_payload = {
        "task_id": str(uuid.uuid4()),
        "session_id": str(uuid.uuid4()),
        "user_id": "tarick",
        "user_input": "Olá, qual a previsão do tempo para Uberlândia hoje, sexta-feira?",
        "callback_details": {
            "webhook_url": callback_url,
            "addressing_info": { 
                "channel": "manual_test",
                "notes": "Este é um teste manual do worker."
            }
        }
    }

    try:
        process_ai_request(job_payload)
        print("✅ Tarefa enviada para a fila com sucesso!")
        print(f"Aguarde o processamento e verifique a resposta em: {callback_url}")
    except Exception as e:
        print(f"❌ Falha ao enviar a tarefa para a fila: {e}")
        print("Verifique se o seu servidor Redis está rodando.")

if __name__ == "__main__":
    send_test_task()