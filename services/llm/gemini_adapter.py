# services/llm/gemini_adapter.py
import google.generativeai as genai
from google.api_core import exceptions as google_exceptions
from google.api_core import retry as google_retry
from datetime import datetime
from models.schemas import ExecutionContext, ManagerSchema, ToolResult
from typing import List
from config import settings
import json
import logging
import re
import time

# Pares de pergunta e resposta cadastrados no painel viram arquivos internos "par_<id>.txt".
# Eles servem de contexto, mas não são documentos para o aluno baixar.
_INTERNAL_DOC = re.compile(r"par_[0-9a-fA-F]{24}\.txt")
_CITED_HEADER = re.compile(
    r"^\s*(?:📎|📄|🔗)?\s*\**\s*(?:Documentos Citados|Fontes?(?: da informação| consultadas| utilizadas)?|Referências)"
    r"\s*\**\s*:?\s*\**\s*$",
    re.IGNORECASE,
)

# Com o modelo sobrecarregado (erro 503), a biblioteca do Google repete o pedido sozinha
# por até 10 minutos e a pergunta fica parada. Aqui são só alguns segundos; depois o pedido
# passa para o próximo modelo da lista (GEMINI_MODEL e, em seguida, GEMINI_FALLBACK_MODELS).
_REPETIR_SE_SOBRECARREGADO = google_retry.Retry(
    initial=1.0,
    maximum=4.0,
    multiplier=2.0,
    predicate=google_retry.if_exception_type(google_exceptions.ServiceUnavailable),
    timeout=10.0,
)
_TEMPO_LIMITE_PEDIDO = 90.0  # segundos para o modelo devolver uma resposta

# O modelo que falhou vai para o fim da fila por um tempo (em segundos), conforme o erro.
_PAUSA_COTA = 60  # 429: limite de pedidos por minuto ou cota do dia
_PAUSA_SOBRECARGA = 600  # 503, 500 ou tempo esgotado
_PAUSA_MODELO_RETIRADO = 3600  # 404: modelo desativado para o projeto
_pausado_ate: dict = {}


def _pausa_para(erro: Exception):
    """Tempo que o modelo fica no fim da fila depois deste erro, ou None quando o erro
    não é de disponibilidade do modelo (aí trocar de modelo não ajuda)."""
    if isinstance(erro, google_exceptions.ResourceExhausted):
        return _PAUSA_COTA
    if isinstance(erro, google_exceptions.NotFound):
        return _PAUSA_MODELO_RETIRADO
    if isinstance(erro, (google_exceptions.ServiceUnavailable, google_exceptions.RetryError,
                         google_exceptions.DeadlineExceeded, google_exceptions.InternalServerError,
                         TimeoutError)):
        return _PAUSA_SOBRECARGA
    texto = str(erro).lower()
    if "429" in texto or "quota" in texto or "rate limit" in texto or "resource_exhausted" in texto:
        return _PAUSA_COTA
    if any(p in texto for p in ("503", "high demand", "overloaded", "unavailable", "deadline", "timed out")):
        return _PAUSA_SOBRECARGA
    return None


def remove_internal_citations(text: str) -> str:
    """Tira de \"Documentos Citados\" os links para arquivos internos dos pares de P&R
    e remove a seção inteira quando não sobra nenhum documento."""
    if not text or not _INTERNAL_DOC.search(text):
        return text
    lines = [l for l in text.split("\n") if not _INTERNAL_DOC.search(l)]
    for i, line in enumerate(lines):
        if _CITED_HEADER.match(line):
            rest = lines[i + 1:]
            if not any("](" in l for l in rest):
                lines = lines[:i] + [l for l in rest if l.strip()]
            break
    return "\n".join(lines).rstrip()

class GeminiAdapter:
    def __init__(self):
        genai.configure(api_key=settings.GEMINI_API_KEY)
        self.model = settings.GEMINI_MODEL
        reservas = [m.strip() for m in (settings.GEMINI_FALLBACK_MODELS or "").split(",") if m.strip()]
        self.models = list(dict.fromkeys([self.model] + reservas))
        self.logger = logging.getLogger(__name__)
        self.system_instruction = self._load_system_instruction()

    def _load_system_instruction(self) -> str:
        """Carrega a instrução do sistema do arquivo"""
        try:
            with open("prompts/system_instruction.md", "r", encoding="utf-8") as file:
                return file.read()
        except FileNotFoundError:
            return "Você é um assistente de IA."
        
    def _create_simplified_manager_list(self, managers: List[ManagerSchema]) -> List[dict]:
        """
        Cria uma lista de dicionários simplificada dos managers e suas ferramentas,
        ideal para ser usada em prompts de LLM, economizando tokens.
        """
        simplified_list = []
        for manager in managers:
            if not manager.isActive:
                continue

            manager_info = {
                "manager_id": manager.manager_id,
                "description": manager.description,
                "tools": []
            }

            for agent in manager.agents:
                if not agent.isActive:
                    continue
                
                for tool in agent.tools:
                    if not tool.isActive:
                        continue
                    
                    params_str = ", ".join([f"{p.name}: {p.type}" for p in tool.parameters_mandatory])
                    tool_info = {
                        "name": tool.tool_name,
                        "description": tool.description,
                        "parameters": params_str if params_str else "Nenhum"
                    }
                    manager_info["tools"].append(tool_info)
            
            if manager_info["tools"]:
                simplified_list.append(manager_info)

        return simplified_list

    @staticmethod
    def _texto_da_resposta(response) -> str:
        """Texto da resposta do Gemini. Alguns modelos dividem a resposta em várias partes,
        e o atalho response.text só aceita uma; aqui as partes de texto são juntadas."""
        try:
            return response.text
        except ValueError:
            partes = []
            for candidato in getattr(response, "candidates", None) or []:
                conteudo = getattr(candidato, "content", None)
                for parte in getattr(conteudo, "parts", None) or []:
                    if getattr(parte, "thought", False):
                        continue
                    texto = getattr(parte, "text", "")
                    if texto:
                        partes.append(texto)
                if partes:
                    break
            if not partes:
                raise
            return "".join(partes)

    def _modelos_em_ordem(self) -> List[str]:
        """Principal e reservas, com os que falharam há pouco no fim da fila."""
        agora = time.time()
        livres = [m for m in self.models if _pausado_ate.get(m, 0) <= agora]
        pausados = sorted((m for m in self.models if _pausado_ate.get(m, 0) > agora),
                          key=lambda m: _pausado_ate[m])
        return livres + pausados

    def generate(self, prompt: str, system_instruction: str = None) -> str:
        instrucao = system_instruction if system_instruction else self.system_instruction
        rodadas = 3
        for rodada in range(rodadas):
            so_limite_de_pedidos = True
            for modelo in self._modelos_em_ordem():
                try:
                    model = genai.GenerativeModel(modelo, system_instruction=instrucao)
                    response = model.generate_content(
                        prompt,
                        request_options={"retry": _REPETIR_SE_SOBRECARREGADO, "timeout": _TEMPO_LIMITE_PEDIDO},
                    )
                    texto = self._texto_da_resposta(response)
                except Exception as e:
                    self.logger.error(f"Erro na geração Gemini com {modelo} (rodada {rodada + 1}/{rodadas}): {e}")
                    pausa = _pausa_para(e)
                    if pausa is None:
                        return ""
                    _pausado_ate[modelo] = time.time() + pausa
                    so_limite_de_pedidos = so_limite_de_pedidos and pausa == _PAUSA_COTA
                    continue
                if modelo != self.model:
                    self.logger.warning(f"Resposta gerada pelo modelo reserva {modelo} ({self.model} indisponível).")
                return texto
            # Só vale esperar e tentar de novo quando todos bateram no limite de pedidos por minuto.
            if not so_limite_de_pedidos or rodada == rodadas - 1:
                break
            espera = 10 * (rodada + 1)
            self.logger.warning(f"Limite de pedidos em todos os modelos. Aguardando {espera}s antes de tentar novamente.")
            time.sleep(espera)
        return ""
        
    def consolidate_final_response(self, context: ExecutionContext, formatting_guidelines: List[str]) -> str:
        """
        Gera a resposta final para o usuário, sintetizando todos os resultados
        e seguindo as diretrizes de formatação fornecidas.
        """
        
        guidelines_section = ""
        if formatting_guidelines:
            formatted_guidelines = "\n- ".join(formatting_guidelines)
            guidelines_section = (
                "### 📜 Regras de Formatação Obrigatórias\n"
                "Para construir a resposta final, você DEVE seguir estas regras de formatação para as informações correspondentes:\n"
                f"- {formatted_guidelines}"
            )

        bot_public_url = settings.BOT_PUBLIC_URL.rstrip("/")

        prompt = f"""
        ## 🤖 Persona
        Você é um Redator Chefe de IA, especialista em comunicação. Sua função é pegar dados brutos e rascunhos de uma equipe de agentes de IA e transformar tudo em uma resposta final, clara, coesa e perfeitamente formatada para um usuário humano.

        ---

        ## 📝 Contexto e Dados Recebidos

        ### Pergunta Original do Usuário:
        {context.user_question}

        ### Resultados Brutos das Ferramentas (Fonte da Verdade):
        ```json
        {json.dumps(context.previous_results, indent=2, ensure_ascii=False)}
        ```

        ### Raciocínio Interno da Equipe (Para seu Contexto):
        ```
        {chr(10).join(context.react_history)}
        ```
        ---
        {guidelines_section}
        ---
        ## 🎯 Tarefa Final e Regras de Ouro
        Sua tarefa é sintetizar os **Resultados Brutos das Ferramentas** em uma resposta única e amigável para o usuário. Siga estas regras rigorosamente:
        1. **Siga as Regras de Formatação:** Se a seção "Regras de Formatação Obrigatórias" existir, suas regras são a prioridade máxima para estilizar as informações correspondentes. Se um resultado não tiver uma regra, apresente-o de forma clara e legível.
        2. **Baseie-se nos Fatos:** Sua resposta deve sintetizar **todas as informações de contexto disponíveis**. Se os `Resultados Brutos das Ferramentas` contiverem dados, eles são a fonte primária da verdade. Se estiverem vazios, use o `Raciocínio Interno da Equipe` para formular sua resposta, pois ele pode conter a conclusão direta encontrada pelo orquestrador. Não invente informações que não estejam no contexto fornecido.
        3. **Fale com o Usuário:** A resposta final deve ser direcionada ao usuário, não um relatório técnico.
        4. **Lide com Falhas:** Se os resultados indicarem que uma tarefa falhou, informe isso ao usuário de forma simples e direta.
        5. **Links de Download de Documentos:** Se os Resultados Brutos das Ferramentas contiverem documentos (identificados por linhas como `### Documento: nome_do_arquivo` seguidas de `Id: HASH`), adicione ao **final da resposta** uma seção chamada "📎 **Documentos Citados**" com um link para download de cada documento único encontrado, no formato de markdown:
           [📄 nome_do_arquivo]({bot_public_url}/download/HASH)
           Use exatamente o hash que aparece na linha `Id:` do documento. Não invente hashes.
           Não cite arquivos cujo nome começa com `par_` (são pares de pergunta e resposta internos da base, não documentos para baixar).

        Agora, gere a resposta final para o usuário.
        """
        return remove_internal_citations(self.generate(prompt).strip())

    def decide_next_manager_action(self, context: ExecutionContext, chat_history:list) -> dict:
        """
        Decide a próxima ação para o orquestrador: chamar um manager ou finalizar.
        """
        try:
            with open("prompts/delegator_prompt.md", "r", encoding="utf-8") as file:
                delegator_prompt_template = file.read()
        except FileNotFoundError:
            self.logger.error("Arquivo de prompt 'delegator_prompt.md' não encontrado.")
            # Retorna uma resposta de erro que pode ser tratada pelo orquestrador
            return {"decision": "error", "final_answer": "Não consegui encontrar minhas instruções para decidir o próximo passo. Por favor, contate o suporte."}

        # Formata os dados do contexto para o prompt
        simplified_managers = self._create_simplified_manager_list(context.available_managers)
        formatted_managers = json.dumps(simplified_managers, indent=2, ensure_ascii=False)

        formatted_results = json.dumps(context.previous_results, indent=2, ensure_ascii=False)
        formatted_react_history = "\n".join(context.react_history) if context.react_history else "Nenhum histórico de raciocínio ainda."

        prompt = delegator_prompt_template.format(
            user_id=context.user_id,
            chat_history=chat_history,
            user_input=context.user_question,
            available_managers=formatted_managers,
            previous_results=formatted_results,
            react_history=formatted_react_history,
            current_date=datetime.now().strftime("%d/%m/%Y %H:%M")
        )

        response_text = self.generate(prompt, system_instruction="Você é um orquestrador de IA que responde em JSON.")
        
        try:
            return self.parse_json_response(response_text)
        except json.JSONDecodeError:
            self.logger.error(f"Falha ao decodificar JSON do delegador: {response_text}")
            return {"decision": "final_answer", "final_answer": "Desculpe, tive um problema ao decidir o que fazer a seguir. Tente novamente."}

    def parse_json_response(self, text_response: str) -> dict:
        """Extrai um objeto JSON de uma string, mesmo que haja texto antes ou depois."""
        try:
            match = re.search(r'\{.*\}', text_response, re.DOTALL)
            if match:
                json_str = match.group(0)
                return json.loads(json_str)
            else:
                self.logger.error(f"Nenhum JSON encontrado na resposta: {text_response}")
                raise json.JSONDecodeError("JSON não encontrado", text_response, 0)
        except json.JSONDecodeError as e:
            self.logger.error(f"Erro ao decodificar JSON: {e}\nResposta recebida:\n{text_response}")
            raise

    def react_cycle(self, user_id: str, manager: ManagerSchema, context: ExecutionContext, history: list, original_question: str) -> dict:
        """Executa um ciclo completo ReAct (Thought + Action)"""
        with open("prompts/react_cycle_prompt.md", "r", encoding="utf-8") as file:
            prompt_template = file.read()

        history_str = "\n".join(history) if history else "Nenhum histórico ainda."
        tools_str = self._format_tools(manager)

        prompt = prompt_template.format(
            user_id=user_id,
            manager_id=manager.manager_id,
            manager_description=manager.description,
            step_objective=context.user_question,
            original_user_question=original_question,
            previous_results=json.dumps(context.previous_results, indent=2, ensure_ascii=False),
            history=history_str,
            available_tools=tools_str,
            current_date=datetime.now().strftime("%d/%m/%Y %H:%M")
        )

        response = self.generate(prompt)
        self.logger.debug(f"Resposta ReAct: {response}")

        return self._parse_react_response(response)

    def _format_tools(self, manager: ManagerSchema) -> str:
        """
        Formata as ferramentas ativas de um manager, agrupando por agente,
        para serem usadas em um prompt de LLM.
        """
        agent_tools_info = []

        # Itera sobre os agentes do manager
        for agent in manager.agents:
            if not agent.isActive:
                continue

            agent_tool_strings = []
            # Itera sobre as ferramentas de cada agente
            for tool in agent.tools:
                if not tool.isActive:
                    continue
                
                # Formata a lista de parâmetros de forma concisa
                params_str = ", ".join([f"{p.name}: {p.type}" for p in tool.parameters_mandatory])

                # Cria a string da ferramenta com a formatação ToolName(params): description
                agent_tool_strings.append(f"  - {tool.tool_name}({params_str}): {tool.description}")

            # Adiciona o cabeçalho do agente e suas ferramentas à lista principal,
            # apenas se o agente tiver ferramentas ativas.
            if agent_tool_strings:
                agent_tools_info.append(f"Agente: {agent.agent_id} ({agent.description})")
                agent_tools_info.extend(agent_tool_strings)

        return "\n".join(agent_tools_info)

    def _parse_react_response(self, response: str) -> dict:
        result = {"thought": "", "action": "", "final_answer": ""}

        thought_match = re.search(r'\[THOUGHT\]:(.*?)(?=\[ACTION\]|\[FINAL_ANSWER\]|$)', response, re.DOTALL | re.IGNORECASE)
        if thought_match:
            result["thought"] = thought_match.group(1).strip()

        action_match = re.search(r'\[ACTION\]:(.*?)(?=\[THOUGHT\]|\[FINAL_ANSWER\]|$)', response, re.DOTALL | re.IGNORECASE)
        if action_match:
            result["action"] = action_match.group(1).strip()

        final_match = re.search(r'\[FINAL_ANSWER\]:(.*?)(?=\[THOUGHT\]|\[ACTION\]|$)', response, re.DOTALL | re.IGNORECASE)
        if final_match:
            result["final_answer"] = final_match.group(1).strip()

        return result