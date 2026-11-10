# estimador-azure

Un agente que estima cuánto cuesta al mes una carga de trabajo en Azure. Le describes lo que necesitas
(«Web para 2.000 usuarios en Sweden Central con SQL y 500 GB de ficheros») y te devuelve:

1. Una arquitectura sencilla, con el porqué de cada servicio.
2. Una tabla con servicio, SKU y medidor, cantidad, precio unitario y coste mensual.
3. El total mensual en EUR.
4. Los supuestos.
5. Las fuentes de Microsoft Learn que justifican cada servicio.

Si le falta un dato importante, pregunta una vez; si no, decide un valor razonable y lo dice.

Es un **Hosted Agent de Microsoft Foundry** hecho con **Agent Framework** en Python 3.13 y protocolo Responses.
Tutorial del Calendario de Adviento Tecnológico 2026 (calendario Agentes e IA).

## Cómo funciona

```
pregunta ──▶ modelo ──▶ microsoft_docs_search (MCP de Microsoft Learn): qué servicios y niveles encajan
                  └──▶ buscar_precios        (api Azure Retail Prices): medidores con su precio
                  └──▶ calcular_estimacion   (código, Decimal): importes y total
```

La regla de oro: **el modelo decide qué servicios hacen falta, pero los precios y las cuentas los hace el código.**

- [src/estimador-azure/precios.py](src/estimador-azure/precios.py) consulta la api pública
  [Azure Retail Prices](https://learn.microsoft.com/rest/api/cost-management/retail-prices/azure-retail-prices)
  en EUR, sigue la paginación, agrupa los tramos por volumen y calcula con `Decimal` y 730 horas al mes.
- [src/estimador-azure/main.py](src/estimador-azure/main.py) define el agente y sus dos herramientas.
  `calcular_estimacion` solo recibe `meter_id` y `cantidad`: el precio lo busca el código, así que el modelo
  no puede inventárselo.

## Por qué un Hosted Agent y no un Prompt Agent

Un [Prompt Agent](https://learn.microsoft.com/azure/foundry/agents/overview#agent-types) se define con
instrucciones, modelo y herramientas, sin código propio que mantener. Aquí no basta:

- **El cálculo tiene que ser código.** Paginación de la api, tramos por volumen, unidades por hora, día o año,
  precios que la api redondea en EUR y aritmética en `Decimal`. Pedírselo al modelo daría importes distintos
  en cada respuesta. Con un Prompt Agent habría que publicar ese código aparte (una Function o una API con su
  herramienta OpenAPI) y mantener dos piezas.
- **Un Hosted Agent es tu código en un contenedor gestionado.** Herramientas, instrucciones y modelo viajan
  juntos, con un endpoint propio, una identidad de Entra para el agente y escalado a cero cuando no se usa.
  Ver [What are hosted agents?](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents).

## Requisitos

- Una suscripción de Azure con cuota para el modelo en la región (ver [Modelo](#modelo)).
- Python 3.13 y [uv](https://docs.astral.sh/uv/).
- [Azure Developer CLI](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd) 1.37 o
  posterior y la extensión `azure.ai.agents`, que instala también `azure.ai.projects`.
- Rol **Owner** (o Contributor más Role Based Access Control Administrator) para crear los recursos y asignar
  roles.

## Despliegue

Estos son los pasos que han funcionado, en este orden.

### 1. Herramientas y sesión

```bash
brew install azure/azd/azd
```

```bash
azd ext install azure.ai.agents
```

```bash
azd auth login --tenant-id <tu-tenant-id>
```

Si el navegador no termina el inicio de sesión, añade `--use-device-code`.

### 2. Entorno y recursos

```bash
azd env new estimador --subscription <tu-subscription-id>
```

```bash
azd env set AZURE_LOCATION swedencentral
```

Antes de crear nada, mira qué se va a crear (no cuesta nada y detecta la falta de cuota):

```bash
azd provision --preview
```

```bash
azd provision
```

Crea cuatro recursos: el grupo `rg-estimador`, una cuenta de Foundry (Azure AI Services), el despliegue del
modelo y el proyecto. No hace falta `azd ai agent init`: el `azure.yaml` ya está completo. No se crea
Container Registry (el despliegue es en modo código) ni Application Insights.

### 3. Etiqueta para Cost Management

Todo lleva `proyecto=estimador-azure-foundry` para filtrar el consumo en Cost Management:

```bash
az tag update --resource-id <id-del-recurso> --operation Merge --tags proyecto=estimador-azure-foundry
```

Hazlo en el grupo de recursos, la cuenta, el despliegue del modelo y el proyecto, **antes de usar el modelo**:
las etiquetas solo cuentan para el consumo desde que se ponen. **Cada `azd provision` borra la etiqueta** del
grupo, la cuenta y el despliegue, así que hay que volver a ponerla después de cada provision.

### 4. Roles

Ser Owner no permite llamar al modelo: Owner y Contributor solo dan permisos de plano de control. Hace falta
**Foundry User** (antes Azure AI User, id `53ca6127-db72-4b80-b1b0-d745d6d5456d`), y en nuestro despliegue
Foundry no lo asignó solo:

```bash
az role assignment create --assignee <tu-object-id> --role 53ca6127-db72-4b80-b1b0-d745d6d5456d --scope <id-del-proyecto>
```

```bash
az role assignment create --assignee <principal-id-de-la-identidad-del-proyecto> --role 53ca6127-db72-4b80-b1b0-d745d6d5456d --scope <id-de-la-cuenta>
```

El primero es para ti (probar en local y desplegar). El segundo, para que el agente desplegado llame al modelo
a través del proyecto. Tardan unos minutos en propagarse. Detalle en
[Hosted agent permissions reference](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agent-permissions).

### 5. Probar en local

```bash
AZURE_TOKEN_CREDENTIALS=AzureDeveloperCliCredential azd ai agent run
```

`AZURE_TOKEN_CREDENTIALS` obliga a usar la sesión de azd. Sin ella, `DefaultAzureCredential` prueba antes la de
`az` y, si la tienes abierta en otro tenant, el modelo responde 401 o 403. En otra terminal:

```bash
azd ai agent invoke --local "Web para 2.000 usuarios en Sweden Central con SQL y 500 GB de ficheros"
```

### 6. Desplegar e invocar

```bash
azd deploy
```

```bash
azd ai agent invoke "Web para 2.000 usuarios en Sweden Central con SQL y 500 GB de ficheros"
```

El primer mensaje tras un rato parado tarda unos 13 s más (arranque en frío). Una estimación completa tarda
entre 45 y 100 s.

## Modelo

El `azure.yaml` usa **gpt-5-mini** (2025-08-07, GlobalStandard, capacidad 500) **de forma temporal**. El modelo
previsto es **gpt-6-luna** (2026-09-22, DataZoneStandard), pero la suscripción de Visual Studio del programa MVP
empieza en el nivel de cuota «Free Tier», con 0 TPM para toda la familia gpt-6. La cuota se pide con el
[formulario de Microsoft](https://aka.ms/oai/stuquotarequest).

Para cambiar de modelo o de capacidad, edita el bloque `deployments` y `AZURE_AI_MODEL_DEPLOYMENT_NAME` en
`azure.yaml` y luego:

```bash
azd env unset AI_PROJECT_DEPLOYMENTS --force
```

```bash
azd provision
```

Sin el `unset`, azd usa la copia de los despliegues que guarda en `.azure/<entorno>/.env` e ignora el cambio.

### Qué tal elige servicios

Lo probamos con cuatro cargas (web con SQL, API en contenedores, VM en horario laboral y Functions con Blob) y
cada respuesta la revisaron revisores independientes: arquitectura, precios y fuentes. gpt-5-mini acierta con
los servicios principales, pero falla en los detalles que distinguen a un arquitecto: elige niveles antiguos
(S2 en lugar de P0v3 o P1v3), añade recargos que no tocan («Disk Mount» en un disco no compartido) aunque las
instrucciones lo prohíben, y a veces no encuentra un servicio en la api y lo cambia por otro. Cuando haya cuota,
la comparación pendiente es gpt-6-luna frente a gpt-6-sol con las mismas cuatro cargas.

## Coste

Precios de lista en EUR, Sweden Central, consultados con la propia api el 9 de noviembre de 2026:

| Concepto | Precio |
|---|---|
| Cuenta y proyecto de Foundry | 0 € fijo, se paga por uso |
| gpt-5-mini, tokens de entrada (Global) | 0,2244 € / 1M |
| gpt-5-mini, tokens de salida (Global) | 1,7954 € / 1M |
| Hosted Agent, vCPU | 0,0982 € / vCPU·hora |
| Hosted Agent, memoria | 0,0117 € / GiB·hora |

- El contenedor (0,5 vCPU y 1 GiB) cuesta 0,061 €/hora **solo mientras hay una sesión activa**: se apaga tras
  15 minutos sin peticiones.
- **Medido de verdad:** las pruebas de este tutorial (182 llamadas al modelo, unas quince estimaciones) costaron
  0,85 € en tokens. Una estimación sale por unos **5 o 6 céntimos**.
- La capacidad del despliegue no cuesta nada: se paga por token, no por capacidad.

## Limpieza

```bash
azd down
```

Borra el grupo `rg-estimador` y **todo lo que tenga dentro**, frontend incluido si lo desplegaste ahí. azd
enseña la lista y pide confirmación. La cuenta de Foundry queda en borrado temporal; para liberarla del todo:

```bash
azd down --purge
```

## Tests

```bash
cd src/estimador-azure && uv sync && cd ../.. && src/estimador-azure/.venv/bin/python -m pytest -q
```

No llaman a Azure: usan respuestas guardadas de la api, varias de ellas reales (`tests/datos`).

## Seguridad

- **Sin secretos.** Todo se autentica con identidades de Entra: la web llama al agente con su identidad
  administrada (rol Foundry Agent Consumer), el agente llama al modelo con la identidad del proyecto y la cuenta
  de Foundry tiene `disableLocalAuth=true`, así que sus claves de API no sirven. La api de precios y el MCP de Learn
  son públicos. Por eso no hay Key Vault: estaría vacío. Si algún día entra una clave de terceros, su sitio es un
  Key Vault leído con la identidad administrada, nunca un fichero ni un ajuste en claro.
- **Gasto acotado.** La web es pública: tiene un límite por IP y hora y un tope diario para todos
  (`LIMITE_POR_DIA`), solo acepta `application/json` (otra web no puede hacer que sus visitantes gasten tokens) y
  el agente limita las vueltas del bucle de herramientas y los tokens de salida de cada llamada.
- **Navegador.** El markdown del agente pasa por DOMPurify, sin imágenes, y la página lleva una
  Content-Security-Policy que solo permite sus propios ficheros, el CDN de las dos librerías y las fuentes.

## Lo que aprendimos por el camino

| Síntoma | Causa | Arreglo |
|---|---|---|
| 429 con muy pocos tokens | Sin límite de salida, Azure reserva el máximo del modelo (~1M tokens) en cada petición contra los TPM del despliegue. Se ve en la métrica `Ratelimit` de Azure Monitor. | `max_tokens` en las opciones del agente |
| 429 en el bucle de herramientas | Cada vuelta reenvía todo el historial; las páginas enteras de Learn lo agotan | Solo `microsoft_docs_search`, respuestas compactas y más capacidad |
| «Function failed» en una herramienta | Agent Framework entrega las listas de objetos como `dict` | Validar con `model_validate` al entrar |
| Precios a 0 en EUR | La api da 4 decimales fuera de USD; 0,000016 $ sale 0,0 € | Recalcular desde USD los precios por debajo de 0,01 |
| Precio de SQL de otro SKU | El `meterId` se repite entre SKU con precios distintos | Identificar el medidor por `meterId:skuId` |
| SQL Standard 30 veces más barato | Se cobra por día (`1/Day`) | Convertir hora, día y año a horas del mes |
| Cantidades mal pasadas | No quedaba claro si iban unidades o bloques | La cantidad va en la unidad del precio y la tabla enseña a qué equivale |

## Frontend

En [web/](web/) hay una web que habla con el agente desplegado mediante identidad administrada, sin secretos.
Cómo arrancarla y desplegarla: [web/README.md](web/README.md).
