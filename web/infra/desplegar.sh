#!/usr/bin/env bash
# Despliega el frontend del estimador en App Service (plantilla main.json) y sube el código.
# Usa el token de azd, así que no depende del tenant en el que esté el az CLI.
# Uso: web/infra/desplegar.sh            (crea o actualiza la infraestructura y sube el código)
#      SOLO_CODIGO=1 web/infra/desplegar.sh   (solo sube el código)
set -euo pipefail

# Tenant, suscripción y cuenta de Foundry salen del entorno de azd del agente (azd env new + azd provision)
TENANT="${TENANT:-$(azd env get-value AZURE_TENANT_ID)}"
SUSCRIPCION="${SUSCRIPCION:-$(azd env get-value AZURE_SUBSCRIPTION_ID)}"
CUENTA="${CUENTA:-$(azd env get-value AZURE_AI_ACCOUNT_NAME)}"
GRUPO="${GRUPO:-rg-estimador}"
WEB_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ARM="https://management.azure.com/subscriptions/$SUSCRIPCION"

token() {
	azd auth token --tenant-id "$TENANT" --scope "https://management.azure.com//.default" -o json | python3 -c "import sys, json; print(json.load(sys.stdin)['token'])"
}
TOKEN="$(token)"
# El token va por un descriptor (printf es interno de bash): así no sale en la línea de comandos que enseña ps
cabecera() { printf 'Authorization: Bearer %s\n' "$TOKEN"; }
api() { curl -sS --fail-with-body -H @<(cabecera) -H "Content-Type: application/json" "$@"; }
campo() { python3 -c "import sys, json; d = json.load(sys.stdin); print(eval('d' + sys.argv[1]))" "$1"; }

if [[ -z "${SOLO_CODIGO:-}" ]]; then
	echo "· Registrando el proveedor Microsoft.Web (gratis, una vez por suscripción)"
	api -X POST -d "" "$ARM/providers/Microsoft.Web/register?api-version=2021-04-01" > /dev/null
	until [[ "$(api "$ARM/providers/Microsoft.Web?api-version=2021-04-01" | campo "['registrationState']")" == "Registered" ]]; do sleep 10; done

	echo "· Desplegando la plantilla en $GRUPO"
	DESPLIEGUE="estimador-web-$(date +%Y%m%d%H%M%S)"
	python3 -c "import json, sys; print(json.dumps({'properties': {'mode': 'Incremental', 'template': json.load(open(sys.argv[1])), 'parameters': {'cuentaFoundry': {'value': sys.argv[2]}}}}))" "$WEB_DIR/infra/main.json" "$CUENTA" \
		| api -X PUT "$ARM/resourceGroups/$GRUPO/providers/Microsoft.Resources/deployments/$DESPLIEGUE?api-version=2021-04-01" -d @- > /dev/null
	while true; do
		ESTADO="$(api "$ARM/resourceGroups/$GRUPO/providers/Microsoft.Resources/deployments/$DESPLIEGUE?api-version=2021-04-01" | campo "['properties']['provisioningState']")"
		[[ "$ESTADO" == "Succeeded" ]] && break
		if [[ "$ESTADO" == "Failed" || "$ESTADO" == "Canceled" ]]; then
			api "$ARM/resourceGroups/$GRUPO/providers/Microsoft.Resources/deployments/$DESPLIEGUE/operations?api-version=2021-04-01" \
				| python3 -c "import sys, json; [print(o['properties'].get('targetResource', {}).get('resourceName'), o['properties'].get('statusMessage')) for o in json.load(sys.stdin)['value'] if o['properties']['provisioningState'] == 'Failed']"
			exit 1
		fi
		sleep 10
	done
fi

WEB="$(api "$ARM/resourceGroups/$GRUPO/providers/Microsoft.Web/sites?api-version=2024-04-01" | python3 -c "import sys, json; print([s['name'] for s in json.load(sys.stdin)['value'] if s['name'].startswith('app-estimador-web-')][0])")"

echo "· Subiendo el código a $WEB"
PAQUETE="$(mktemp -d)/web.zip"
(cd "$WEB_DIR" && zip -qr "$PAQUETE" server.py requirements.txt publico simulado/*.sse)
# zipdeploy acepta el token de Entra ID: la plantilla desactiva usuario y contraseña de publicación
curl -sS --fail-with-body -X POST -H @<(cabecera) --data-binary @"$PAQUETE" \
	"https://$WEB.scm.azurewebsites.net/api/zipdeploy?isAsync=false" -o /dev/null -w "  zipdeploy: HTTP %{http_code}\n"

echo "Listo: https://$WEB.azurewebsites.net"
