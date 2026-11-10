"""Genera web/simulado/simulado-estimacion.sse con la forma real del stream Responses del agente."""
import asyncio, json, sys
sys.path.insert(0, sys.argv[1])
import precios

async def estimacion():
	M = {}
	for s, sk, pr in [("Azure App Service", "P1 v3", "Linux"), ("SQL Database", "2 vCore", "General Purpose - Compute Gen5"), ("SQL Database", None, "General Purpose - Storage"), ("Storage", "Hot LRS", "General Block Blob v2")]:
		for x in precios.agrupar(await precios.consultar(precios.construir_filtro(s, "swedencentral", sk, pr), "EUR")):
			M[x.meter_id] = x
	L = [
		precios.Linea("a19e37c1-d748-575b-a4d1-a034f2654c11:DZH318Z0DCR6/008D", 1, "Aplicación web (Linux)"),
		precios.Linea("f413518b-3ad9-55b3-a3b5-48f500672226:DZH318Z0BPXL/05NZ", 1, "Base de datos"),
		precios.Linea("24bf0b7c-5071-5f0f-8632-af57636f125d:DZH318Z0BQDW/004H", 50, "Almacenamiento de la base de datos"),
		precios.Linea("39d8c6c1-22c0-5719-9330-593a56b3a2e0:DZH318Z0BPH7/01F8", 500, "Ficheros"),
	]
	return precios.estimar(L, M)

R = "caresp_simulado_estimacion"
eventos, n = [], 0
def ev(tipo, **datos):
	global n
	eventos.append(f"event: {tipo}\ndata: " + json.dumps({"type": tipo, **datos, "sequence_number": n}, ensure_ascii=False))
	n += 1
resp = lambda estado: {"id": R, "object": "response", "status": estado, "output": []}
idx = 0
def item(it, salida=None):
	global idx
	ev("response.output_item.added", output_index=idx, item={**it, "status": "in_progress"})
	ev("response.output_item.done", output_index=idx, item={**it, **(salida or {}), "status": "completed"})
	idx += 1

ev("response.created", response=resp("in_progress"))
ev("response.in_progress", response=resp("in_progress"))
item({"type": "reasoning", "id": "rs_1", "summary": []})
for titulos in [["What are Azure App Service plans?", "Azure App Service plan overview", "Scale up an app in Azure App Service"], ["vCore purchasing model - Azure SQL Database", "General Purpose service tier - Azure SQL Database", "Resource limits for single databases using the vCore purchasing model"]]:
	# Como en el stream real: arguments vacío y la salida con las páginas encontradas
	item({"type": "mcp_call", "id": f"mcp_{idx}", "name": "microsoft_docs_search", "server_label": "microsoft_learn", "arguments": ""}, {"output": json.dumps({"results": [{"title": t, "content": "…"} for t in titulos]})})
busquedas = [
	({"servicio": "Azure App Service", "region": "swedencentral", "sku_contiene": "P1 v3", "producto_contiene": "Linux"}, 1),
	({"servicio": "SQL Database", "region": "swedencentral", "sku_contiene": "2 vCore", "producto_contiene": "General Purpose"}, 14),
	({"servicio": "Storage", "region": "swedencentral", "sku_contiene": "Hot LRS", "producto_contiene": "Block Blob v2"}, 12),
]
for args, total in busquedas:
	c = f"call_{idx}"
	item({"type": "function_call", "id": f"fc_{idx}", "call_id": c, "name": "buscar_precios", "arguments": json.dumps(args, ensure_ascii=False)})
	item({"type": "function_call_output", "id": f"fco_{idx}", "call_id": c, "output": json.dumps({"moneda": "EUR", "total_encontrados": total, "medidores": ["…"]})})
e = asyncio.run(estimacion())
c = f"call_{idx}"
item({"type": "function_call", "id": f"fc_{idx}", "call_id": c, "name": "calcular_estimacion", "arguments": json.dumps({"lineas": [{"meter_id": l["meter_id"], "cantidad": 1} for l in e["lineas"]]})})
item({"type": "function_call_output", "id": f"fco_{idx}", "call_id": c, "output": json.dumps(e, ensure_ascii=False)})

fila = lambda l: f"| {l['servicio']} — {l['descripcion']} | {l['sku']} | {l['cantidad']} | {l['precio_unitario'].replace('.', ',')} € / {l['unidad']} | {l['coste_mensual'].replace('.', ',')} € |"
texto = f"""## Arquitectura propuesta

- **Azure App Service, plan Premium v3 P1 v3 (Linux)**: aguanta de sobra 200 usuarios simultáneos para una web típica y permite escalar en horizontal si hace falta.
- **Azure SQL Database, General Purpose 2 vCore**: 50 GB caben con margen y el modelo vCore deja subir de tamaño sin migrar.
- **Azure Blob Storage, nivel Hot LRS**: los 500 GB de ficheros, con acceso frecuente y una sola región.

## Coste mensual

| Servicio | SKU | Cantidad | Precio unitario | Coste mensual |
|---|---|---|---|---|
""" + "\n".join(fila(l) for l in e["lineas"]) + f"""
| **Total** | | | | **{e['total_mensual'].replace('.', ',')} €** |

**Total mensual: {e['total_mensual'].replace('.', ',')} €**

## Supuestos

""" + "\n".join(f"- {s}" for s in e["supuestos"]) + """
- Una sola instancia de App Service y sin alta disponibilidad, como pediste.
- No incluye salida de datos a Internet, copias de seguridad adicionales ni operaciones de Blob Storage.

## Fuentes

- [¿Qué son los planes de App Service?](https://learn.microsoft.com/azure/app-service/overview-hosting-plans)
- [Niveles de servicio vCore de Azure SQL Database](https://learn.microsoft.com/azure/azure-sql/database/service-tiers-sql-database-vcore)
- [Niveles de acceso de Blob Storage](https://learn.microsoft.com/azure/storage/blobs/access-tiers-overview)
"""
ev("response.output_item.added", output_index=idx, item={"type": "message", "id": "msg_1", "role": "assistant", "content": [], "status": "in_progress"})
trozos = [texto[i:i + 40] for i in range(0, len(texto), 40)]
for t in trozos:
	ev("response.output_text.delta", item_id="msg_1", output_index=idx, content_index=0, delta=t)
ev("response.output_text.done", item_id="msg_1", output_index=idx, content_index=0, text=texto)
ev("response.completed", response=resp("completed"))
open(sys.argv[2], "w", encoding="utf-8").write("\n\n".join(eventos) + "\n\n")
print(texto)
