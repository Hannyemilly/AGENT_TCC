"""
Renova o token JWT do RAG e atualiza todas as ferramentas no MongoDB.
Uso: python atualizar_token_rag.py
Requer que o RAG_TCC esteja rodando em http://localhost:3333
"""
import requests
import pymongo
from dotenv import load_dotenv
import os

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "ai_agents")
RAG_BASE_URL = os.getenv("RAG_BASE_URL", "http://localhost:3333")

def main():
    print("Gerando novo token RAG...")
    resp = requests.post(f"{RAG_BASE_URL}/generate_token")
    resp.raise_for_status()
    data = resp.json()
    token = data.get("access_token") or data.get("token")
    if not token:
        print("Erro: token não encontrado na resposta:", data)
        return

    print(f"Token gerado: {token[:40]}...")

    client = pymongo.MongoClient(MONGO_URI)
    db = client[MONGO_DB]

    result = db.tool.update_many(
        {"api_config.auth.type": "bearer"},
        {"$set": {"api_config.auth.token": token}}
    )

    print(f"Ferramentas atualizadas: {result.modified_count}")
    client.close()
    print("Pronto! Reinicie o AGENT_TCC server e worker.")

if __name__ == "__main__":
    main()
