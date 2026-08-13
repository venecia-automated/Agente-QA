from types import SimpleNamespace
from unittest.mock import AsyncMock

import anthropic

import agente_qa_1


def _text_block(text):
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(name, input_, id_="tool_1"):
    return SimpleNamespace(type="tool_use", name=name, input=input_, id=id_)


def _fake_response(stop_reason, content):
    return SimpleNamespace(stop_reason=stop_reason, content=content)


class FakeMCPClient:
    """Reemplaza a MCPClient para no hablar MCP de verdad en los tests."""

    def __init__(self, *_args, **_kwargs):
        self.closed = False
        self.call_tool = AsyncMock(return_value="resultado de la tool")

    async def connect(self):
        pass

    async def list_tools(self):
        return []

    async def close(self):
        self.closed = True


def test_extract_text_joins_only_text_blocks():
    content = [_text_block("Hola "), _tool_use_block("x", {}), _text_block("mundo")]
    assert agente_qa_1._extract_text(content) == "Hola mundo"


async def test_run_agent_returns_final_text(monkeypatch):
    monkeypatch.setattr(agente_qa_1, "MCPClient", FakeMCPClient)
    response = _fake_response("end_turn", [_text_block("Todo OK")])
    monkeypatch.setattr(
        agente_qa_1.anthropic_client,
        "messages",
        SimpleNamespace(create=lambda **kw: response),
    )
    resultado = await agente_qa_1.run_agent("chequeá el ticket X")
    assert resultado == "Todo OK"


async def test_run_agent_stops_at_max_iterations(monkeypatch):
    monkeypatch.setattr(agente_qa_1, "MCPClient", FakeMCPClient)
    response = _fake_response(
        "tool_use", [_tool_use_block("get_ticket", {"ticket_id": "X"})]
    )
    calls = {"n": 0}

    def fake_create(**kwargs):
        calls["n"] += 1
        return response

    monkeypatch.setattr(
        agente_qa_1.anthropic_client, "messages", SimpleNamespace(create=fake_create)
    )
    resultado = await agente_qa_1.run_agent("chequeá el ticket X")
    assert calls["n"] == agente_qa_1.MAX_ITERATIONS
    assert "límite" in resultado


async def test_run_agent_handles_anthropic_error(monkeypatch):
    monkeypatch.setattr(agente_qa_1, "MCPClient", FakeMCPClient)

    def raise_error(**kwargs):
        raise anthropic.AnthropicError("boom")

    monkeypatch.setattr(
        agente_qa_1.anthropic_client, "messages", SimpleNamespace(create=raise_error)
    )
    resultado = await agente_qa_1.run_agent("chequeá el ticket X")
    assert "Error al llamar a la API de Anthropic" in resultado


async def test_run_agent_reports_tool_error_without_crashing(monkeypatch):
    fake_client = FakeMCPClient()
    fake_client.call_tool = AsyncMock(side_effect=RuntimeError("tool rota"))
    monkeypatch.setattr(agente_qa_1, "MCPClient", lambda *a, **k: fake_client)

    first_response = _fake_response(
        "tool_use", [_tool_use_block("get_ticket", {"ticket_id": "X"})]
    )
    second_response = _fake_response("end_turn", [_text_block("listo")])
    responses = iter([first_response, second_response])

    monkeypatch.setattr(
        agente_qa_1.anthropic_client,
        "messages",
        SimpleNamespace(create=lambda **kw: next(responses)),
    )
    resultado = await agente_qa_1.run_agent("chequeá el ticket X")
    assert resultado == "listo"


async def test_run_agent_handles_max_tokens(monkeypatch):
    monkeypatch.setattr(agente_qa_1, "MCPClient", FakeMCPClient)
    response = _fake_response("max_tokens", [_text_block("respuesta cortada")])
    monkeypatch.setattr(
        agente_qa_1.anthropic_client,
        "messages",
        SimpleNamespace(create=lambda **kw: response),
    )
    resultado = await agente_qa_1.run_agent("chequeá el ticket X")
    assert "respuesta cortada" in resultado
    assert "max_tokens" in resultado
