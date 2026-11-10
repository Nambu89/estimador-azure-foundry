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


async def test_consultar_recupera_desde_usd_los_precios_que_la_api_redondea_a_cero(monkeypatch):
	"""Respuestas reales de Functions en swedencentral (2026-11-09): en EUR, Consumo sale a 0,0 por el redondeo."""
	monkeypatch.setattr(precios, "_tipos", {})
	peticiones = []

	def responder(request: httpx.Request) -> httpx.Response:
		peticiones.append(request)
		return httpx.Response(200, json=_cargar("functions_usd.json" if "USD" in str(request.url) else "functions_eur.json"))

	async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
		medidores = precios.agrupar(await precios.consultar("filtro", "EUR", http))
	assert len(peticiones) == 4  # EUR, USD (hay precios a 0) y la consulta de referencia del tipo de cambio en las dos monedas
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


def _servidor_functions(peticiones, solo_standard_en_consulta):
	"""Responde la referencia con todas las filas de Functions y la consulta, si se pide, solo con las Standard."""
	def responder(request: httpx.Request) -> httpx.Response:
		peticiones.append(request)
		datos = _cargar("functions_usd.json" if "USD" in str(request.url) else "functions_eur.json")
		if solo_standard_en_consulta and "Azure+App+Service" not in str(request.url):
			datos = dict(datos, Items=[i for i in datos["Items"] if i["skuName"] == "Standard"])
		return httpx.Response(200, json=datos)
	return httpx.AsyncClient(transport=httpx.MockTransport(responder))


async def test_el_precio_convertido_no_depende_de_la_consulta(monkeypatch):
	"""Antes, una consulta sin filas de referencia (solo Standard) dejaba el precio a 0 en la caché compartida."""
	precios_vistos = []
	for estrecha in (False, True):
		monkeypatch.setattr(precios, "_tipos", {})
		async with _servidor_functions([], estrecha) as http:
			ms = precios.agrupar(await precios.consultar("filtro", "EUR", http))
		precios_vistos.append(next(m for m in ms if m.nombre == "Standard Execution Time").tramos[1].precio)
	assert precios_vistos[0] == precios_vistos[1] > 0


async def test_el_tipo_de_cambio_se_pide_una_vez(monkeypatch):
	monkeypatch.setattr(precios, "_tipos", {})
	peticiones = []
	async with _servidor_functions(peticiones, False) as http:
		await precios.consultar("filtro", "EUR", http)
		await precios.consultar("filtro", "EUR", http)
	assert sum("Azure+App+Service" in str(r.url) for r in peticiones) == 2  # EUR y USD, solo la primera vez


def _servidor_con_referencia(referencia_x, falla_referencia=False, peticiones=None):
	"""Referencia con un tipo distinto al de la consulta (precios EUR × referencia_x), o caída con un 500."""
	def responder(request: httpx.Request) -> httpx.Response:
		if peticiones is not None:
			peticiones.append(request)
		usd = "USD" in str(request.url)
		datos = _cargar("functions_usd.json" if usd else "functions_eur.json")
		if "Azure+App+Service" in str(request.url):
			if falla_referencia:
				return httpx.Response(500)
			if not usd:
				datos = dict(datos, Items=[dict(i, retailPrice=round(i["retailPrice"] * referencia_x, 4)) for i in datos["Items"]])
		return httpx.Response(200, json=datos)
	return httpx.AsyncClient(transport=httpx.MockTransport(responder))


async def test_se_usa_el_tipo_de_la_referencia_y_no_el_de_la_consulta(monkeypatch):
	monkeypatch.setattr(precios, "_tipos", {})
	async with _servidor_con_referencia(1.1) as http:
		ms = precios.agrupar(await precios.consultar("filtro", "EUR", http))
	tipo_ref = Decimal(str(round(0.1517 * 1.1, 4))) / Decimal("0.169")
	m = next(m for m in ms if m.nombre == "Standard Execution Time")
	assert m.tramos[1].precio == (Decimal("0.000016") * tipo_ref).quantize(Decimal("1e-10"))
	assert not m.provisional


async def test_si_la_referencia_falla_los_medidores_son_provisionales(monkeypatch):
	monkeypatch.setattr(precios, "_tipos", {})
	async with _servidor_con_referencia(1.0, falla_referencia=True) as http:
		ms = precios.agrupar(await precios.consultar("filtro", "EUR", http))  # no lanza: usa el tipo de la consulta
	m = next(m for m in ms if m.nombre == "Standard Execution Time")
	assert m.provisional and m.tramos[1].precio > 0
	premium = next(m for m in ms if m.nombre == "Premium vCPU Duration")
	assert not premium.provisional  # No se ha convertido: su precio no depende del tipo, puede ir a la caché


async def test_tras_un_fallo_de_la_referencia_no_se_reintenta_enseguida(monkeypatch):
	peticiones = []
	async with _servidor_con_referencia(1.0, falla_referencia=True, peticiones=peticiones) as http:
		await precios.consultar("filtro", "EUR", http)
		await precios.consultar("filtro", "EUR", http)
	assert sum("Azure+App+Service" in str(r.url) for r in peticiones) == 1  # solo la primera vez (falla en EUR)


async def test_si_la_referencia_falla_se_usa_el_tipo_de_ayer(monkeypatch):
	monkeypatch.setattr(precios, "_tipos", {"EUR": (Decimal("0.9"), precios.time.time() - 2 * 86400)})  # de anteayer
	async with _servidor_con_referencia(1.0, falla_referencia=True) as http:
		ms = precios.agrupar(await precios.consultar("filtro", "EUR", http))
	m = next(m for m in ms if m.nombre == "Standard Execution Time")
	assert m.tramos[1].precio == (Decimal("0.000016") * Decimal("0.9")).quantize(Decimal("1e-10"))
	assert not m.provisional


async def test_sin_referencia_una_consulta_estrecha_no_deja_ceros_en_la_cache(monkeypatch):
	"""Una consulta sin filas de 0,1 o más no tiene tipo propio: sus precios a 0 tienen que ser provisionales."""
	def responder(request: httpx.Request) -> httpx.Response:
		if "Azure+App+Service" in str(request.url):
			return httpx.Response(500)
		datos = _cargar("functions_usd.json" if "USD" in str(request.url) else "functions_eur.json")
		return httpx.Response(200, json=dict(datos, Items=[i for i in datos["Items"] if i["meterName"] == "Standard Execution Time"]))
	async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
		m, = precios.agrupar(await precios.consultar("filtro", "EUR", http))
	assert m.provisional  # aunque no se haya podido convertir


async def test_el_cupo_agotado_al_pedir_la_referencia_no_cuenta_como_fallo(monkeypatch):
	monkeypatch.setattr(precios, "MAX_SALIDAS_POR_MINUTO", 2)  # EUR y USD de la consulta; la referencia ya no cabe
	async with _servidor_con_referencia(1.0) as http:
		with pytest.raises(RuntimeError, match="Demasiadas consultas"):
			await precios.consultar("filtro", "EUR", http)
	assert not precios._fallos_tipo  # la api está bien: no se bloquea la referencia 5 minutos


async def test_durante_la_espera_tras_un_fallo_se_usa_el_tipo_de_ayer(monkeypatch):
	monkeypatch.setattr(precios, "_tipos", {"EUR": (Decimal("0.9"), precios.time.time() - 2 * 86400)})
	monkeypatch.setattr(precios, "_fallos_tipo", {"EUR": precios.time.time()})
	async with _servidor_con_referencia(1.0) as http:
		assert await precios.tipo_de_cambio("EUR", http) == Decimal("0.9")


async def test_un_tipo_de_hace_mas_de_una_semana_no_sirve(monkeypatch):
	monkeypatch.setattr(precios, "_tipos", {"EUR": (Decimal("0.9"), precios.time.time() - 8 * 86400)})
	async with _servidor_con_referencia(1.0, falla_referencia=True) as http:
		assert await precios.tipo_de_cambio("EUR", http) is None


async def test_con_la_cache_fria_la_referencia_se_pide_una_vez_aunque_haya_consultas_a_la_vez():
	import asyncio
	peticiones = []
	async with _servidor_con_referencia(1.0, peticiones=peticiones) as http:
		await asyncio.gather(*(precios.consultar("filtro", "EUR", http) for _ in range(3)))
	assert sum("Azure+App+Service" in str(r.url) for r in peticiones) == 2  # EUR y USD una sola vez


async def test_una_referencia_en_otra_moneda_no_se_guarda():
	"""Si la api ignora currencyCode y devuelve USD, el tipo saldría 1: no es plausible y no se guarda."""
	def responder(request: httpx.Request) -> httpx.Response:
		datos = _cargar("functions_usd.json" if "USD" in str(request.url) or "Azure+App+Service" in str(request.url) else "functions_eur.json")
		return httpx.Response(200, json=datos)
	async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as http:
		ms = precios.agrupar(await precios.consultar("filtro", "EUR", http))
	assert "EUR" not in precios._tipos
	assert next(m for m in ms if m.nombre == "Standard Execution Time").provisional


async def test_cupo_global_de_salidas_a_la_api(monkeypatch):
	monkeypatch.setattr(precios, "MAX_SALIDAS_POR_MINUTO", 3)
	monkeypatch.setattr(precios, "_ultimas_salidas", __import__("collections").deque())
	peticiones = []
	async with _cliente_simulado(peticiones) as http:
		await precios.consultar("filtro", "EUR", http)  # 2 páginas
		with pytest.raises(RuntimeError, match="Demasiadas consultas"):
			await precios.consultar("filtro", "EUR", http)
	assert len(peticiones) == 3
