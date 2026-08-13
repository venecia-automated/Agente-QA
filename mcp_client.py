"""
mcp_client.py

Encapsula la conexión MCP: el carril "MCP Client" del diagrama de secuencia.

Sabe cómo:
  - conectarse a un servidor MCP vía stdio
  - listar las tools disponibles, ya traducidas al formato de Anthropic
  - ejecutar una tool y devolver su resultado como texto

No sabe nada de Claude ni del loop de conversación — eso es responsabilidad
del agente (Agente_1_QA.py), que es el carril "Our Server" del diagrama.
"""

from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class MCPClient:
    def __init__(self, server_script: str):
        self.server_script = server_script
        self.session: ClientSession | None = None
        self._exit_stack = AsyncExitStack()

    async def connect(self) -> None:
        """Lanza el servidor MCP como subproceso y abre la sesión."""
        server_params = StdioServerParameters(
            command="python",
            args=[self.server_script],
        )
        read, write = await self._exit_stack.enter_async_context(
            stdio_client(server_params)
        )
        self.session = await self._exit_stack.enter_async_context(
            ClientSession(read, write)
        )
        await self.session.initialize()

    async def list_tools(self) -> list[dict]:
        """
        Pide al servidor la lista de tools (ListToolsRequest/Result del
        diagrama) y la devuelve ya traducida al formato que espera la
        API de Anthropic.
        """
        response = await self.session.list_tools()
        return [
            {
                "name": tool.name,
                "description": tool.description or "",
                "input_schema": tool.inputSchema,
            }
            for tool in response.tools
        ]

    async def call_tool(self, name: str, arguments: dict) -> str:
        """
        Ejecuta una tool en el servidor (CallToolRequest/Result del
        diagrama) y devuelve su resultado como texto plano.
        """
        result = await self.session.call_tool(name, arguments)
        return "".join(item.text for item in result.content if item.type == "text")

    async def close(self) -> None:
        """Cierra la sesión MCP y termina el subproceso del servidor."""
        await self._exit_stack.aclose()