# services/definitions/definition_loader.py
import pymongo
import os
from typing import Dict, List, Tuple
from models.schemas import ManagerSchema, AgentSchema, ToolSchema
from .system_managers import MEMORY_MANAGER_DEFINITION, META_MANAGER_DEFINITION
from config import settings

class DefinitionLoader:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(DefinitionLoader, cls).__new__(cls)
        return cls._instance

    def _connect_if_needed(self):
        """
        Garante que uma conexão com o MongoDB exista para o processo atual.
        Se a conexão não foi criada ainda neste processo, ela é estabelecida aqui.
        """
        if not hasattr(self, 'client') or self.client is None:
            try:
                self.client = pymongo.MongoClient(settings.MONGO_URI)
                self.db = self.client[settings.MONGO_DB]
                # Este log mostrará que cada processo worker está criando sua própria conexão
                print(f"Processo PID:{os.getpid()}: Conectado com sucesso ao MongoDB no banco '{settings.MONGO_DB}'")
            except pymongo.errors.ConnectionFailure as e:
                print(f"Processo PID:{os.getpid()}: Falha ao conectar ao MongoDB: {e}")
                raise RuntimeError(f"Não foi possível conectar ao MongoDB: {e}")

    def load_definitions_for_user(self, user_id: str) -> Tuple[List[ManagerSchema], Dict[str, AgentSchema]]:
        """
        Carrega todas as definições (Managers, Agents, Tools) permitidas para um usuário específico.
        A operação é feita sob demanda com uma única agregação no MongoDB.
        """

        self._connect_if_needed()

        user_data = self.db.user.find_one(
            {"username": user_id},
            {"projects": 1, "settings": 1, "_id": 0}
        )

        if not user_data:
            print(f"Usuário '{user_id}' não encontrado. Usando configuração padrão.")
            user_data = self.db.user.find_one(
                {"username": "tarick"},
                {"projects": 1, "settings": 1, "_id": 0}
            )
            if not user_data:
                print("Usuário padrão 'tarick' também não encontrado.")
                return [], {}
        
        # Obter os projetos e as configurações
        user_project_names = user_data.get("projects", [])
        user_settings = user_data.get("settings", {})

        all_managers = [META_MANAGER_DEFINITION]
        all_agents_dict: Dict[str, AgentSchema] = {
            agent.agent_id: agent for agent in META_MANAGER_DEFINITION.agents
        }

        # Carrega os managers customizados apenas se o usuário tiver projetos
        if user_project_names:
            # Pipeline de agregação para buscar Managers e aninhar Agents e Tools ativos
            pipeline = [
                {"$match": {"project_name": {"$in": user_project_names}, "isActive": True}},
                {"$lookup": {
                    "from": "agent", "localField": "agents", "foreignField": "agent_id", "as": "populated_agents",
                    "pipeline": [
                        {"$match": {"isActive": True}},
                        {"$lookup": {
                            "from": "tool", "localField": "tools", "foreignField": "tool_name", "as": "populated_tools",
                            "pipeline": [{"$match": {"isActive": True}}, {"$project": {"_id": 0}}]
                        }},
                        {"$addFields": {"tools": "$populated_tools"}},
                        {"$project": {"populated_tools": 0, "_id": 0}}
                    ]
                }},
                {"$addFields": {"agents": "$populated_agents"}},
                {"$project": {"populated_agents": 0, "_id": 0}}
            ]


            managers_data = list(self.db.manager.aggregate(pipeline))

            for manager_data in managers_data:
                agent_objects = []
                for agent_data in manager_data.get("agents", []):
                    tool_objects = [ToolSchema(**tool_data) for tool_data in agent_data.get("tools", [])]
                    agent_data["tools"] = tool_objects
                    agent_schema = AgentSchema(**agent_data)
                    agent_objects.append(agent_schema)
                    all_agents_dict[agent_schema.agent_id] = agent_schema

                manager_data["agents"] = agent_objects
                all_managers.append(ManagerSchema(**manager_data))

        
        if user_settings.get("long_term_memory_enabled", False):
            print(f"Injetando MemoryManager para o usuário '{user_id}'.")
            
            all_managers.append(MEMORY_MANAGER_DEFINITION)
            
            # Adiciona o agente do manager de sistema ao dicionário de agentes
            for agent in MEMORY_MANAGER_DEFINITION.agents:
                all_agents_dict[agent.agent_id] = agent
        
        return all_managers, all_agents_dict

definition_loader = DefinitionLoader()