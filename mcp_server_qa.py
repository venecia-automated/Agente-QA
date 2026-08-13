"""
mcp_server_qa.py

Servidor MCP para el proyecto QA Agent.
Expone cuatro tools:
  - get_ticket: trae título y descripción de un ticket real de Jira
  - get_target_content: lee el doc de Word que simula "la web" a testear
  - get_computed_style: abre una URL real con Playwright y devuelve
    TODAS las propiedades CSS computadas de un elemento
  - check_link_redirect: clickea un elemento con Playwright y reporta
    a qué URL terminó navegando (misma pestaña o pestaña nueva)

Las tools de Playwright comparten un único browser Chromium, lanzado
una vez al arrancar el servidor (ver app_lifespan) y reusado en cada
tool call.

Transporte: stdio (el cliente/host lanza este script como subproceso).
"""

import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import requests
from dotenv import load_dotenv
from docx import Document
from mcp.server.fastmcp import Context, FastMCP
from playwright.async_api import (
    Browser,
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)
from requests.auth import HTTPBasicAuth

load_dotenv()

JIRA_DOMAIN = os.getenv("JIRA_DOMAIN")
JIRA_EMAIL = os.getenv("JIRA_EMAIL")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN")


@dataclass
class AppContext:
    playwright: Playwright
    browser: Browser


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """
    Lanza un único browser Chromium cuando arranca el servidor y lo
    reusa para todas las tools de Playwright durante toda la corrida
    del agente, en vez de lanzar uno nuevo por cada tool call.
    """
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch()
    try:
        yield AppContext(playwright=playwright, browser=browser)
    finally:
        await browser.close()
        await playwright.stop()


# Nombre del servidor: así lo va a ver el agente al conectarse
mcp = FastMCP("qa-agent-tools", lifespan=app_lifespan)


class ElementNotFoundError(Exception):
    """El selector no matcheó ningún elemento visible a tiempo."""


@asynccontextmanager
async def _open_page_with_element(
    browser: Browser, url: str, selector: str
) -> AsyncIterator[tuple[Page, Locator]]:
    """
    Abre una página nueva en `url` (dentro del browser compartido) y
    ubica el primer elemento visible que matchee `selector`. Comparte
    el boilerplate de Playwright entre todas las tools que necesitan
    "ir a una URL y agarrar un elemento" (get_computed_style,
    check_link_redirect).

    Lanza ElementNotFoundError si el elemento no aparece a tiempo.
    Cierra la página (y su browser context) al salir del bloque.
    """
    page = await browser.new_page()
    try:
        await page.goto(url, wait_until="load", timeout=20000)
        locator = page.locator(selector).first
        try:
            await locator.wait_for(state="visible", timeout=10000)
        except Exception:
            raise ElementNotFoundError(
                f"No se encontró ningún elemento visible que matchee "
                f"el selector '{selector}' en '{url}'."
            )
        yield page, locator
    finally:
        await page.close()


def adf_to_text(node) -> str:
    """
    Convierte un nodo de Atlassian Document Format (el JSON en el que
    Jira Cloud guarda texto enriquecido) a texto plano. Es una versión
    simplificada: soporta párrafos, títulos y listas, que cubre la
    mayoría de las descripciones de tickets. No soporta tablas ni
    contenido más avanzado (menciones, emojis, etc.).
    """
    if not isinstance(node, dict):
        return ""

    tipo = node.get("type")
    hijos = "".join(adf_to_text(hijo) for hijo in node.get("content", []) or [])

    if tipo == "text":
        return node.get("text", "")
    if tipo in ("paragraph", "heading"):
        return hijos + "\n"
    if tipo == "listItem":
        return f"- {hijos}\n"
    return hijos


@mcp.tool()
def get_ticket(ticket_id: str) -> str:
    """
    Devuelve el título y la descripción de un ticket real de Jira,
    dado su ID (por ejemplo 'SCRUM-1'). Usa la API REST de Jira Cloud
    con autenticación por email + API token, leídos desde el .env.
    """
    if not (JIRA_DOMAIN and JIRA_EMAIL and JIRA_API_TOKEN):
        return (
            "Faltan credenciales de Jira en el .env "
            "(JIRA_DOMAIN, JIRA_EMAIL, JIRA_API_TOKEN)."
        )

    url = f"https://{JIRA_DOMAIN}/rest/api/3/issue/{ticket_id}"
    try:
        response = requests.get(
            url,
            auth=HTTPBasicAuth(JIRA_EMAIL, JIRA_API_TOKEN),
            headers={"Accept": "application/json"},
            timeout=10,
        )
    except requests.RequestException as e:
        return f"Error de conexión al pedir el ticket '{ticket_id}': {e}"

    if response.status_code == 404:
        return f"No se encontró ningún ticket con id '{ticket_id}'."
    if response.status_code == 401:
        return "No autorizado por Jira: revisá JIRA_EMAIL y JIRA_API_TOKEN en tu .env."
    if response.status_code != 200:
        return (
            f"Jira devolvió un error inesperado ({response.status_code}): "
            f"{response.text[:300]}"
        )

    data = response.json()
    fields = data.get("fields", {})
    titulo = fields.get("summary", "(sin título)")
    descripcion = adf_to_text(fields.get("description")).strip() or "(sin descripción)"

    return f"Ticket: {ticket_id}\nTítulo: {titulo}\nDescripción: {descripcion}"


@mcp.tool()
def get_target_content(doc_path: str) -> str:
    """
    Lee un documento de Word (.docx) y devuelve su contenido indicando,
    para cada fragmento de texto, si está en negrita o no.

    Se usa cuando el "objetivo" a testear es un documento de Word (no
    una web real -- para eso está get_computed_style).

    Args:
        doc_path: ruta al archivo .docx a leer.
    """
    try:
        document = Document(doc_path)
    except Exception as e:
        return f"No se pudo leer el documento en '{doc_path}': {e}"

    lineas = []
    for i, parrafo in enumerate(document.paragraphs, start=1):
        if not parrafo.text.strip():
            continue

        fragmentos = []
        for run in parrafo.runs:
            if not run.text.strip():
                continue
            estado = "negrita" if run.bold else "sin negrita"
            fragmentos.append(f'"{run.text}" ({estado})')

        if fragmentos:
            lineas.append(f"Párrafo {i}: " + " | ".join(fragmentos))
        else:
            lineas.append(f'Párrafo {i}: "{parrafo.text}" (formato no detectado)')

    if not lineas:
        return "El documento está vacío o no tiene texto legible."

    return "\n".join(lineas)


@mcp.tool()
async def get_computed_style(url: str, selector: str, ctx: Context) -> str:
    """
    Abre una URL real en el navegador Chromium compartido (vía Playwright),
    espera a que la página renderice su JavaScript, ubica el primer
    elemento que matchee `selector`, y devuelve TODAS sus propiedades
    CSS computadas -- el mismo resultado final que se ve en la pestaña
    "Computed" de Chrome DevTools (color, background-color, font-weight,
    padding, bordes, etc., todo junto).

    Usar esta tool para cualquier criterio de aceptación sobre el
    aspecto visual de una página web real (no un doc de Word).

    Args:
        url: URL completa de la página a inspeccionar
            (ej: 'https://ejemplo.com/login').
        selector: selector del elemento a inspeccionar. Acepta CSS
            estándar (ej: 'button.login', '#submit'), o con prefijo
            explícito 'text=Ingresar' para buscar por el texto visible
            del elemento, o 'xpath=...'.
    """
    browser = ctx.request_context.lifespan_context.browser
    try:
        async with _open_page_with_element(browser, url, selector) as (_page, locator):
            texto_visible = await locator.inner_text()
            estilos = await locator.evaluate(
                "el => { const s = getComputedStyle(el); const r = {}; "
                "for (const prop of s) { r[prop] = s.getPropertyValue(prop); } "
                "return r; }"
            )
    except ElementNotFoundError as e:
        return str(e)
    except Exception as e:
        print(f"[get_computed_style] error inesperado: {e!r}", file=sys.stderr)
        return f"Error al inspeccionar '{selector}' en '{url}': {e}"

    return (
        f"Elemento encontrado (selector: '{selector}')\n"
        f'Texto visible: "{texto_visible}"\n'
        f"Propiedades CSS computadas:\n{json.dumps(estilos, indent=2, ensure_ascii=False)}"
    )


@mcp.tool()
async def check_link_redirect(url: str, selector: str, ctx: Context) -> str:
    """
    Abre `url`, hace click de verdad (con Playwright) sobre el primer
    elemento que matchee `selector`, y reporta a qué URL terminó
    navegando el navegador -- ya sea en la misma pestaña o en una
    pestaña nueva (ej: links con target="_blank").

    Usar esta tool para criterios de aceptación sobre COMPORTAMIENTO
    de navegación (ej: "el botón X debe redirigir a Y"), no para
    aspecto visual -- para eso está get_computed_style. No lee el
    atributo href del HTML porque en apps modernas (SPAs) la
    navegación suele manejarse por JavaScript, no por un link
    tradicional -- clickear de verdad es más confiable.

    Args:
        url: URL completa de la página donde está el elemento a clickear.
        selector: selector del elemento a clickear. Acepta CSS estándar,
            o 'text=...' para buscar por el texto visible del elemento.
    """
    browser = ctx.request_context.lifespan_context.browser
    try:
        async with _open_page_with_element(browser, url, selector) as (page, locator):
            url_antes = page.url

            nueva_pagina = None
            try:
                async with page.context.expect_page(timeout=5000) as new_page_info:
                    await locator.click()
                nueva_pagina = await new_page_info.value
            except PlaywrightTimeoutError:
                pass  # no se abrió pestaña nueva; puede haber navegado en la misma

            if nueva_pagina is not None:
                await nueva_pagina.wait_for_load_state("load", timeout=8000)
                url_despues = nueva_pagina.url
                abrio_pestana_nueva = True
            else:
                try:
                    await page.wait_for_url(
                        lambda nueva_url: nueva_url != url_antes, timeout=8000
                    )
                except Exception:
                    pass  # puede que la URL no haya cambiado; lo reportamos igual
                url_despues = page.url
                abrio_pestana_nueva = False
    except ElementNotFoundError as e:
        return str(e)
    except Exception as e:
        print(f"[check_link_redirect] error inesperado: {e!r}", file=sys.stderr)
        return f"Error al clickear '{selector}' en '{url}': {e}"

    cambio = abrio_pestana_nueva or url_despues != url_antes
    return (
        f"URL antes del click: {url_antes}\n"
        f"URL después del click: {url_despues}\n"
        f"¿Abrió una pestaña nueva?: {'Sí' if abrio_pestana_nueva else 'No'}\n"
        f"¿Cambió la URL?: {'Sí' if cambio else 'No'}"
    )


if __name__ == "__main__":
    # stdio: este script corre como subproceso, hablando por stdin/stdout
    # con quien lo lance (nuestro agente agente_qa_1.py)
    mcp.run(transport="stdio")