"""Precios de Azure con la api pública Azure Retail Prices y cálculo de la estimación mensual.

Aquí no hay IA: el modelo decide qué servicios hacen falta, pero los precios y las cuentas
salen siempre de este módulo. Así una estimación se puede repetir y comprobar.

Documentación de la api: https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices
"""
from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import httpx

API = "https://prices.azure.com/api/retail/prices"
HORAS_MES = 730  # Las mismas horas al mes que usa la calculadora de precios de Azure
MAX_PAGINAS = 5  # shortcut: 1.000 filas por página, corta a 5.000 (Virtual Machines sin filtro de SKU pasa de 7.000 en swedencentral); súbelo si el modelo busca sin SKU
PRECISION = Decimal("0.01")  # Por debajo, el redondeo a 4 decimales de la api pierde más de un 0,5 %
MAX_RESULTADOS = 25  # Lo que devolvemos al modelo, para no llenarle el contexto
# Consulta fija para el tipo de cambio: 135 filas en una página, más de 120 con precio de sobra en EUR y USD
REFERENCIA = "serviceName eq 'Azure App Service' and armRegionName eq 'westeurope' and priceType eq 'Consumption'"
_tipos: dict[str, tuple[Decimal, float]] = {}  # moneda -> (tipo, instante)
_fallos_tipo: dict[str, float] = {}  # moneda -> instante del último fallo de la referencia
ESPERA_TRAS_FALLO = 300  # Segundos sin volver a pedir la referencia: si la api va lenta, no se hace cola detrás del cerrojo
VIDA_TIPO_VIEJO = 7 * 86400  # Hasta cuándo sirve el tipo de días anteriores si la referencia sigue caída
_cerrojo_tipos = asyncio.Lock()
# Salidas a la api desde todo el proceso (todas las sesiones): pocas a la vez y un cupo por minuto, para que
# una pregunta manipulada no pueda lanzar cientos de consultas aunque el modelo invente argumentos
MAX_SALIDAS_A_LA_VEZ = 4
MAX_SALIDAS_POR_MINUTO = 120
_salidas = asyncio.Semaphore(MAX_SALIDAS_A_LA_VEZ)
_ultimas_salidas: deque = deque()


@dataclass(frozen=True)
class Tramo:
	"""Un precio de un medidor a partir de cierta cantidad (los precios por volumen tienen varios)."""

	desde: Decimal
	precio: Decimal


@dataclass
class Medidor:
	"""Un medidor de facturación de Azure: lo que se cobra, por qué unidad y a qué precio."""

	meter_id: str  # «meterId:skuId». El meterId solo no basta: SQL Database lo comparte entre SKU con precios distintos
	servicio: str
	producto: str
	sku: str
	nombre: str
	unidad: str
	region: str
	moneda: str
	tramos: list[Tramo] = field(default_factory=list)
	convertido: bool = False  # Algún precio venía a 0 en la moneda pedida y se ha calculado desde USD
	provisional: bool = False  # Convertido sin el tipo de referencia: vale para esta respuesta, no para la caché

	def resumen(self) -> dict:
		return {
			"meter_id": self.meter_id,
			"producto": self.producto,
			"sku": self.sku,
			"medidor": self.nombre,
			"unidad": self.unidad,
			"precio": str(self.tramos[0].precio) if self.tramos else None,
			"tramos": len(self.tramos),
		}


def _texto(valor: str) -> str:
	"""Escapa una cadena para un filtro OData (las comillas simples se duplican)."""
	return valor.replace("'", "''")


def construir_filtro(
	servicio: str,
	region: str,
	sku_contiene: str | None = None,
	producto_contiene: str | None = None,
) -> str:
	"""Filtro OData para precios de pago por uso de un servicio en una región."""
	partes = [
		f"serviceName eq '{_texto(servicio)}'",
		f"armRegionName eq '{_texto(region.lower())}'",
		"priceType eq 'Consumption'",
	]
	if sku_contiene:
		partes.append(f"contains(skuName, '{_texto(sku_contiene)}')")
	if producto_contiene:
		partes.append(f"contains(productName, '{_texto(producto_contiene)}')")
	return " and ".join(partes)


def filtro_medidor(meter_id: str) -> str:
	"""Filtro OData para volver a pedir un medidor por su meter_id («meterId:skuId»)."""
	meter, _, sku = meter_id.partition(":")
	return f"meterId eq '{_texto(meter)}' and skuId eq '{_texto(sku)}' and priceType eq 'Consumption'"


async def _paginas(http: httpx.AsyncClient, filtro: str, moneda: str) -> list[dict]:
	"""Llama a la api y sigue la paginación. Devuelve las filas tal cual."""
	filas: list[dict] = []
	url: str | None = API
	params: dict | None = {"currencyCode": f"'{moneda}'", "$filter": filtro}
	for _ in range(MAX_PAGINAS):
		if not url:
			break
		ahora = time.time()
		while _ultimas_salidas and _ultimas_salidas[0] < ahora - 60:
			_ultimas_salidas.popleft()
		if len(_ultimas_salidas) >= MAX_SALIDAS_POR_MINUTO:
			raise RuntimeError("Demasiadas consultas a la api de precios en el último minuto; espera y vuelve a intentarlo.")
		_ultimas_salidas.append(ahora)
		async with _salidas:
			r = await http.get(url, params=params)
		r.raise_for_status()
		datos = r.json()
		filas.extend(datos.get("Items", []))
		url = datos.get("NextPageLink")
		params = None  # El enlace de la página siguiente ya lleva el filtro
	return filas


def _clave_tramo(f: dict) -> tuple:
	return f["meterId"], f.get("skuId"), f.get("tierMinimumUnits", 0)


def tipo_desde(filas: list[dict], filas_usd: list[dict]) -> Decimal | None:
	"""Mediana de precio en la moneda pedida / precio en USD de los tramos con precio de sobra en las dos."""
	usd = {_clave_tramo(f): Decimal(str(f["retailPrice"])) for f in filas_usd}
	ratios = sorted(
		Decimal(str(f["retailPrice"])) / usd[_clave_tramo(f)]
		for f in filas
		if Decimal(str(f["retailPrice"])) >= Decimal("0.1") and usd.get(_clave_tramo(f))
	)
	return ratios[len(ratios) // 2] if ratios else None


async def tipo_de_cambio(moneda: str, http: httpx.AsyncClient) -> Decimal | None:
	"""Un solo tipo de cambio por moneda para todo el proceso (24 h), sacado de una consulta fija.

	Así el precio de un medidor no depende de qué otras filas traía su consulta: la caché de medidores
	es compartida entre sesiones y una consulta estrecha no puede dejar a 0 lo que otra calculó bien.
	"""
	async with _cerrojo_tipos:  # Con la caché fría, varias consultas a la vez piden la referencia una sola vez
		guardado = _tipos.get(moneda)
		if guardado and guardado[1] > time.time() - 86400:
			return guardado[0]
		viejo = guardado[0] if guardado and guardado[1] > time.time() - VIDA_TIPO_VIEJO else None
		if _fallos_tipo.get(moneda, 0) > time.time() - ESPERA_TRAS_FALLO:
			return viejo
		try:
			filas = await _paginas(http, REFERENCIA, moneda)
			tipo = tipo_desde(filas, await _paginas(http, REFERENCIA, "USD"))
		except httpx.HTTPError:  # El RuntimeError del cupo propio no es un fallo de la api: sube hasta la herramienta
			tipo = None
		# Plausible: filas en la moneda pedida y un tipo razonable frente al dólar (si no, la api ignoró la moneda).
		# shortcut: el rango 0,2-5 vale para monedas cercanas al dólar (EUR, GBP, CHF); para JPY o INR, ampliarlo
		if tipo and all(f.get("currencyCode") == moneda for f in filas) and Decimal("0.2") < tipo < Decimal("5"):
			_tipos[moneda] = (tipo, time.time())
			return tipo
		_fallos_tipo[moneda] = time.time()
		return viejo  # Mejor el tipo de ayer que ninguno


def completar_desde_usd(filas: list[dict], filas_usd: list[dict], tipo: Decimal | None) -> None:
	"""Recalcula desde USD los precios que la api redondea demasiado fuera de USD.

	La api da los precios en otras monedas con 4 decimales: 0,000016 $ por GB·s (Functions Consumo) sale 0,0 en EUR
	y un precio de 0,00011 $ sale 0,0001 (un 10 % menos). Por debajo de 0,01 se recalcula todo desde USD.
	"""
	if not tipo:
		return  # shortcut: sin tipo de cambio (ni de referencia ni de la propia consulta) esos precios se quedan a 0
	usd = {_clave_tramo(f): Decimal(str(f["retailPrice"])) for f in filas_usd}
	for f in filas:
		precio_usd = usd.get(_clave_tramo(f))
		if Decimal(str(f["retailPrice"])) < PRECISION and precio_usd:
			f["retailPrice"] = str((precio_usd * tipo).quantize(Decimal("1e-10")))
			f["convertidoDesdeUsd"] = True


async def consultar(filtro: str, moneda: str = "EUR", http: httpx.AsyncClient | None = None) -> list[dict]:
	"""Filas de la api en la moneda pedida, con los precios minúsculos recuperados desde USD."""
	propio = http is None
	http = http or httpx.AsyncClient(timeout=30)
	try:
		filas = await _paginas(http, filtro, moneda)
		# Solo hace falta la segunda consulta si hay precios pequeños (tramos gratis o redondeos)
		if moneda != "USD" and any(Decimal(str(f.get("retailPrice") or 0)) < PRECISION for f in filas):
			filas_usd = await _paginas(http, filtro, "USD")
			tipo = await tipo_de_cambio(moneda, http)
			if tipo is None:
				# Sin referencia, el tipo de la propia consulta sirve para responder, pero depende de qué filas
				# trajo (y una consulta estrecha puede no tener ninguno): todo lo que había que convertir se marca
				# provisional, se convierta o se quede a 0, y no entra en la caché de larga duración
				usd = {_clave_tramo(f): Decimal(str(f["retailPrice"])) for f in filas_usd}
				for f in filas:
					if Decimal(str(f["retailPrice"])) < PRECISION and usd.get(_clave_tramo(f)):
						f["provisional"] = True
				completar_desde_usd(filas, filas_usd, tipo_desde(filas, filas_usd))
			else:
				completar_desde_usd(filas, filas_usd, tipo)
	finally:
		if propio:
			await http.aclose()
	return filas


def agrupar(filas: list[dict]) -> list[Medidor]:
	"""Junta las filas de un mismo medidor (una por tramo de precio) y ordena sus tramos."""
	medidores: dict[str, Medidor] = {}
	for f in filas:
		if f.get("type") != "Consumption":
			continue
		clave = f"{f['meterId']}:{f['skuId']}"
		m = medidores.get(clave)
		if m is None:
			m = Medidor(
				meter_id=clave,
				servicio=f["serviceName"],
				producto=f["productName"],
				sku=f["skuName"],
				nombre=f["meterName"],
				unidad=f["unitOfMeasure"],
				region=f["armRegionName"],
				moneda=f["currencyCode"],
			)
			medidores[m.meter_id] = m
		m.convertido = m.convertido or bool(f.get("convertidoDesdeUsd"))
		m.provisional = m.provisional or bool(f.get("provisional"))
		tramo = Tramo(Decimal(str(f.get("tierMinimumUnits", 0))), Decimal(str(f["retailPrice"])))
		if tramo not in m.tramos:
			m.tramos.append(tramo)
	for m in medidores.values():
		m.tramos.sort(key=lambda t: t.desde)
	return list(medidores.values())


# El multiplicador va pegado al número («10K», «1M»); «1 MB» son megabytes, no un millón
_UNIDAD = re.compile(r"^\s*(?P<n>[\d.]+)(?P<mult>[KM])?\b\s*(?P<resto>.*)$", re.IGNORECASE)
_PERIODOS = {"hour": 1, "day": 24, "year": 8760}  # Horas que cubre un precio que se cobra por tiempo


def interpretar_unidad(unidad: str) -> tuple[Decimal, Decimal | None]:
	"""Lee una unidad de medida de la api.

	Devuelve cuántas unidades cubre un precio («10K» → 10.000) y, si se cobra por tiempo, cuántas horas cubre.
	Ejemplos: «1 Hour» → (1, 1) · «1/Day» → (1, 24) · «10K» → (10000, None) · «1 GB/Month» → (1, None).
	"""
	m = _UNIDAD.match(unidad)
	if not m:
		return Decimal(1), None
	n = Decimal(m["n"])
	mult = {"K": Decimal(1000), "M": Decimal(1_000_000)}.get((m["mult"] or "").upper(), Decimal(1))
	resto = m["resto"].lower()
	periodo = next((Decimal(h) for nombre, h in _PERIODOS.items() if nombre in resto), None)
	return n * mult, periodo


def coste_por_tramos(tramos: list[Tramo], unidades: Decimal) -> Decimal:
	"""Aplica los precios por volumen: cada tramo cobra solo las unidades que caen en él."""
	total = Decimal(0)
	for i, t in enumerate(tramos):
		hasta = tramos[i + 1].desde if i + 1 < len(tramos) else None
		if unidades <= t.desde:
			break
		en_tramo = (min(unidades, hasta) if hasta is not None else unidades) - t.desde
		total += en_tramo * t.precio
	return total


def _dinero(x: Decimal) -> Decimal:
	return x.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass
class Linea:
	"""Una línea de la estimación: un medidor y la cantidad al mes."""

	meter_id: str
	cantidad: float
	descripcion: str = ""
	horas_mes: float | None = None  # Solo para medidores que se cobran por tiempo (hora, día, año); si no, 730


def estimar(lineas: list[Linea], medidores: dict[str, Medidor]) -> dict:
	"""Calcula el coste mensual de cada línea y el total. Todo con Decimal, sin redondeos intermedios."""
	salida, total, supuestos, moneda = [], Decimal(0), [], None
	for ln in lineas:
		m = medidores.get(ln.meter_id)
		if m is None:
			raise ValueError(f"Medidor desconocido: {ln.meter_id}. Búscalo antes con buscar_precios.")
		moneda = moneda or m.moneda
		bloque, periodo = interpretar_unidad(m.unidad)
		cantidad = Decimal(str(ln.cantidad))
		if periodo:
			horas = Decimal(str(ln.horas_mes if ln.horas_mes is not None else HORAS_MES))
			unidades = cantidad * horas / periodo / bloque
			detalle = f"{cantidad} × {horas} h"
		else:
			# La cantidad va en la unidad del precio (3000 de «10K»), igual que los tramos de la api
			unidades = cantidad
			detalle = f"{cantidad} × {m.unidad}" + (f" = {cantidad * bloque:,.0f}".replace(",", ".") if bloque != 1 else "")
		coste = coste_por_tramos(m.tramos, unidades)
		total += coste
		if m.convertido:
			supuestos.append(f"«{m.nombre}»: la api da su precio en {m.moneda} con 4 decimales; se ha calculado desde USD.")
		if len(m.tramos) > 1:
			supuestos.append(f"«{m.nombre}» tiene precios por volumen: se aplican sus {len(m.tramos)} tramos.")
		salida.append({
			"descripcion": ln.descripcion or m.nombre,
			"servicio": m.servicio,
			"sku": m.sku,
			"medidor": m.nombre,
			"meter_id": m.meter_id,
			"unidad": m.unidad,
			"cantidad": detalle,
			"precio_unitario": str(m.tramos[0].precio),
			"coste_mensual": str(_dinero(coste)),
		})
	supuestos += [
		f"Mes de {HORAS_MES} horas para lo que se cobra por tiempo (por hora, día o año), salvo que se indique otra cosa.",
		"Precios de lista de pago por uso, sin descuentos, reservas ni planes de ahorro.",
		"La api da los precios oficiales en USD; los importes en otras monedas son de referencia.",
		"No incluye impuestos.",
	]
	return {
		"moneda": moneda,
		"lineas": salida,
		"total_mensual": str(_dinero(total)),
		"supuestos": supuestos,
		"fuente": API,
		"fecha": date.today().isoformat(),
	}
