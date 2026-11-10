"""Tests de las herramientas del agente tal como las llama Agent Framework (argumentos en JSON, sin red)."""
import json
from pathlib import Path
from types import SimpleNamespace

import httpx

import main
import precios

DATOS = Path(__file__).parent / "datos"


async def test_calcular_estimacion_acepta_lineas_como_dict(monkeypatch):
	filas = json.loads((DATOS / "real_swedencentral.json").read_text(encoding="utf-8"))["Items"]
	monkeypatch.setattr(main, "MEDIDORES", {m.meter_id: m for m in precios.agrupar(filas)})
	# Así llegan los argumentos desde el modelo: cada línea es un dict, no un LineaEntrada
	r = await main.calcular_estimacion.invoke(arguments={"lineas": [
		{"meter_id": "633d684b-ca73-56a2-a73d-900e394737c4:DZH318Z0BQHF/0236", "cantidad": 1, "descripcion": "SQL S0"},
	]})
	texto = "".join(c.text or "" for c in r)  # invoke devuelve una lista de Content
	assert "13.21" in texto  # «1/Day»: 0,4344 × 730 / 24


async def test_calcular_estimacion_rechaza_horas_imposibles():
	try:
		await main.calcular_estimacion.invoke(arguments={"lineas": [{"meter_id": "x:y", "cantidad": 1, "horas_mes": 900}]})
	except Exception as e:
		assert "horas_mes" in str(e) or "744" in str(e)
	else:
		raise AssertionError("debería rechazar horas_mes > 744")


async def test_calcular_estimacion_rechaza_demasiadas_lineas():
	lineas = [{"meter_id": "x:y", "cantidad": 1}] * 41
	try:
		await main.calcular_estimacion.invoke(arguments={"lineas": lineas})
	except Exception as e:
		assert "40" in str(e) or "lineas" in str(e)
	else:
		raise AssertionError("debería rechazar más de 40 líneas")


async def test_calcular_estimacion_no_pide_a_la_api_cientos_de_medidores_inventados(monkeypatch):
	async def prohibido(*a, **k):
		raise AssertionError("no debería consultar la api")
	monkeypatch.setattr(main, "MEDIDORES", {})
	monkeypatch.setattr(main.precios, "consultar", prohibido)
	lineas = [{"meter_id": f"inventado-{i}:x", "cantidad": 1} for i in range(main.MAX_RECARGAS + 1)]
	r = await main.calcular_estimacion.invoke(arguments={"lineas": lineas})
	assert "no están en la caché" in "".join(c.text or "" for c in r)


def _texto(r):
	return "".join(c.text or "" for c in r)


def _filas_provisionales():
	"""Filas de Functions como las deja consultar cuando la referencia del tipo de cambio ha caído."""
	filas = json.loads((DATOS / "functions_eur.json").read_text(encoding="utf-8"))["Items"]
	return [dict(f, provisional=True, convertidoDesdeUsd=True) for f in filas]


async def test_los_provisionales_sirven_para_estimar_sin_volver_a_buscar(monkeypatch):
	"""Antes no se guardaban en ninguna caché: con más de MAX_RECARGAS líneas, el modelo entraba en bucle de búsquedas."""
	llamadas = []
	async def consultar(filtro, moneda):
		llamadas.append(filtro)
		return _filas_provisionales()
	monkeypatch.setattr(main, "MEDIDORES", {})
	monkeypatch.setattr(main, "PROVISIONALES", {})
	monkeypatch.setattr(main.precios, "consultar", consultar)
	r = json.loads(_texto(await main.buscar_precios.invoke(arguments={"servicio": "Functions", "region": "swedencentral"})))
	assert not main.MEDIDORES  # la caché de larga duración no los guarda
	ids = [m["meter_id"] for m in r["medidores"]]
	assert len(ids) > main.MAX_RECARGAS
	e = await main.calcular_estimacion.invoke(arguments={"lineas": [{"meter_id": i, "cantidad": 1} for i in ids]})
	assert "total_mensual" in _texto(e)
	assert len(llamadas) == 1  # no ha vuelto a la api


async def test_los_provisionales_caducan(monkeypatch):
	monkeypatch.setattr(main, "MEDIDORES", {})
	monkeypatch.setattr(main, "PROVISIONALES", {})
	m = precios.agrupar(_filas_provisionales())[0]
	main.guardar(m)
	assert m.meter_id in main.conocidos()
	monkeypatch.setattr(main, "time", SimpleNamespace(time=lambda: 10**12))
	assert m.meter_id not in main.conocidos()


def test_el_bueno_manda_sobre_el_provisional_y_la_cache_desaloja_el_mas_antiguo(monkeypatch):
	filas = json.loads((DATOS / "functions_eur.json").read_text(encoding="utf-8"))["Items"]
	bueno, = [m for m in precios.agrupar(filas) if m.nombre == "Standard Execution Time"]
	provisional, = [m for m in precios.agrupar(_filas_provisionales()) if m.nombre == "Standard Execution Time"]
	main.guardar(bueno)
	main.guardar(provisional)
	assert main.conocidos()[bueno.meter_id] is bueno
	monkeypatch.setattr(main, "MAX_CACHE", 3)
	otros = precios.agrupar(filas)
	for m in otros:
		main.guardar(m)
	assert list(main.MEDIDORES) == [m.meter_id for m in otros[-3:]]  # se quitan los primeros, no se vacía entera


async def test_calcular_estimacion_con_la_api_caida_o_el_cupo_agotado(monkeypatch):
	for error in (RuntimeError("Demasiadas consultas"), httpx.ConnectError("sin red")):
		async def falla(*a, **k):
			raise error
		monkeypatch.setattr(main.precios, "consultar", falla)
		r = await main.calcular_estimacion.invoke(arguments={"lineas": [{"meter_id": "x:y", "cantidad": 1}]})
		assert "no ha respondido" in json.loads(_texto(r))["error"]


async def test_calcular_estimacion_con_un_medidor_que_no_existe(monkeypatch):
	async def vacio(*a, **k):
		return []
	monkeypatch.setattr(main.precios, "consultar", vacio)
	r = await main.calcular_estimacion.invoke(arguments={"lineas": [{"meter_id": "x:y", "cantidad": 1}]})
	assert "error" in json.loads(_texto(r))


async def test_cupo_de_la_api_agotado_llega_al_modelo_como_error(monkeypatch):
	async def agotado(*a, **k):
		raise RuntimeError("Demasiadas consultas a la api de precios")
	monkeypatch.setattr(main.precios, "consultar", agotado)
	r = await main.buscar_precios.invoke(arguments={"servicio": "Functions", "region": "swedencentral"})
	assert "Demasiadas consultas" in json.loads(_texto(r))["error"]
	async def caida(*a, **k):
		raise httpx.ConnectError("sin red")
	monkeypatch.setattr(main.precios, "consultar", caida)
	r = await main.buscar_precios.invoke(arguments={"servicio": "Functions", "region": "swedencentral"})
	assert "no ha respondido" in json.loads(_texto(r))["error"]


def test_main_configura_topes_e_historial(monkeypatch):
	"""Arranca main() con dobles (sin red) y comprueba lo que recibe cada pieza."""
	import re
	visto = {}

	class Cliente:
		def __init__(self, **k):
			self.function_invocation_configuration = {}
			visto["cliente"] = self

		def get_mcp_tool(self, **k):
			visto["mcp"] = k
			return "mcp"

	class Servidor:
		def __init__(self, agente, options=None):
			visto["options"] = options

		def run(self):
			pass

	yaml = (Path(__file__).parent.parent / "azure.yaml").read_text(encoding="utf-8")
	historial = re.search(r'DEFAULT_FETCH_HISTORY_ITEM_COUNT:\s*"(\d+)"', yaml).group(1)
	monkeypatch.setenv("DEFAULT_FETCH_HISTORY_ITEM_COUNT", historial)
	monkeypatch.setenv("FOUNDRY_PROJECT_ENDPOINT", "https://x")
	monkeypatch.setenv("AZURE_AI_MODEL_DEPLOYMENT_NAME", "m")
	monkeypatch.setattr(main, "FoundryChatClient", Cliente)
	monkeypatch.setattr(main, "DefaultAzureCredential", lambda: None)
	monkeypatch.setattr(main, "Agent", lambda **k: visto.setdefault("agente", k))
	monkeypatch.setattr(main, "ResponsesHostServer", Servidor)
	main.main()
	assert visto["options"].default_fetch_history_count == int(historial) >= 30 * 8  # 8 turnos de unos 30 items
	assert visto["agente"]["default_options"] == {"store": False, "max_tokens": 8000, "max_tool_calls": 5}
	assert visto["cliente"].function_invocation_configuration == {
		"max_iterations": 15, "max_function_calls": 25, "allow_concurrent_invocation": False, "max_duration_seconds": 240}
	assert visto["mcp"]["allowed_tools"] == ["microsoft_docs_search"]
	assert visto["mcp"]["approval_mode"] == "never_require"
