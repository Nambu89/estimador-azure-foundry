"""Tests del cálculo de precios. No llaman a Azure: usan respuestas guardadas con la forma real de la api.

Los precios de los ficheros de datos son inventados y redondos para que las cuentas se comprueben a mano.
"""
import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

import precios

DATOS = Path(__file__).parent / "datos"
# meter_id = «meterId:skuId», como lo devuelve agrupar
APP, BLOB, ESCRITURAS = "app-p1v3:PRUEBA0001/0001", "blob-hot-lrs:PRUEBA0002/0001", "blob-escrituras:PRUEBA0002/0001"


def _cargar(nombre):
	return json.loads((DATOS / nombre).read_text(encoding="utf-8"))


def _cliente_simulado(peticiones):
	"""Cliente HTTP que devuelve la página 1 y, al seguir NextPageLink, la página 2."""

	def responder(request: httpx.Request) -> httpx.Response:
		peticiones.append(request)
		pagina = "pagina2.json" if "skip=1000" in str(request.url) else "app_service_y_storage.json"
		return httpx.Response(200, json=_cargar(pagina))

	return httpx.AsyncClient(transport=httpx.MockTransport(responder))


async def _medidores():
	peticiones = []
	async with _cliente_simulado(peticiones) as http:
		filas = await precios.consultar("filtro", "EUR", http)
	return {m.meter_id: m for m in precios.agrupar(filas)}, peticiones


def test_filtro_escapa_comillas_y_pone_la_region_en_minusculas():
	f = precios.construir_filtro("Azure App Service", "SwedenCentral", sku_contiene="P1 v3", producto_contiene="O'Brien")
	assert "armRegionName eq 'swedencentral'" in f
	assert "priceType eq 'Consumption'" in f
	assert "contains(skuName, 'P1 v3')" in f
	assert "contains(productName, 'O''Brien')" in f


async def test_consultar_sigue_la_paginacion_y_pide_euros():
	medidores, peticiones = await _medidores()
	assert len(peticiones) == 2
	assert "currencyCode=%27EUR%27" in str(peticiones[0].url)
	assert set(medidores) == {APP, BLOB, ESCRITURAS}


async def test_agrupar_descarta_reservas_y_junta_los_tramos():
	medidores, _ = await _medidores()
	assert "app-p1v3-reserva:PRUEBA0001/0002" not in medidores
	assert [t.desde for t in medidores[BLOB].tramos] == [Decimal(0), Decimal(51200)]


@pytest.mark.parametrize(
	"unidad, esperado",
	[
		("1 Hour", (Decimal(1), Decimal(1))),
		("100 Hours", (Decimal(100), Decimal(1))),
		("1 GiB/Hour", (Decimal(1), Decimal(1))),
		("1/Day", (Decimal(1), Decimal(24))),
		("1/Year", (Decimal(1), Decimal(8760))),
		("10K", (Decimal(10000), None)),
		("10K/Month", (Decimal(10000), None)),
		("1M", (Decimal(1_000_000), None)),
		("1 MB", (Decimal(1), None)),  # megabytes, no un millón
		("1 GB/Month", (Decimal(1), None)),
		("1/Month", (Decimal(1), None)),
	],
)
def test_interpretar_unidad(unidad, esperado):
	assert precios.interpretar_unidad(unidad) == esperado


def test_coste_por_tramos_cobra_cada_tramo_a_su_precio():
	tramos = [precios.Tramo(Decimal(0), Decimal("0.02")), precios.Tramo(Decimal(51200), Decimal("0.019"))]
	assert precios.coste_por_tramos(tramos, Decimal(1000)) == Decimal("20.00")
	# 51.200 GB a 0,02 + 8.800 GB a 0,019 = 1.024 + 167,2
	assert precios.coste_por_tramos(tramos, Decimal(60000)) == Decimal("1191.200")


async def test_estimar_suma_horas_almacenamiento_y_operaciones():
	medidores, _ = await _medidores()
	lineas = [
		precios.Linea(APP, 2, "Web"),                   # 2 × 730 h × 0,20 = 292,00
		precios.Linea(BLOB, 500, "Ficheros"),         # 500 GB × 0,02 = 10,00
		precios.Linea(ESCRITURAS, 300, "Subidas"),  # 300 de «10K» (3.000.000 escrituras) × 0,05 = 15,00
	]
	r = precios.estimar(lineas, medidores)
	assert [ln["coste_mensual"] for ln in r["lineas"]] == ["292.00", "10.00", "15.00"]
	assert r["total_mensual"] == "317.00"
	assert r["moneda"] == "EUR"
	assert any("730 horas" in s for s in r["supuestos"])


async def test_estimar_respeta_las_horas_indicadas():
	medidores, _ = await _medidores()
	r = precios.estimar([precios.Linea(APP, 1, horas_mes=200)], medidores)
	assert r["total_mensual"] == "40.00"


async def test_estimar_rechaza_un_medidor_que_no_se_ha_buscado():
	medidores, _ = await _medidores()
	with pytest.raises(ValueError, match="Medidor desconocido"):
		precios.estimar([precios.Linea("inventado", 1)], medidores)


def test_filtro_medidor_pide_meter_y_sku_escapados():
	assert precios.filtro_medidor("abc:X/1") == "meterId eq 'abc' and skuId eq 'X/1' and priceType eq 'Consumption'"
	assert "meterId eq 'a''b'" in precios.filtro_medidor("a'b:X")


async def test_respuesta_real_de_la_api():
	"""Filas reales de swedencentral en EUR (capturadas el 2026-11-09), con lo que rompía la versión anterior."""
	medidores = {m.meter_id: m for m in precios.agrupar(_cargar("real_swedencentral.json")["Items"])}
	linux = "a19e37c1-d748-575b-a4d1-a034f2654c11:DZH318Z0DCR6/008D"
	hot_lrs = "39d8c6c1-22c0-5719-9330-593a56b3a2e0:DZH318Z0BPH7/01F8"
	escrituras = "0ce2115f-eedc-5ac1-82ed-b0651b1d7e5b:DZH318Z0BPH7/01F8"
	sql_2 = "f413518b-3ad9-55b3-a3b5-48f500672226:DZH318Z0BPXL/05NZ"
	sql_8 = "f413518b-3ad9-55b3-a3b5-48f500672226:DZH318Z0BPXL/05NQ"
	s0 = "633d684b-ca73-56a2-a73d-900e394737c4:DZH318Z0BQHF/0236"
	# Mismo meterId, dos SKU con precios distintos: tienen que ser dos medidores
	assert medidores[sql_2].tramos == [precios.Tramo(Decimal(0), Decimal("0.3006"))]
	assert medidores[sql_8].tramos == [precios.Tramo(Decimal(0), Decimal("1.2025"))]
	assert [t.desde for t in medidores[hot_lrs].tramos] == [Decimal(0), Decimal(51200), Decimal(512000)]
	r = precios.estimar([
		precios.Linea(linux, 1),             # 0,1598 × 730 h = 116,654
		precios.Linea(hot_lrs, 60000),       # 51.200 GB × 0,0165 + 8.800 GB × 0,0159 = 984,72
		precios.Linea(escrituras, 300),      # 300 de «10K» × 0,0449 = 13,47
		precios.Linea(sql_2, 1),             # 0,3006 × 730 h = 219,438
		precios.Linea(s0, 1),                # «1/Day»: 0,4344 × 730 / 24 = 13,213
	], medidores)
	assert [ln["coste_mensual"] for ln in r["lineas"]] == ["116.65", "984.72", "13.47", "219.44", "13.21"]
	assert r["total_mensual"] == "1347.50"  # 1.347,495 sin redondeos intermedios


async def test_consultar_recupera_desde_usd_los_precios_que_la_api_redondea_a_cero():
	"""Respuestas reales de Functions en swedencentral (2026-11-09): en EUR, Consumo sale a 0,0 por el redondeo."""
	peticiones = []

	def responder(request: httpx.Request) -> httpx.Response:
		peticiones.append(request)
		return httpx.Response(200, json=_cargar("functions_usd.json" if "USD" in str(request.url) else "functions_eur.json"))

	async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
		medidores = precios.agrupar(await precios.consultar("filtro", "EUR", http))
	assert len(peticiones) == 2  # EUR y, como hay precios a 0, USD
	tiempo = next(m for m in medidores if m.nombre == "Standard Execution Time")
	# Tramo gratis (0 en las dos monedas) se queda a 0; el de pago: 0,000016 $ × (0,1517 / 0,169)
	assert tiempo.tramos[0].precio == 0
	assert abs(tiempo.tramos[1].precio - Decimal("0.000016") * Decimal("0.1517") / Decimal("0.169")) < Decimal("1e-10")  # se guarda con 10 decimales
	assert tiempo.convertido
	premium = next(m for m in medidores if m.nombre == "Premium vCPU Duration")
	assert premium.tramos[0].precio == Decimal("0.1517") and not premium.convertido
	r = precios.estimar([precios.Linea(tiempo.meter_id, 1_400_000)], {tiempo.meter_id: tiempo})
	assert any("desde USD" in x for x in r["supuestos"])
	assert r["total_mensual"] == "14.36"  # (1,4M − 0,4M gratis) GB·s × 1,436e-05 €


async def test_estimar_muestra_a_que_equivale_la_cantidad_en_unidades_base():
	medidores, _ = await _medidores()
	r = precios.estimar([precios.Linea(ESCRITURAS, 3000)], medidores)
	assert r["lineas"][0]["cantidad"] == "3000 × 10K = 30.000.000"
	assert r["total_mensual"] == "150.00"  # 3000 × 0,05
