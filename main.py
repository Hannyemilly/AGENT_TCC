# main.py
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from routers.api_router import router as api_router
from config import settings
import uvicorn

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

logger = logging.getLogger(__name__)

RAG_TOKEN_REFRESH_INTERVAL = 4 * 60 * 60  # 4 horas em segundos

async def _rag_token_refresh_loop():
    from atualizar_token_rag import main as renovar_token
    while True:
        try:
            await asyncio.to_thread(renovar_token)
            logger.info("Token RAG renovado com sucesso.")
        except Exception as e:
            logger.warning(f"Falha ao renovar token RAG (tentará novamente em 4h): {e}")
        await asyncio.sleep(RAG_TOKEN_REFRESH_INTERVAL)

@asynccontextmanager
async def lifespan(app: FastAPI):
    asyncio.create_task(_rag_token_refresh_loop())
    yield

# A aplicação FastAPI agora é mais simples
app = FastAPI(
    title=f"{settings.APP_NAME} - Health Check API",
    description="Serviço de Health Check para o AI Agent Worker.",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(api_router, prefix="/api/v1")

@app.get("/health")
def health_check():
    """
    Verifica se o serviço está online. Útil para monitoramento em contêineres (Kubernetes, Docker).
    """
    return {"status": "healthy", "version": app.version}


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.DEBUG
    )