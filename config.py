# config.py
import os
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    # LLM GEMINI
    GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY")
    GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash-preview-05-20")
    
    # MONGODB
    MONGO_URI: str = os.getenv("MONGO_URI", "mongodb://localhost:27017")
    MONGO_DB: str = os.getenv("MONGO_DB", "ai_agents")
    
    # RAG
    RAG_BASE_URL: str = os.getenv("RAG_BASE_URL", "http://localhost:3333")
    RAG_API_TOKEN: str = os.getenv("RAG_API_TOKEN", "")

    # QDRANT
    QDRANT_URL: str = os.getenv("QDRANT_URL")
    QDRANT_PORT: int = int(os.getenv("QDRANT_PORT", 6333))

    # API
    APP_NAME: str = "IA Agent Orchestrator"
    DEBUG: bool = os.getenv("DEBUG", "True") == "True"

    # Bot public URL (ngrok) para links de download nos chats
    BOT_PUBLIC_URL: str = os.getenv("BOT_PUBLIC_URL", "http://localhost:3978")

    # API Version
    API_VERSION: str = "1.0.0"

    class Config:
        env_file = ".env"

settings = Settings()