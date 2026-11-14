from collections import defaultdict
from models.schemas import ManagerSchema, ExecutionContext, ToolResult
from services.llm.gemini_adapter import GeminiAdapter
from .agent_executor import AgentExecutor
from services.logging.execution_logger import execution_logger
import json
import logging
import re
import copy

class ManagerExecutor:
    def __init__(self):
        self.gemini = GeminiAdapter()
        self.agent_executor = AgentExecutor()
        self.logger = logging.getLogger(__name__)
    
    def execute_manager(self, manager: ManagerSchema, context: ExecutionContext, original_question: str) -> bool:
        """Executa manager usando padrão ReAct com histórico explícito"""
        MAX_REACT_CYCLES = 2
        requires_user_input = False
        
        # Guarda estado inicial para preservar resultados de managers anteriores
        initial_previous_results = copy.deepcopy(context.previous_results)
        initial_react_history = copy.deepcopy(context.react_history) if hasattr(context, 'react_history') else []
        
        # Inicializa histórico se necessário
        if not hasattr(context, 'react_history'):
            context.react_history = []
        
        try:
            for cycle in range(MAX_REACT_CYCLES):
                self.logger.info(f"Iniciando ciclo ReAct {cycle+1}/{MAX_REACT_CYCLES}")
                
                # Executa ciclo ReAct
                cycle_result = self.gemini.react_cycle(
                    context.user_id,
                    manager,
                    context,
                    context.react_history,
                    original_question 
                )
                
                thought = cycle_result["thought"]
                action = cycle_result["action"]
                final_answer = cycle_result["final_answer"]
                
                # Registra thought no histórico
                if thought:
                    thought_entry = f"[THOUGHT]: {thought}"
                    context.react_history.append(thought_entry)
                    execution_logger.log_react_thought(
                        session_id=context.session_id,
                        manager_id=manager.manager_id,
                        thought=thought
                    )
                    self.logger.info(thought_entry)
                
                # Processa FINAL ANSWER
                if final_answer:
                    final_entry = f"[FINAL_ANSWER]: {final_answer}"
                    context.react_history.append(final_entry)
                    context.final_output = final_answer

                    execution_logger.log_react_final_answer(
                        session_id=context.session_id,
                        manager_id=manager.manager_id,
                        final_answer=final_answer
                    )
                    self.logger.info(final_entry)
                    return False
                
                # Processa ACTION
                if action:
                    action_entry = f"[ACTION]: {action}"
                    context.react_history.append(action_entry)
                    execution_logger.log_react_action(
                        session_id=context.session_id,
                        manager_id=manager.manager_id,
                        action=action
                    )
                    self.logger.info(action_entry)
                    
                    # Executa a ação
                    tool_result, requires_user_input = self._execute_react_action(
                        manager, context, action
                    )
                    
                    if requires_user_input:
                        return True
                    
                    # Se obtivemos resultado, registra como observação
                    if tool_result:
                        observation_entry = f"[OBSERVATION]: {tool_result}" #Se precisar colocar [OBSERVATION]:
                        context.react_history.append(observation_entry)
                        execution_logger.log_react_observation(
                            session_id=context.session_id,
                            manager_id=manager.manager_id,
                            observation=observation_entry
                        )
                        self.logger.info(observation_entry)
                
                # Limite de segurança
                if cycle == MAX_REACT_CYCLES - 1:
                    context.react_history.append("[OBSERVATION]: Limite máximo de ciclos atingido")
            
            return requires_user_input
        
        finally:
            # 1. Combina previous_results
            for agent_id, tools in context.previous_results.items():
                if agent_id not in initial_previous_results:
                    initial_previous_results[agent_id] = {}
                initial_previous_results[agent_id].update(tools)
            
            # 2. Combina react_history
            combined_history = initial_react_history + context.react_history
            
            # Atualiza contexto com estado combinado
            context.previous_results = initial_previous_results
            context.react_history = combined_history
    
    def _execute_react_action(self, manager: ManagerSchema, context: ExecutionContext, action: str) -> tuple:
        """Executa uma ação no formato ReAct e retorna (observação, requires_user_input)"""
        try:
            # Tenta parsear ação como JSON
            action_json = self._parse_action_json(action)
            if action_json:
                tool_name = action_json.get("tool_name")
                params = action_json.get("params", {})
                
                if tool_name:
                    return self._execute_tool(manager, context, tool_name, params)
            
            # Se não for JSON, tenta extrair padrão simples
            match = re.match(r'(\w+)\(([^)]*)\)', action.strip())
            if match:
                tool_name = match.group(1)
                params_str = match.group(2)
                params = self._parse_params(params_str)
                return self._execute_tool(manager, context, tool_name, params)
            
            # Se nenhum padrão for reconhecido
            observation = f"Formato de ação não reconhecido: {action}"
            return observation, False
            
        except Exception as e:
            return f"Erro na execução: {str(e)}", False
    
    def _parse_action_json(self, action_str: str) -> dict:
        """Tenta analisar a ação como JSON"""
        try:
            # Tenta encontrar um objeto JSON na string
            start = action_str.find('{')
            end = action_str.rfind('}') + 1
            if start != -1 and end != 0:
                json_str = action_str[start:end]
                return json.loads(json_str)
        except json.JSONDecodeError:
            pass
        return None
    
    def _parse_params(self, params_str: str) -> dict:
        """Analisa parâmetros no formato chave=valor"""
        params = {}
        parts = [p.strip() for p in params_str.split(',') if p.strip()]
        for part in parts:
            if '=' in part:
                key, value = part.split('=', 1)
                params[key.strip()] = value.strip().strip('"\'')
            else:
                params[part] = True
        return params
    
    def _execute_tool(self, manager: ManagerSchema, context: ExecutionContext, tool_name: str, params: dict) -> tuple:
        """Executa uma ferramenta específica"""
        # Encontrar agente dono da ferramenta
        agent_id, agent, tool_def = self._find_agent_by_tool(manager, tool_name)
        if not agent or not tool_def:
            return f"Ferramenta '{tool_name}' ou seu agente não foram encontrados", False
        
        # Executar a ferramenta
        result = self.agent_executor.execute_agent(agent, tool_name, params, context)
        
        # Se precisar de input do usuário
        if result.next_step == "REQUEST_USER_INPUT":
            self._handle_pending_input(context, agent_id, result)
            return None, True
        
        if isinstance(result.output, (dict, list)):
            observation = json.dumps(result.output, ensure_ascii=False)
        else:
            observation = str(result.output)

        execution_logger.log_tool_invocation_result(
            session_id=context.session_id,
            manager_id=manager.manager_id,
            agent_id=agent_id,
            tool_name=tool_name,
            success=bool(getattr(result, "success", True)),
            output=observation
        )
        
        # Armazenar resultado no contexto
        self._store_result(context, agent_id, tool_name, result)
        
        return observation, False
    
    def _find_agent_by_tool(self, manager: ManagerSchema, tool_name: str) -> tuple:
        """Encontra o agente que possui uma ferramenta específica"""
        for agent in manager.agents:
            for tool in agent.tools:
                if tool.tool_name.lower() == tool_name.lower():
                    return agent.agent_id, agent, tool
        return None, None, None
    
    def _store_result(self, context: ExecutionContext, agent_id: str, tool_name: str, result: ToolResult):
        """Armazena resultado no contexto"""

        if agent_id not in context.previous_results:
            context.previous_results[agent_id] = {}
        context.previous_results[agent_id][tool_name] = result.output
    
    def _handle_pending_input(self, context: ExecutionContext, agent_id: str, result: ToolResult):
        """Configura ação pendente para input do usuário"""
        context.pending_actions.append({
            "agent_id": agent_id,
            "required_params": result.required_params
        })