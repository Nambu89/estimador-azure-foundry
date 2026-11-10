"""Backend mínimo del frontend: sirve web/ y reenvía las preguntas al agente.

El navegador nunca ve un token. Este proceso es el único que habla con el agente:
- En local, contra `azd ai agent run` (http://localhost:8088/responses), sin autenticación.
- Contra el agente desplegado en Foundry, con un token de Entra ID de DefaultAzureCredential
  (tu `az login` en local, la identidad administrada en Azure). Esa identidad necesita el rol
  Foundry Agent Consumer.
- Con SIMULADO=1 reproduce dos respuestas grabadas del agente, para enseñarlo sin gastar nada.

Uso:
	python server.py                                   # local, contra localhost:8088
	SIMULADO=1 python server.py                        # respuesta simulada
	AGENTE_URL="https://…/agents/estimador-azure/endpoint/protocols/openai/responses?api-version=v1" python server.py
"""
import json
import os
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CARPETA = Path(__file__).parent
AGENTE_URL = os.environ.get("AGENTE_URL", "http://localhost:8088/responses")
SIMULADO = os.environ.get("SIMULADO") == "1"
PUERTO = int(os.environ.get("PORT", "8000"))
LIMITE_POR_HORA = int(os.environ.get("LIMITE_POR_HORA", "30"))  # Preguntas por IP y hora: cada una gasta tokens del modelo
# Tope para todos juntos: con muchas IPs el límite por IP no frena el gasto (unos 5-6 céntimos por pregunta)
LIMITE_POR_DIA = int(os.environ.get("LIMITE_POR_DIA", "100"))
MAX_CUERPO = 16_000
MAX_PREGUNTA = 4_000

_token = {"valor": None, "caduca": 0}
_peticiones: dict[str, deque] = defaultdict(deque)


def modo() -> str:
	if SIMULADO:
		return "simulado"
	return "foundry" if AGENTE_URL.startswith("https://") else "local"


def cabeceras_agente() -> dict:
	"""Cabeceras para el agente; con Foundry añade el token y lo reutiliza hasta 5 minutos antes de caducar."""
	cabeceras = {"Content-Type": "application/json"}
	if modo() != "foundry":
		return cabeceras
	if time.time() > _token["caduca"] - 300:
		from azure.identity import DefaultAzureCredential  # Solo hace falta en modo Foundry

		token = DefaultAzureCredential().get_token("https://ai.azure.com/.default")
		_token.update(valor=token.token, caduca=token.expires_on)
	cabeceras["Authorization"] = f"Bearer {_token['valor']}"
	return cabeceras


def dentro_del_limite(clave: str, limite: int, ventana: int) -> bool:
	"""Ventana deslizante en memoria (se reinicia con el proceso; en F1 hay un solo proceso)."""
	ahora = time.time()
	cola = _peticiones[clave]
	while cola and cola[0] < ahora - ventana:
		cola.popleft()
	if len(cola) >= limite:
		return False
	cola.append(ahora)
	return True


def validar(cuerpo: bytes) -> dict:
	"""Rehace la petición con solo los campos permitidos: desde fuera no se pueden cambiar modelo, herramientas ni instrucciones."""
	datos = json.loads(cuerpo)
	pregunta = datos.get("input")
	anterior = datos.get("previous_response_id")
	if not isinstance(pregunta, str) or not pregunta.strip() or len(pregunta) > MAX_PREGUNTA:
		raise ValueError(f"Escribe una pregunta de entre 1 y {MAX_PREGUNTA} caracteres.")
	if anterior is not None and (not isinstance(anterior, str) or len(anterior) > 200):
		raise ValueError("previous_response_id no es válido.")
	peticion = {"input": pregunta.strip(), "stream": True}
	if anterior:
		peticion["previous_response_id"] = anterior
	return peticion


class Manejador(SimpleHTTPRequestHandler):
	timeout = 30  # Corta las conexiones que no envían nada: en la F1 cada una ocupa un hilo

	def do_GET(self):
		if self.path == "/api/estado":
			return self.responder_json(200, {"modo": modo()})
		return super().do_GET()

	def do_POST(self):
		if self.path != "/api/responses":
			return self.responder_json(404, {"error": "No existe."})
		# Solo JSON: obliga al navegador a hacer preflight, que aquí no se acepta, así que otra web no puede
		# hacer que sus visitantes gasten tokens con un formulario o un fetch text/plain (CSRF)
		if not (self.headers.get("Content-Type") or "").startswith("application/json"):
			return self.responder_json(415, {"error": "Envía la pregunta como application/json."})
		try:
			longitud = int(self.headers.get("Content-Length") or 0)
		except ValueError:
			longitud = 0
		if longitud <= 0 or longitud > MAX_CUERPO:
			return self.responder_json(413, {"error": "La petición es demasiado grande."})
		try:
			peticion = validar(self.rfile.read(longitud))
		except (ValueError, json.JSONDecodeError) as e:
			return self.responder_json(400, {"error": str(e)})
		# En App Service la IP real llega en X-Client-IP, que pone la plataforma (X-Forwarded-For se puede falsear y lleva el puerto)
		ip = self.headers.get("X-Client-IP") or self.client_address[0]
		if not dentro_del_limite(ip, LIMITE_POR_HORA, 3600):
			return self.responder_json(429, {"error": "Has llegado al límite de preguntas por hora. Vuelve en un rato."})
		if not dentro_del_limite("*", LIMITE_POR_DIA, 86400):
			return self.responder_json(429, {"error": "La demo ha llegado a su límite de preguntas de hoy. Vuelve mañana."})

		if modo() == "simulado":
			return self.reproducir("simulado-estimacion.sse" if "previous_response_id" in peticion else "simulado-pregunta.sse")
		try:
			req = urllib.request.Request(AGENTE_URL, data=json.dumps(peticion).encode(), headers=cabeceras_agente())
			respuesta = urllib.request.urlopen(req, timeout=600)
		except urllib.error.HTTPError as e:
			self.log_error("El agente devolvió %s: %s", e.code, e.read()[:500])
			return self.responder_json(502, {"error": f"El agente ha devuelto un error {e.code}."})
		except Exception as e:  # Agente apagado, sin red o sin credenciales
			self.log_error("No se pudo llamar al agente: %r", e)
			return self.responder_json(502, {"error": "No se puede hablar con el agente. ¿Está levantado?"})
		self.empezar_stream()
		with respuesta:
			while trozo := respuesta.read1(8192):
				self.wfile.write(trozo)
				self.wfile.flush()

	def reproducir(self, nombre: str):
		"""Reproduce un stream grabado con pausas parecidas a las reales."""
		self.empezar_stream()
		for evento in (CARPETA / "simulado" / nombre).read_text(encoding="utf-8").split("\n\n"):
			if not evento.strip():
				continue
			time.sleep(0.02 if "output_text.delta" in evento else 0.5)
			self.wfile.write((evento + "\n\n").encode())
			self.wfile.flush()

	def empezar_stream(self):
		self.send_response(200)
		self.send_header("Content-Type", "text/event-stream; charset=utf-8")
		self.send_header("Cache-Control", "no-cache")
		self.end_headers()

	def responder_json(self, codigo: int, datos: dict):
		cuerpo = json.dumps(datos, ensure_ascii=False).encode()
		self.send_response(codigo)
		self.send_header("Content-Type", "application/json; charset=utf-8")
		self.send_header("Content-Length", str(len(cuerpo)))
		self.end_headers()
		self.wfile.write(cuerpo)

	def end_headers(self):
		self.send_header("X-Content-Type-Options", "nosniff")
		# Solo lo que usa la página: sus ficheros, marked y DOMPurify del CDN y las fuentes de Google.
		# img-src sin hosts externos: una imagen inyectada en la respuesta no puede sacar datos a otro servidor
		self.send_header("Content-Security-Policy", "; ".join([
			"default-src 'self'",
			"script-src 'self' https://cdn.jsdelivr.net",
			"style-src 'self' https://fonts.googleapis.com",
			"font-src https://fonts.gstatic.com",
			"img-src 'self' data:",
			"connect-src 'self'",
			"frame-ancestors 'none'",
			"base-uri 'none'",
			"form-action 'self'",
		]))
		if self.command == "GET":
			# Que el navegador revalide siempre (304 si no ha cambiado): así nadie se queda con el CSS o el JS de antes de un despliegue
			self.send_header("Cache-Control", "no-cache")
		super().end_headers()


if __name__ == "__main__":
	print(f"Estimador web en http://localhost:{PUERTO} · modo {modo()}")
	ThreadingHTTPServer(("0.0.0.0", PUERTO), partial(Manejador, directory=str(CARPETA / "publico"))).serve_forever()
