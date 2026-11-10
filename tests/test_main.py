"""Tests de las herramientas del agente tal como las llama Agent Framework (argumentos en JSON, sin red)."""
import json
from pathlib import Path

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
