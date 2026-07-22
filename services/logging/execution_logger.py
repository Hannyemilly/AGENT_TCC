import json
import os
import uuid
from datetime import datetime
from typing import Optional

from pymongo import MongoClient
from pymongo.collection import Collection

from config import settings
from models.schemas import ExecutionContext

class ExecutionLogger:
    _instance = None
    _client: MongoClient = None
    _db = None
    _collection: Collection = None


    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ExecutionLogger, cls).__new__(cls)

            # 3. Conectar ao MongoDB na primeira inicialização (padrão Singleton)
            try:
                cls._client = MongoClient(settings.MONGO_URI)
                cls._db = cls._client[settings.MONGO_DB]
                cls._collection = cls._db["execution_logs"]
                # 4. Criar índices para otimizar buscas futuras
                cls._collection.create_index("session_id")
                cls._collection.create_index("execution_id", unique=True)
                print("[EXECUTION_LOG] Conectado ao MongoDB com sucesso.")
            except Exception as e:
                print(f"[EXECUTION_LOG] ERRO CRÍTICO: Não foi possível conectar ao MongoDB. {e}")
                cls._collection = None

            cls._instance._execution_registry = {}
        return cls._instance

    def initialize_execution_log(self, session_id: str, context: dict):
        """Inicializa um novo log de execução com estrutura hierárquica"""
        log_entry = {
            "session_id": session_id,
            "execution_id": f"exec_{uuid.uuid4().hex[:8]}",
            "user_id": context.get("user_id", ""),
            "user_question": context.get("user_question", ""),
            "start_timestamp": datetime.utcnow().isoformat() + "Z",
            "status": "in_progress",
            "orchestrator": [],
            "managers": [],
            "final_output": "",
            "insufficient_context": False,
            "pending_actions": [],
            "metadata": {
                "api_version": settings.API_VERSION,
                "llm_model": settings.GEMINI_MODEL,
                "execution_mode": "orchestrator"
            }
        }
        self._execution_registry[session_id] = log_entry
        return log_entry

    def add_manager(self, session_id: str, manager_id: str, new_question: str):
        """Adiciona um novo manager ao log de execução"""
        if session_id not in self._execution_registry:
            return None

        manager_log = {
            "manager_id": manager_id,
            "new_question": new_question,
            "previous_results": {},
            "react_history": []
        }
        self._execution_registry[session_id]["managers"].append(manager_log)
        if manager_id not in self._execution_registry[session_id]["orchestrator"]:
            self._execution_registry[session_id]["orchestrator"].append(manager_id)
        return manager_log

    def get_manager_log(self, session_id: str, manager_id: str):
        """Obtém o log específico de um manager"""
        if session_id in self._execution_registry:
            # Primeiro, verifica o registro em memória
            for manager in self._execution_registry[session_id]["managers"]:
                if manager["manager_id"] == manager_id:
                    return manager
        return None

    def add_manager_react_history(self, session_id: str, manager_id: str, entry: str, entry_type: str):
        """Adiciona uma entrada ao histórico do ReAct de um manager específico"""
        manager_log = self.get_manager_log(session_id, manager_id)
        if not manager_log:
            return
        prefix = {
            "thought": "[THOUGHT]",
            "action": "[ACTION]",
            "observation": "[OBSERVATION]",
            "final_answer": "[FINAL_ANSWER]"
        }.get(entry_type, "[UNKNOWN]")
        if entry.strip().startswith(prefix):
            formatted_entry = entry.strip()
        else:
            formatted_entry = f"{prefix}: {entry}"
            
        manager_log["react_history"].append(formatted_entry)

    def add_tool_result(self, session_id: str, manager_id: str, agent_id: str, tool_name: str, result: dict):
        """Adiciona o resultado de uma ferramenta ao log do manager"""
        manager_log = self.get_manager_log(session_id, manager_id)
        if not manager_log:
            return
        if agent_id not in manager_log["previous_results"]:
            manager_log["previous_results"][agent_id] = {}
        manager_log["previous_results"][agent_id][tool_name] = result

    def update_final_output(self, session_id: str, final_output: str):
        """Atualiza a saída final da execução"""
        if session_id in self._execution_registry:
            self._execution_registry[session_id]["final_output"] = final_output

    def update_pending_actions(self, session_id: str, actions: list):
        """Atualiza as ações pendentes"""
        if session_id in self._execution_registry:
            self._execution_registry[session_id]["pending_actions"] = actions

    def finalize_execution_log(self, session_id: str, status: str = "completed"):
        """Finaliza e salva o log de execução no formato desejado"""
        if session_id not in self._execution_registry:
            return None
        
        # Garante que a conexão com o DB esteja ativa
        if self._collection is None:
            print(f"[EXECUTION_LOG] ERRO: Não é possível salvar o log. Sem conexão com o MongoDB.")
            return None
        
        log_entry = self._execution_registry[session_id]
        end_time = datetime.utcnow()
        start_time = datetime.fromisoformat(log_entry["start_timestamp"].replace("Z", ""))
        log_entry["end_timestamp"] = end_time.isoformat() + "Z"
        log_entry["duration_ms"] = int((end_time - start_time).total_seconds() * 1000)
        log_entry["status"] = status

        try:
            # A operação de inserção é atômica e segura contra concorrência.
            self._collection.insert_one(log_entry)
            execution_id = log_entry['execution_id']
            if settings.DEBUG:
                print(f"[EXECUTION_LOG] Log da execução {execution_id} salvo no MongoDB.")
        except Exception as e:
            print(f"[EXECUTION_LOG] ERRO ao salvar log no MongoDB para a sessão {session_id}: {e}")

        # Limpa o registro em memória para esta execução, liberando recursos
        if session_id in self._execution_registry:
            del self._execution_registry[session_id]

    def log_react_thought(self, session_id: str, manager_id: str, thought: str):
        self.add_manager_react_history(session_id, manager_id, thought, "thought")

    def log_react_action(self, session_id: str, manager_id: str, action: str):
        self.add_manager_react_history(session_id, manager_id, action, "action")

    def log_react_observation(self, session_id: str, manager_id: str, observation: str):
        self.add_manager_react_history(session_id, manager_id, observation, "observation")

    def log_react_final_answer(self, session_id: str, manager_id: str, final_answer: str):
        self.add_manager_react_history(session_id, manager_id, final_answer, "final_answer")

    def log_tool_invocation_result(self, session_id: str, manager_id: str, agent_id: str, tool_name: str, success: bool, output: str):
        """Registra o resultado de uma invocação de ferramenta"""
        self.add_tool_result(
            session_id,
            manager_id,
            agent_id,
            tool_name,
            {
                "success": success,
                "output_summary": output[:300] + "..." if len(output) > 300 else output,
                "full_output": output
            }
        )
        self._flag_insufficient_context(session_id, success, output)

    def _flag_insufficient_context(self, session_id: str, success: bool, output: str):
        """Marca a execução quando uma ferramenta falha ou o serviço consultado
        responde sem contexto (ex.: RAG retorna {"success": false, ...}).
        O painel administrativo usa este campo para listar respostas incompletas."""
        no_context = not success
        if not no_context:
            try:
                data = json.loads(output)
                no_context = isinstance(data, dict) and data.get("success") is False
            except (json.JSONDecodeError, TypeError):
                pass
        if no_context and session_id in self._execution_registry:
            self._execution_registry[session_id]["insufficient_context"] = True

    def get_execution_log(self, session_id: str) -> Optional[dict]:
        """Recupera todos os logs de uma sessão do MongoDB, do mais novo para o mais antigo."""
        if self._collection is None: return None
        return list(self._collection.find({"session_id": session_id}).sort("start_timestamp", -1))

    def reconstruct_context_from_log(self, session_id: str) -> Optional[ExecutionContext]:
        """Recupera o ÚLTIMO log da sessão no MongoDB para reconstruir o contexto."""
        if self._collection is None: return None
        
        log_entry = self._collection.find_one({"session_id": session_id}, sort=[("start_timestamp", -1)])
        
        if not log_entry:
            return None
        
        consolidated_previous_results = {}
        consolidated_react_history = []
        for manager_log in log_entry.get("managers", []):
            consolidated_react_history.extend(manager_log.get("react_history", []))
            for agent_id, tools in manager_log.get("previous_results", {}).items():
                if agent_id not in consolidated_previous_results:
                    consolidated_previous_results[agent_id] = {}
                for tool_name, result_dict in tools.items():
                    output_string = result_dict.get("full_output", str(result_dict))
                    consolidated_previous_results[agent_id][tool_name] = output_string

        context = ExecutionContext(
            session_id=log_entry.get("session_id"),
            user_id=log_entry.get("user_id"),
            user_question=log_entry.get("user_question"),
            execution_id=log_entry.get("execution_id"),
            pending_actions=log_entry.get("pending_actions", []),
            previous_results=consolidated_previous_results,
            react_history=consolidated_react_history,
            final_output=log_entry.get("final_output", ""),
            user_data={"user_id": log_entry.get("user_id")}
        )
        return context


execution_logger = ExecutionLogger()