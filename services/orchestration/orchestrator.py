# services/orchestration/orchestrator.py
import asyncio
import copy
import json
import logging
import uuid
from models.schemas import ExecutionContext, ManagerSchema
from services.conversation.conversation_history import conversation_history
from services.definitions.definition_loader import definition_loader
from services.llm.gemini_adapter import GeminiAdapter
from services.logging.execution_logger import execution_logger

from .manager_executor import ManagerExecutor


class Orchestrator:
    def __init__(self):
        self.gemini = GeminiAdapter()
        self.manager_executor = ManagerExecutor()
        self.definition_loader = definition_loader
        self.logger = logging.getLogger(__name__)

    def process_task_sync(self, job_payload: dict) -> dict:
        """
        Ponto de entrada síncrono para o worker.
        Ele executa o loop de eventos asyncio internamente.
        """
        return asyncio.run(self.process_task_async(job_payload))

    async def process_task_async(self, job_payload: dict) -> dict:
        """
        Lógica principal de orquestração, iniciada a partir de uma tarefa de background.
        """
        session_id = job_payload.get("session_id", str(uuid.uuid4()))
        user_id = job_payload.get("user_id")
        user_question = job_payload.get("user_input")

        self.logger.info(f"Orquestrador processando tarefa para a sessão {session_id}")
        
        if not user_id or not user_question:
            raise ValueError("user_id e user_input são obrigatórios no payload da tarefa.")

        context = ExecutionContext(
            session_id=session_id,
            user_id=user_id,
            user_question=user_question,
            user_data={"user_id": user_id}
        )

        await self.get_manager_agent(context)

        if not context.available_managers:
            self.logger.warning(f"Nenhum manager ativo encontrado para o usuário {context.user_id}.")
            return {"response": "Não tenho as ferramentas necessárias para responder à sua pergunta no momento."}

        self._initialize_logs(context)
        return await self._cooperative_execution_flow(context)

    def _initialize_logs(self, context: ExecutionContext):
        """Inicializa os logs de execução e de conversa."""
        execution_id = f"exec_{uuid.uuid4().hex[:8]}"
        context.execution_id = execution_id
        conversation_history.log_message(
            session_id=context.session_id, execution_id=execution_id, role="user",
            user_id=context.user_id, message=context.user_question
        )
        execution_logger.initialize_execution_log(
            session_id=context.session_id,
            context={"user_id": context.user_id, "user_question": context.user_question}
        )

    async def _cooperative_execution_flow(self, context: ExecutionContext) -> dict:
        """Executa um fluxo de delegação cooperativo, decidindo um passo de cada vez."""
        MAX_CYCLES = 5  # Limite de segurança para evitar loops infinitos

        chat_history = conversation_history.get_last_messages(context.session_id, num_messages=10)

        for cycle in range(MAX_CYCLES):
            self.logger.info(f"Ciclo de Orquestração [{cycle + 1}/{MAX_CYCLES}] para a sessão {context.session_id}")

            # 1. Decidir a próxima ação usando o LLM
            next_action_plan = await asyncio.to_thread(self.gemini.decide_next_manager_action, context, chat_history)
            
            thought = next_action_plan.get('thought', 'Nenhum pensamento registrado.')
            decision = next_action_plan.get('decision')

            self.logger.info(f"[ORCHESTRATOR_THOUGHT]: {thought}")
            context.react_history.append(f"[ORCHESTRATOR_THOUGHT]: {thought}")

            # 2. Processar a decisão
            if decision == "final_answer":
                self.logger.info("Delegador decidiu que a coleta de dados terminou. Construindo resposta final formatada.")
                # O Delegador apenas sinaliza. O Orquestrador agora é responsável por chamar o construtor.
                final_answer = self._build_final_response_with_guidelines(context)
                return self._handle_final_response(context, final_answer)

            if decision == "call_manager":
                manager_id = next_action_plan.get("manager_id")
                new_question = next_action_plan.get("new_question")

                if not manager_id or not new_question:
                    msg = "Decisão de chamar manager inválida (faltando manager_id ou new_question)."
                    self.logger.error(msg)
                    return self._handle_final_response(context, f"Ocorreu um erro interno: {msg}")
                
                self.logger.info(f"Decisão: Delegar para o Manager '{manager_id}' com a tarefa: '{new_question}'")
                
                # 3. Executar o manager escolhido
                needs_input = await asyncio.to_thread(
                    self._execute_single_manager, context, manager_id, new_question
                )

                if needs_input:
                    self.logger.info("Execução pausada, aguardando input do usuário.")
                    return self._pending_response(context)
                
                continue
            
            self.logger.error(f"Decisão desconhecida ou erro do LLM: '{decision}'. Finalizando.")
            return self._handle_final_response(context, "Desculpe, ocorreu um erro no meu processo de decisão.")
        
        self.logger.warning(f"Máximo de {MAX_CYCLES} ciclos atingido para a sessão {context.session_id}. Finalizando.")
        final_answer = self._build_final_response_with_guidelines(context)
        return self._handle_final_response(context, final_answer)

    def _execute_single_manager(self, context: ExecutionContext, manager_id: str, new_question: str) -> bool:
        """Executa um único manager e atualiza o contexto principal."""
        manager = next((m for m in context.available_managers if m.manager_id == manager_id), None)
        if not manager:
            self.logger.error(f"Manager {manager_id} não encontrado ou não permitido para o usuário.")
            # Adiciona uma observação de erro no histórico para o próximo ciclo de decisão
            context.react_history.append(f"[ORCHESTRATOR_OBSERVATION]: Tentativa de chamar um manager inválido '{manager_id}'.")
            return False

        execution_logger.add_manager(context.session_id, manager_id, new_question)

        step_context = copy.deepcopy(context)
        step_context.react_history = [] 
        step_context.user_question = new_question
        
        needs_input = self.manager_executor.execute_manager(manager, step_context, context.user_question)

        self._consolidate_results(context.previous_results, step_context.previous_results)
        context.react_history.extend(step_context.react_history)
        if needs_input:
            context.pending_actions = step_context.pending_actions

        return needs_input

    def _consolidate_results(self, target: dict, source: dict):
        """Mescla os resultados de uma fonte para um alvo."""
        if not source: return
        for agent_id, tools in source.items():
            if agent_id not in target:
                target[agent_id] = {}
            for tool_name, output in tools.items():
                target[agent_id][tool_name] = output

    def _handle_final_response(self, context: ExecutionContext, final_answer: str) -> dict:
        """Formata e loga a resposta final antes de retornar."""
        self._log_final_response(context, final_answer)
        return {"type": "completed", "session_id": context.session_id, "response": final_answer}

    def _pending_response(self, context: ExecutionContext) -> dict:
        """Cria uma resposta quando o sistema precisa de input do usuário."""
        if not context.pending_actions:
            self.logger.error("Ação pendente solicitada mas não configurada.")
            return {"type": "error", "message": "Erro interno."}
        required_params = context.pending_actions[0].get("required_params", [])
        execution_logger.update_pending_actions(context.session_id, context.pending_actions)
        return {
            "type": "pending", "session_id": context.session_id,
            "message": "Precisamos de mais informações para continuar.",
            "required_params": required_params, "context": context.dict()
        }
    
    def _build_final_response_with_guidelines(self, context: ExecutionContext) -> str:
        """Coleta as diretrizes dos agentes executados e gera a resposta final."""

        formatting_guidelines = []
        for agent_id in context.previous_results.keys():
            agent_def = context.available_agents.get(agent_id)
            if agent_def and agent_def.response_guideline:
                guideline_with_context = (
                    f"Para os resultados do especialista '{agent_def.description}', "
                    f"siga esta regra de formato: '{agent_def.response_guideline}'"
                )
                formatting_guidelines.append(guideline_with_context)

        result = self.gemini.consolidate_final_response(context, formatting_guidelines)
        if not result:
            self.logger.warning("consolidate_final_response retornou vazio. Usando fallback.")
            result = "Desculpe, encontrei um problema ao processar sua resposta. Por favor, tente novamente em instantes."
        return result
    
    def _log_final_response(self, context: ExecutionContext, response: str):
        """Loga a resposta final nos históricos."""
        conversation_history.log_message(
            session_id=context.session_id, execution_id=context.execution_id,
            role="system", user_id="orchestrator", message=response
        )
        execution_logger.update_final_output(context.session_id, response)
        execution_logger.finalize_execution_log(context.session_id, status="completed")

    async def get_manager_agent(self, context: ExecutionContext) -> dict:
            """Carrega as definições de managers e agents para o usuário."""
            try:
                managers, agents = await asyncio.to_thread(
                    self.definition_loader.load_definitions_for_user, context.user_id
                )
                context.available_managers = managers
    
                # A variável 'agents' já é um dicionário no formato {'agent_id': AgentSchema}
                self.logger.debug(f"Agents carregados: {agents}")
    
                context.available_agents = agents
    
            except Exception as e:
                self.logger.error(f"Falha ao carregar definições para o usuário {context.user_id}: {e}", exc_info=True)