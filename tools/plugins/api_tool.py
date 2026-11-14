# tools/plugins/api_tool.py
import requests
from urllib.parse import quote
from tools.base_tool import BaseTool
from models.schemas import ToolResult, ExecutionContext, ToolSchema, ApiConfigSchema
import json
import logging

class ApiTool(BaseTool):
    """
    Uma ferramenta genérica para executar chamadas de API.
    A configuração específica é injetada em tempo de execução através do tool_def.
    """
    
    @property
    def name(self):
        return "ExecutarAPI"

    @property
    def description(self):
        return "Ferramenta genérica para executar APIs configuradas no banco de dados."
        
    @property
    def mandatory_params(self):
        return []

    def _prepare_request_data(self, config: ApiConfigSchema, params: dict, tool_def: ToolSchema) -> dict:
        """
        Prepara a URL, headers, query params e body usando os parâmetros recebidos.
        """
        prepared_data = {
            "url": config.base_url,
            "headers": config.headers.copy() if config.headers else {},
            "params": {},
            "json": None 
        }
        
        params_usados = set()

        # 1. Processar parâmetros de path (substituição na URL)
        for key, value in params.items():
            placeholder = f"{{{key}}}"
            if placeholder in prepared_data["url"]:
                # Codifica o valor: "&", "#" e "+" na pergunta quebravam a URL
                prepared_data["url"] = prepared_data["url"].replace(placeholder, quote(str(value), safe=""))
                params_usados.add(key)

        # 2. Processar parâmetros de body (para POST, PUT, etc.)
        if config.body_template:
            body = config.body_template.copy()
            for key, value in body.items():
                if isinstance(value, str) and value.startswith("{") and value.endswith("}"):
                    param_key = value.strip("{}")
                    if param_key in params:
                        body[key] = params[param_key]
                        params_usados.add(param_key)
            prepared_data["json"] = body
        
        # 3. Processar parâmetros de query (todos os parâmetros definidos que não foram usados ainda)
        if tool_def.parameters_mandatory:
            for param_schema in tool_def.parameters_mandatory:
                param_name = param_schema.name
                if param_name in params and param_name not in params_usados:
                    prepared_data["params"][param_name] = params[param_name]
        
        # 4. Configurar autenticação
        auth_config = config.auth
        if auth_config:
            if auth_config.type == "bearer" and auth_config.token:
                prepared_data["headers"]["Authorization"] = f"Bearer {auth_config.token}"
            
        return prepared_data

    def execute(self, params: dict, context: ExecutionContext, tool_def: ToolSchema) -> ToolResult:
        """Executa a chamada de API dinâmica usando a configuração do tool_def."""
        try:
            api_config = tool_def.api_config
            if not api_config:
                return ToolResult(success=False, output=f"A ferramenta '{tool_def.tool_name}' não possui 'api_config'.")

            request_data = self._prepare_request_data(api_config, params, tool_def)
            method = (api_config.method or "GET").upper()

            response = requests.request(
                method=method,
                url=request_data["url"],
                headers=request_data.get("headers", {}),
                params=request_data.get("params", {}),
                json=request_data.get("json"),
                # Sem tempo limite uma pergunta podia ficar presa por até 10 minutos
                timeout=60
            )
            response.raise_for_status()

            try:
                output_data = response.json()
            except json.JSONDecodeError:
                output_data = response.text

            return ToolResult(success=True, output=json.dumps(output_data, ensure_ascii=False, indent=2))

        except requests.exceptions.HTTPError as e:
            error_body = e.response.text
            error_msg = f"Erro HTTP ao chamar a API '{tool_def.tool_name}': {e.response.status_code} - {error_body}"
            return ToolResult(success=False, output=error_msg)
        except requests.exceptions.RequestException as e:
            error_msg = f"Erro de conexão ao chamar a API '{tool_def.tool_name}': {str(e)}"
            return ToolResult(success=False, output=error_msg)
        except Exception as e:
            return ToolResult(success=False, output=f"Erro inesperado: {str(e)}")