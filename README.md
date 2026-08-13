# Agente QA

Agente que usa Claude (vía la API de Anthropic) y el protocolo MCP para
revisar si un objetivo real -- un documento `.docx` o una página web --
cumple los criterios de aceptación de un ticket de Jira.

## Arquitectura

- `agente_qa_1.py` -- orquesta la conversación con Claude y decide
  cuándo pedirle al MCP Client que ejecute una tool.
- `mcp_client.py` -- encapsula el protocolo MCP (conectar, listar
  tools, ejecutar tools).
- `mcp_server_qa.py` -- servidor MCP que expone las tools: `get_ticket`
  (Jira), `get_target_content` (lectura de `.docx`), `get_computed_style`
  y `check_link_redirect` (inspección de una web real con Playwright).

## Requisitos

- Python 3.10+
- Una API key de Anthropic
- Credenciales de una cuenta de Jira Cloud (dominio, email y API token)

## Instalación

```bash
git clone https://github.com/venecia-automated/Agente-QA.git
cd Agente-QA

python3 -m venv venv
source venv/bin/activate

pip install -r requirements.txt
playwright install chromium
```

## Configuración

Creá un archivo `.env` en la raíz del proyecto con:

```
ANTHROPIC_API_KEY=tu-api-key-de-anthropic
JIRA_DOMAIN=tu-dominio.atlassian.net
JIRA_EMAIL=tu-email@ejemplo.com
JIRA_API_TOKEN=tu-api-token-de-jira
```

El API token de Jira se genera en
`https://id.atlassian.com/manage-profile/security/api-tokens`.

## Uso

```bash
python agente_qa_1.py --ticket SCRUM-1 --target ./ruta/al/objetivo.docx
# o contra una web real:
python agente_qa_1.py --ticket SCRUM-1 --target https://ejemplo.com
```

El agente trae el ticket, decide qué tools usar según el tipo de
criterio (visual, de comportamiento, de contenido), y responde si el
objetivo cumple, no cumple, o si falta información para decidir.

## Tests

```bash
pytest -v
```
