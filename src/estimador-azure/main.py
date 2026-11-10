"""Estimador de costes de Azure: un Hosted Agent de Microsoft Foundry con Agent Framework.

- El modelo entiende la carga de trabajo y busca en Microsoft Learn (servidor MCP) qué servicios usar.
- Los precios y las cuentas los hace el código de precios.py, nunca el modelo.
"""
import json
import os
import time

import httpx
from agent_framework import Agent, tool
from agent_framework.foundry import FoundryChatClient
from agent_framework_foundry_hosting import ResponsesHostServer
from azure.ai.agentserver.responses import ResponsesServerOptions
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from typing_extensions import Annotated

import precios

load_dotenv()

MONEDA = os.environ.get("MONEDA", "EUR")

# Medidores que ya ha visto el agente. Son precios públicos, así que se pueden compartir entre sesiones.
MEDIDORES: dict[str, precios.Medidor] = {}
MAX_CACHE = 5_000  # Al llegar se quitan los más antiguos: lo recién buscado por otra sesión sigue en caché
MAX_RECARGAS = 5  # Medidores sin buscar que calcular_estimacion pide a la api en una llamada: con meter_id inventados serían cientos
# Medidores convertidos sin el tipo de referencia: sirven para la estimación en curso, pero no se guardan mucho
# tiempo (su precio depende de qué filas trajo la consulta). meter_id -> (caduca, medidor)
PROVISIONALES: dict[str, tuple[float, precios.Medidor]] = {}
VIDA_PROVISIONAL = 600


def guardar(m: precios.Medidor):
	"""Guarda un medidor en la caché que le toca. Las dos están acotadas: son compartidas entre sesiones."""
	ahora = time.time()
	if m.provisional:
		for k in [k for k, (caduca, _) in PROVISIONALES.items() if caduca < ahora]:
			del PROVISIONALES[k]
		cache, valor = PROVISIONALES, (ahora + VIDA_PROVISIONAL, m)
	else:
		cache, valor = MEDIDORES, m
	cache.pop(m.meter_id, None)  # Al final del orden: es el más reciente
	cache[m.meter_id] = valor
	while len(cache) > MAX_CACHE:
		del cache[next(iter(cache))]  # El dict guarda el orden de entrada: el primero es el más antiguo


def conocidos() -> dict[str, precios.Medidor]:
	"""Copia de los medidores que se pueden usar ahora: otra sesión puede vaciar las cachés mientras esta espera a la api."""
	ahora = time.time()
	return {**{k: m for k, (caduca, m) in PROVISIONALES.items() if caduca >= ahora}, **MEDIDORES}

INSTRUCCIONES = f"""Eres un estimador de costes de Azure. Hablas en castellano, claro y sin rodeos.

Tu trabajo:
1. Entiende la carga que te describen: usuarios, datos, región, disponibilidad. Si falta algo importante,
   pregunta una sola vez; si no, decide un valor razonable y dilo como supuesto.
2. Busca en Microsoft Learn con microsoft_docs_search (como mucho 3 búsquedas) qué servicios y niveles
   encajan. Propón una arquitectura sencilla y guarda la URL de cada resultado que uses.
3. Para cada servicio, llama a buscar_precios con el nombre exacto del servicio en la api de precios
   (por ejemplo «Azure App Service», «SQL Database», «Storage», «Virtual Machines», «Functions»,
   «Azure Cosmos DB», «Event Hubs», «Azure Container Apps») y la región en formato
   de Azure Resource Manager (por ejemplo «swedencentral», «westeurope», «spaincentral»). Filtra siempre
   con sku_contiene o producto_contiene cuando sepas qué nivel quieres: sin filtro llegan decenas de medidores.
4. Elige los medidores que corresponden y llama UNA vez a calcular_estimacion con todas las líneas.
   Un servicio suele tener varios medidores obligatorios (cómputo, licencia, almacenamiento): inclúyelos todos.
   No añadas medidores de opciones que no usas (por ejemplo, cuotas de planes dedicados o de discos compartidos).

Reglas que no te puedes saltar:
- No escribas nunca un precio ni un importe que no venga de buscar_precios o de calcular_estimacion.
  No hagas cuentas tú: sumas, multiplicaciones y totales los hace calcular_estimacion.
- Si no encuentras un medidor, dilo y deja esa línea fuera; no te lo inventes.
- Las fuentes de Learn justifican la elección de cada servicio, no los precios: los precios salen de la api.
  Cita solo URLs que te haya devuelto microsoft_docs_search.
- Moneda: {MONEDA}.

Tu respuesta final lleva, en este orden:
1. La arquitectura propuesta en pocas líneas, con el porqué de cada servicio.
2. Una tabla en markdown (con cabecera y fila |---|) con las columnas servicio, SKU y medidor, cantidad, precio unitario
   y coste mensual, copiados de calcular_estimacion, y una última fila «**Total**» con el total mensual.
3. Una línea «**Total mensual:** <importe> EUR».
4. Los supuestos que devuelve calcular_estimacion y los tuyos.
5. Las fuentes de Microsoft Learn que has usado.
"""


@tool(approval_mode="never_require")
async def buscar_precios(
	servicio: Annotated[str, Field(description="Nombre del servicio en la api de precios, p. ej. «Azure App Service».")],
	region: Annotated[str, Field(description="Región de Azure en formato ARM, p. ej. «swedencentral».")],
	sku_contiene: Annotated[str | None, Field(description="Texto que debe aparecer en el SKU, p. ej. «P1 v3».")] = None,
	producto_contiene: Annotated[str | None, Field(description="Texto que debe aparecer en el producto, p. ej. «Linux».")] = None,
) -> str:
	"""Busca precios de pago por uso de un servicio de Azure en una región. Devuelve medidores con su meter_id."""
	filtro = precios.construir_filtro(servicio, region, sku_contiene, producto_contiene)
	try:
		medidores = precios.agrupar(await precios.consultar(filtro, MONEDA))
	except (RuntimeError, httpx.HTTPError) as e:  # Cupo de salidas agotado o api caída: que el modelo lo sepa y pare
		return json.dumps({"error": f"La api de precios no ha respondido: {e}"}, ensure_ascii=False)
	if not medidores:
		return json.dumps({"medidores": [], "aviso": "Sin resultados. Revisa el nombre del servicio, la región o el filtro de SKU."}, ensure_ascii=False)
	medidores.sort(key=lambda m: (m.producto, m.sku, m.nombre))
	# En la caché solo lo que ve el modelo: es compartida entre sesiones y sin tope crecería sin límite
	for m in medidores[: precios.MAX_RESULTADOS]:
		guardar(m)
	# Servicio y región van una sola vez y no en cada medidor: menos tokens en cada vuelta del bucle
	return json.dumps({
		"servicio": servicio,
		"region": region,
		"moneda": MONEDA,
		"total_encontrados": len(medidores),
		"medidores": [m.resumen() for m in medidores[: precios.MAX_RESULTADOS]],
	}, ensure_ascii=False)


class LineaEntrada(BaseModel):
	meter_id: str = Field(description="meter_id que devolvió buscar_precios, tal cual («meterId:skuId»).")
	cantidad: float = Field(description=(
		"Si el medidor se cobra por tiempo (Hour, Day, Year): unidades a la vez (2 instancias, 1 vCPU, 3,5 GiB), "
		"NUNCA un total mensual; las horas van en horas_mes. Si no: total del mes en la unidad del precio "
		"(30.000.000 escrituras con unidad «10K» son 3000; 500 GB con unidad «1 GB/Month» son 500)."
	))
	descripcion: str = Field(default="", description="Para qué es esta línea, en pocas palabras.")
	horas_mes: float | None = Field(
		default=None, gt=0, le=744,
		description="Horas al mes que está encendido, también si el medidor se cobra por día (730 = todo el mes; no son días). "
		"Déjalo vacío salvo que no esté encendido todo el mes.",
	)


@tool(approval_mode="never_require")
async def calcular_estimacion(
	lineas: Annotated[list[LineaEntrada], Field(max_length=40, description="Las líneas de la estimación (40 como mucho).")],
) -> str:
	"""Calcula el coste mensual de cada línea y el total con los precios oficiales. Es la única fuente de importes."""
	# Agent Framework entrega cada línea como dict: se valida aquí con el modelo de Pydantic
	lineas = [LineaEntrada.model_validate(ln) for ln in lineas]
	disponibles = conocidos()
	faltan = {ln.meter_id for ln in lineas if ln.meter_id not in disponibles}
	if len(faltan) > MAX_RECARGAS:
		return json.dumps({"error": f"Hay {len(faltan)} medidores que no están en la caché. Vuelve a buscar esos servicios con buscar_precios y usa sus meter_id."}, ensure_ascii=False)
	for meter_id in faltan:
		try:
			filas = await precios.consultar(precios.filtro_medidor(meter_id), MONEDA)
		except (RuntimeError, httpx.HTTPError) as e:
			return json.dumps({"error": f"La api de precios no ha respondido: {e}"}, ensure_ascii=False)
		for m in precios.agrupar(filas):
			disponibles[m.meter_id] = m
			guardar(m)
	try:
		resultado = precios.estimar([precios.Linea(**ln.model_dump()) for ln in lineas], disponibles)
	except ValueError as e:
		return json.dumps({"error": str(e)}, ensure_ascii=False)
	return json.dumps(resultado, ensure_ascii=False)


def main():
	client = FoundryChatClient(
		project_endpoint=os.environ["FOUNDRY_PROJECT_ENDPOINT"],
		model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME"],
		credential=DefaultAzureCredential(),
	)
	# Tope de vueltas y de llamadas a herramientas por pregunta: una inyección de prompt no puede encadenar
	# búsquedas sin fin y gastar los tokens por minuto que comparten todos los usuarios (una estimación normal usa ~10)
	client.function_invocation_configuration["max_iterations"] = 15
	client.function_invocation_configuration["max_function_calls"] = 25
	# Las llamadas que el modelo pide en un mismo lote se ejecutan en serie (no cuesta tokens: es local) y con un
	# tope de tiempo. Un lote grande puede pasar de 25 en su vuelta; las salidas a la api las acota precios._paginas
	client.function_invocation_configuration["allow_concurrent_invocation"] = False
	client.function_invocation_configuration["max_duration_seconds"] = 240
	learn = client.get_mcp_tool(
		name="microsoft_learn",
		url="https://learn.microsoft.com/api/mcp",
		# Solo la búsqueda: docs_fetch trae páginas enteras y el bucle de herramientas las reenvía en cada
		# llamada al modelo, así que se agotan los tokens por minuto del despliegue (error 429)
		allowed_tools=["microsoft_docs_search"],
		approval_mode="never_require",
	)
	agent = Agent(
		client=client,
		name="estimador-azure",
		instructions=INSTRUCCIONES,
		tools=[buscar_precios, calcular_estimacion, learn],
		# store: el historial lo guarda la plataforma de Hosted Agents.
		# max_tokens: sin límite, Azure reserva el máximo de salida del modelo en cada petición (~1M tokens)
		# contra los tokens por minuto del despliegue y da 429 aunque se usen muchos menos
		# max_tool_calls: tope de búsquedas del MCP alojado por respuesta del modelo (no cuentan en max_function_calls)
		default_options={"store": False, "max_tokens": 8000, "max_tool_calls": 5},
	)
	# from_env lee DEFAULT_FETCH_HISTORY_ITEM_COUNT (azure.yaml); sin pasarlo, el historial no tiene tope
	ResponsesHostServer(agent, options=ResponsesServerOptions.from_env()).run()


if __name__ == "__main__":
	main()
