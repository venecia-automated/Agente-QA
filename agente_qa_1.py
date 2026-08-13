"""
Agente_1_QA.py

"Our Server" del diagrama de secuencia: orquesta la conversación con
Claude y decide cuándo pedirle al MCP Client que ejecute una tool.
No habla el protocolo MCP directamente -- eso vive en mcp_client.py.

Requiere la variable de entorno ANTHROPIC_API_KEY seteada.
"""

import argparse
import asyncio

import anthropic
from anthropic import Anthropic
from dotenv import load_dotenv

from mcp_client import MCPClient

load_dotenv()  # lee el archivo .env y carga ANTHROPIC_API_KEY al entorno

MODEL = "claude-sonnet-4-5"  # ajustá si querés otro modelo
SERVER_SCRIPT = "mcp_server_qa.py"
MAX_ITERATIONS = 10  # tope de vueltas de tool-use, para no loopear sin control

anthropic_client = Anthropic()  # usa ANTHROPIC_API_KEY del entorno


def _extract_text(content) -> str:
    return "".join(block.text for block in content if block.type == "text")


async def _post_fallback_comment(client: MCPClient, ticket_id: str, mensaje: str) -> None:
    """
    Best-effort: cuando el agente no llega a una conclusión propia (se
    corta por límite de iteraciones o por un error de la API), deja
    igual una constancia concisa en el ticket en vez de fallar en
    silencio. Si publicar el comentario también falla, solo se loguea.
    """
    try:
        await client.call_tool(
            "add_ticket_comment", {"ticket_id": ticket_id, "comment": mensaje}
        )
    except Exception as e:
        print(f"[Agente] No se pudo publicar el comentario de fallback en Jira: {e!r}")


async def run_agent(
    user_prompt: str, ticket_id: str | None = None, post_comment: bool = True
) -> str:
    client = MCPClient(SERVER_SCRIPT)
    await client.connect()

    async def fallar(mensaje: str) -> str:
        if ticket_id and post_comment:
            await _post_fallback_comment(client, ticket_id, f"⚠️ {mensaje}")
        return mensaje

    try:
        # Nada de esto sabe de MCP: solo pide "las tools" y "ejecutá esta tool"
        anthropic_tools = await client.list_tools()
        messages = [{"role": "user", "content": user_prompt}]
        ultima_tool = None
        ultimo_resultado = None

        for _ in range(MAX_ITERATIONS):
            try:
                response = anthropic_client.messages.create(
                    model=MODEL,
                    max_tokens=1024,
                    tools=anthropic_tools,
                    messages=messages,
                )
            except anthropic.AnthropicError as e:
                return await fallar(f"Error al llamar a la API de Anthropic: {e}")

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "max_tokens":
                # Se cortó a mitad de respuesta (posiblemente a mitad de un
                # tool_use): devolvemos lo que haya de texto y avisamos.
                texto = _extract_text(response.content)
                aviso = "[Aviso] La respuesta se cortó por alcanzar max_tokens; puede estar incompleta."
                return f"{texto}\n\n{aviso}" if texto else aviso

            if response.stop_reason != "tool_use":
                # Claude terminó: extraemos el texto final y salimos del loop
                return _extract_text(response.content)

            # Claude pidió usar una o más tools: se las delegamos al MCP Client
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue

                print(f"[Agente] Llamando tool '{block.name}' con input {block.input}")
                ultima_tool = block.name
                try:
                    result_text = await client.call_tool(block.name, block.input)
                except Exception as e:
                    print(f"[Agente] Error ejecutando tool '{block.name}': {e!r}")
                    ultimo_resultado = f"Error ejecutando la tool: {e}"
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": f"Error ejecutando la tool '{block.name}': {e}",
                            "is_error": True,
                        }
                    )
                    continue

                ultimo_resultado = result_text
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    }
                )

            messages.append({"role": "user", "content": tool_results})

        resumen_falla = (
            f"No se pudo completar la revisión del ticket tras {MAX_ITERATIONS} "
            "intentos de tool-use."
        )
        if ultima_tool:
            resumen_falla += (
                f" Última tool usada: '{ultima_tool}'. Último resultado: "
                f"{(ultimo_resultado or '(sin datos)')[:400]}"
            )
        return await fallar(resumen_falla)
    finally:
        await client.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Agente QA: revisa si un objetivo cumple los criterios de un ticket."
    )
    parser.add_argument(
        "--ticket", required=True, help="ID del ticket a revisar (ej: SCRUM-1)"
    )
    parser.add_argument(
        "--target",
        required=False,
        default=None,
        help=(
            "Objetivo a testear: ruta a un .docx o URL de una página web. "
            "Si se omite, el agente intenta extraerlo de la descripción del ticket."
        ),
    )
    parser.add_argument(
        "--no-comment",
        action="store_true",
        help=(
            "No publicar el resultado como comentario en el ticket de Jira "
            "(por defecto sí se publica)."
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    instruccion_comentario = (
        ""
        if args.no_comment
        else (
            " Al terminar, publicá tu conclusión (cumple / no cumple / falta "
            "información, con tu razonamiento) como comentario en el ticket "
            "usando la tool add_ticket_comment."
        )
    )
    if args.target:
        objetivo_instruccion = f"comparalo contra este objetivo: '{args.target}'"
    else:
        objetivo_instruccion = (
            "extraé el objetivo a testear (una URL de una página web o la "
            "ruta a un documento .docx) de la descripción del ticket, y "
            "comparalo contra eso"
        )
    prompt = (
        f"Revisá el ticket {args.ticket} y {objetivo_instruccion}. Usá las "
        "tools disponibles que correspondan según el tipo de criterio "
        "(visual, de comportamiento, de contenido, etc.). Decime si cumple, "
        "no cumple, o si falta información para decidir, y explicá tu "
        "razonamiento." + instruccion_comentario
    )
    resultado = asyncio.run(
        run_agent(prompt, ticket_id=args.ticket, post_comment=not args.no_comment)
    )
    print("\n--- Resultado del Agente ---")
    print(resultado)