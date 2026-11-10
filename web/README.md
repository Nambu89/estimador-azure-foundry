# Frontend del estimador

Una página estática (`publico/`) y un backend mínimo (`server.py`) que hace de proxy hacia el agente.
El navegador nunca ve un token: solo `server.py` habla con el agente.

```
navegador ──/api/responses──▶ server.py ──▶ agente (local en :8088 o desplegado en Foundry)
```

## Arrancarlo

Sin agente, con dos respuestas grabadas (la pregunta de aclaración real y una estimación con precios reales de la api):

```bash
SIMULADO=1 python web/server.py
```

Contra el agente en local (en otra terminal, `azd ai agent run`):

```bash
python web/server.py
```

Contra el agente desplegado, con tu `az login` (necesitas el rol Foundry Agent Consumer y `pip install azure-identity`):

```bash
AGENTE_URL="https://<cuenta>.services.ai.azure.com/api/projects/<proyecto>/agents/estimador-azure/endpoint/protocols/openai/responses?api-version=v1" python web/server.py
```

Abre http://localhost:8000.

## Desplegarlo en Azure

App Service Linux F1 (0 €/mes) en `rg-estimador`, con identidad administrada y el rol **Foundry Agent Consumer**
sobre el proyecto de Foundry: la web se autentica contra el agente sin ningún secreto. Todo lleva la etiqueta
`proyecto=estimador-azure-foundry`. El script toma el tenant, la suscripción y la cuenta de Foundry del entorno
de azd del agente (`azd env get-value`), así que va después de `azd provision`.

```bash
web/infra/desplegar.sh                # infraestructura (infra/main.json) y código
SOLO_CODIGO=1 web/infra/desplegar.sh  # solo el código
```

El script usa el token de `azd auth`, no el `az` CLI. Para pasar a B1 (11,83 €/mes, sin cuotas de CPU), cambia el
parámetro `sku` de la plantilla. Va fuera de `azure.yaml` a propósito: así `azd provision` no le quita las etiquetas.

## Variables

| Variable | Por defecto | Para qué |
|---|---|---|
| `AGENTE_URL` | `http://localhost:8088/responses` | Dónde está el agente. Si empieza por `https://` pide un token de Entra ID con `DefaultAzureCredential`. |
| `SIMULADO` | — | `1` para usar las respuestas grabadas de `simulado/`. |
| `PORT` | `8000` | Puerto. |
| `LIMITE_POR_HORA` | `30` | Preguntas por IP y hora. Cada pregunta gasta tokens del modelo. |
| `LIMITE_POR_DIA` | `100` | Preguntas al día entre todos los usuarios: el tope de gasto aunque lleguen desde muchas IP (unos 5-6 céntimos cada una). |

## Pruebas

```bash
node web/formato.test.mjs
```

`simulado/generar.py` rehace la estimación simulada con precios del día: `python web/simulado/generar.py src/estimador-azure web/simulado/simulado-estimacion.sse` (con el entorno del agente).
