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
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import OrderedDict, defaultdict, deque
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CARPETA = Path(__file__).parent
AGENTE_URL = os.environ.get("AGENTE_URL", "http://localhost:8088/responses")
SIMULADO = os.environ.get("SIMULADO") == "1"
PUERTO = int(os.environ.get("PORT", "8000"))
LIMITE_POR_HORA = int(os.environ.get("LIMITE_POR_HORA", "10"))  # Preguntas por IP y hora
LIMITE_IP_DIA = int(os.environ.get("LIMITE_IP_DIA", "20"))  # Por IP y día: por debajo del tope global, para que una IP no lo agote sola
LIMITE_POR_DIA = int(os.environ.get("LIMITE_POR_DIA", "100"))  # Preguntas al día entre todos
# Tokens del modelo al día entre todos. Es el tope de gasto de verdad: una sola pregunta llegó a 1M de tokens
LIMITE_TOKENS_DIA = int(os.environ.get("LIMITE_TOKENS_DIA", "15000000"))
# Tokens que se apartan al admitir cada pregunta y se cambian por el uso real cuando llega. Si el cliente se va
# antes del final, se queda la reserva: así las preguntas en curso y las cortadas también cuentan
RESERVA_TOKENS = int(os.environ.get("RESERVA_TOKENS", "800000"))
MAX_TURNOS = int(os.environ.get("MAX_TURNOS", "8"))  # Preguntas encadenadas por conversación: el historial se reenvía entero
MAX_CUERPO = 16_000
MAX_PREGUNTA = 4_000
MAX_CONEXIONES = 32  # Conexiones atendidas a la vez; el resto se cierra al llegar
MAX_CLAVES = 50_000  # Entradas de los contadores por IP
MAX_CONVERSACIONES = 10_000
PLAZO_PETICION = 15  # Segundos para recibir cabeceras y cuerpo: una conexión que gotea bytes no retiene su hueco
# En App Service, /home se conserva entre reinicios: así reciclar el proceso no pone a cero el tope diario
FICHERO_LIMITES = os.environ.get("FICHERO_LIMITES") or ("/home/estimador-limites.json" if os.environ.get("WEBSITE_SITE_NAME") else None)

_token = {"valor": None, "caduca": 0}
_cerrojo = threading.Lock()  # Protege los contadores, los tokens y las conversaciones (un hilo por conexión)
_peticiones: dict[str, deque] = defaultdict(deque)
_tokens: deque = deque()  # [instante, tokens] de las últimas 24 h (lista: la reserva se corrige con el uso real)
_conversaciones: OrderedDict[str, tuple[str, int]] = OrderedDict()  # response_id -> (huella de la clave, turnos)


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


def ip_cliente(cabeceras, ip_socket: str) -> str:
	"""IP del visitante: la última entrada de X-Forwarded-For, que añade el frontal de la plataforma.

	Las entradas anteriores (y X-Client-IP) las puede escribir el propio cliente, así que no sirven para limitar.
	"""
	valores = cabeceras.get_all("X-Forwarded-For") or []
	ultima = valores[-1].split(",")[-1].strip() if valores else ""
	if ultima.startswith("["):  # [IPv6]:puerto
		ultima = ultima[1:].split("]")[0]
	elif ultima.count(":") == 1:  # IPv4:puerto
		ultima = ultima.split(":")[0]
	try:
		ip = ipaddress.ip_address(ultima) if len(ultima) <= 45 else None
	except ValueError:
		ip = None
	if ip is None or getattr(ip, "scope_id", None):
		return ip_socket
	ip = getattr(ip, "ipv4_mapped", None) or ip  # ::ffff:a.b.c.d es una IPv4: no meterlas todas en el mismo /64
	# Quien tiene IPv6 suele controlar un /64 entero: contarlo como una sola IP
	return str(ipaddress.ip_network(f"{ip}/64", strict=False)) if ip.version == 6 else str(ip)


def _guardar():
	"""Guarda los contadores diarios y los tokens (llamar con el cerrojo cogido). Si falla, avisa y sigue en memoria."""
	if not FICHERO_LIMITES:
		return
	diarios = {k: list(c) for k, c in _peticiones.items() if k == "*" or k.endswith("|d")}
	tmp = f"{FICHERO_LIMITES}.{secrets.token_hex(8)}.tmp"
	try:
		with open(tmp, "w", encoding="utf-8") as f:
			json.dump({"diarios": diarios, "tokens": list(_tokens)}, f)
		os.replace(tmp, FICHERO_LIMITES)
	except OSError as e:
		print(f"AVISO: no se pudieron guardar los límites en {FICHERO_LIMITES}: {e!r}", file=sys.stderr)
		try:
			os.unlink(tmp)  # Con el nombre único, cada fallo dejaría un temporal huérfano
		except OSError:
			pass


def _cargar():
	if not FICHERO_LIMITES or not os.path.exists(FICHERO_LIMITES):
		return
	ahora = time.time()
	try:
		datos = json.loads(Path(FICHERO_LIMITES).read_text(encoding="utf-8"))
		with _cerrojo:
			for k, marcas in datos["diarios"].items():
				_peticiones[k].extend(t for t in marcas if t > ahora - 86400)
			_tokens.extend([x[0], x[1]] for x in datos["tokens"] if x[0] > ahora - 86400)
	except (OSError, ValueError, KeyError, TypeError) as e:
		print(f"AVISO: límites no recuperados de {FICHERO_LIMITES}, se empieza de cero: {e!r}", file=sys.stderr)


def tokens_de_hoy(ahora: float) -> int:
	"""Tokens gastados en las últimas 24 h (llamar con el cerrojo cogido)."""
	while _tokens and _tokens[0][0] < ahora - 86400:
		_tokens.popleft()
	return sum(x[1] for x in _tokens)  # [instante, tokens] o [instante, tokens, "real"]


def admitir(ip: str) -> tuple[str | None, list | None]:
	"""Comprueba y apunta a la vez el presupuesto de tokens y los tres límites de preguntas.

	Devuelve (motivo del rechazo, None) o (None, reserva). La reserva es la entrada de tokens que se corrige con el
	uso real. Si un límite rechaza, no se gasta ningún otro.
	"""
	reglas = [
		("*", LIMITE_POR_DIA, 86400, "La demo ha llegado a su límite de preguntas de hoy. Vuelve más tarde."),
		(f"{ip}|h", LIMITE_POR_HORA, 3600, "Has llegado al límite de preguntas por hora. Vuelve en un rato."),
		(f"{ip}|d", LIMITE_IP_DIA, 86400, "Has llegado a tu límite de preguntas de hoy. Vuelve más tarde."),
	]
	with _cerrojo:
		ahora = time.time()  # Dentro del cerrojo: las marcas entran en orden y la caducidad por la cabeza funciona
		if tokens_de_hoy(ahora) + RESERVA_TOKENS > LIMITE_TOKENS_DIA:
			return "La demo ha gastado su presupuesto de hoy. Vuelve más tarde.", None
		if len(_peticiones) > MAX_CLAVES:
			for k in [k for k, c in _peticiones.items() if k != "*" and (not c or c[-1] < ahora - (3600 if k.endswith("|h") else 86400))]:
				del _peticiones[k]
			if len(_peticiones) > MAX_CLAVES:
				return "Hay demasiado tráfico. Vuelve en un rato.", None
		for clave, limite, ventana, motivo in reglas:
			cola = _peticiones[clave]
			while cola and cola[0] < ahora - ventana:
				cola.popleft()
			if len(cola) >= limite:
				return motivo, None
		for clave, *_ in reglas:
			_peticiones[clave].append(ahora)
		reserva = [ahora, RESERVA_TOKENS]
		_tokens.append(reserva)
		_guardar()
	return None, reserva


def devolver(ip: str, reserva: list, marcas: bool = True):
	"""Si el agente no llega a responder, la pregunta no cuenta para el día ni para el presupuesto.

	La marca por hora se queda: así, mientras el agente falle, nadie puede repetir sin tope. Con marcas=False
	solo se quita la reserva de tokens (el modo simulado no gasta, pero sus preguntas sí cuentan).
	"""
	with _cerrojo:
		if marcas:
			for clave in ("*", f"{ip}|d"):
				if reserva[0] in _peticiones[clave]:
					_peticiones[clave].remove(reserva[0])
		for i, x in enumerate(_tokens):
			if x is reserva:  # Por identidad: otra reserva del mismo instante sería igual en valor
				del _tokens[i]
				break
		_guardar()


def apuntar_tokens(reserva: list, n: int):
	"""Cambia la reserva por el uso real (la primera vez que llega)."""
	with _cerrojo:
		if len(reserva) == 2:
			reserva[1] = n
			reserva.append("real")
			_guardar()


def huella(clave: str) -> str:
	return hashlib.sha256(clave.encode()).hexdigest()


def tomar_conversacion(anterior: str, clave: str) -> int | None:
	"""Turnos ya hechos, o None si esa clave no es la que la empezó (o ya no se recuerda).

	El id no se consume: reenviarlo solo abre otra rama con los mismos turnos, que no pasa de MAX_TURNOS y paga
	los mismos límites. Así un stream cortado se puede reintentar sin perder la conversación.
	"""
	with _cerrojo:
		dato = _conversaciones.get(anterior)
	if not dato or not hmac.compare_digest(dato[0], huella(clave)):
		return None
	return dato[1]


def registrar_respuesta(response_id: str, h: str, turnos: int):
	with _cerrojo:
		_conversaciones[response_id] = (h, turnos)
		_conversaciones.move_to_end(response_id)
		while len(_conversaciones) > MAX_CONVERSACIONES:
			_conversaciones.popitem(last=False)


def mirar_evento(linea: bytes, h: str, turnos: int, reserva: list | None):
	"""Del stream SSE solo interesan el id de la respuesta (para atarlo a la clave) y los tokens gastados."""
	if not linea.startswith(b"data:") or not any(t in linea for t in (b'"response.created"', b'"response.completed"', b'"response.failed"', b'"response.incomplete"')):
		return
	try:
		evento = json.loads(linea[5:])
	except ValueError:
		return
	if not isinstance(evento, dict):
		return
	respuesta = evento.get("response")
	if not isinstance(respuesta, dict):
		return
	if evento.get("type") in ("response.created", "response.completed") and isinstance(respuesta.get("id"), str):
		registrar_respuesta(respuesta["id"], h, turnos)
	uso = respuesta.get("usage")
	if evento.get("type") != "response.created" and isinstance(uso, dict) and reserva is not None:
		n = uso.get("total_tokens")
		if type(n) is not int:  # type y no isinstance: True no es un recuento
			n = sum(v for v in (uso.get("input_tokens"), uso.get("output_tokens")) if type(v) is int)
		if n > 0:
			apuntar_tokens(reserva, n)


def validar(cuerpo: bytes) -> tuple[dict, str]:
	"""Rehace la petición con solo los campos permitidos: desde fuera no se pueden cambiar modelo, herramientas ni instrucciones."""
	try:
		datos = json.loads(cuerpo)
	except RecursionError:  # Unas 10.000 capas caben en MAX_CUERPO y no es un ValueError
		raise ValueError("El JSON está demasiado anidado.")
	if not isinstance(datos, dict):
		raise ValueError("El cuerpo debe ser un objeto JSON.")
	pregunta = datos.get("input")
	anterior = datos.get("previous_response_id")
	clave = datos.get("clave")
	if not isinstance(pregunta, str) or not pregunta.strip() or len(pregunta) > MAX_PREGUNTA:
		raise ValueError(f"Escribe una pregunta de entre 1 y {MAX_PREGUNTA} caracteres.")
	if anterior is not None and (not isinstance(anterior, str) or len(anterior) > 200):
		raise ValueError("previous_response_id no es válido.")
	# La clave la genera el navegador al empezar cada conversación y solo la conoce él (hexadecimal)
	if not isinstance(clave, str) or not re.fullmatch(r"[0-9a-f]{32,128}", clave):
		raise ValueError("Falta la clave de la conversación.")
	peticion = {"input": pregunta.strip(), "stream": True}
	if anterior:
		peticion["previous_response_id"] = anterior
	return peticion, clave


class Manejador(SimpleHTTPRequestHandler):
	timeout = 30  # Corta las conexiones que no envían nada

	def setup(self):
		super().setup()
		# Plazo total para recibir la petición: el timeout del socket se rearma con cada byte y no basta
		self._cortado = False
		self._plazo = threading.Timer(PLAZO_PETICION, self._cortar)
		self._plazo.daemon = True
		self._plazo.start()

	def _cortar(self):
		self._cortado = True
		try:
			self.connection.shutdown(socket.SHUT_RDWR)
		except OSError:
			pass

	def finish(self):
		self._plazo.cancel()
		super().finish()

	def do_GET(self):
		self._plazo.cancel()
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
			cuerpo = self.rfile.read(longitud)
		except OSError:
			return
		self._plazo.cancel()
		if len(cuerpo) < longitud or self._cortado:
			return  # Conexión cortada por el plazo de lectura (o el cliente se fue): no hay nadie a quien responder
		try:
			peticion, clave = validar(cuerpo)
		except ValueError as e:  # json.JSONDecodeError es un ValueError
			return self.responder_json(400, {"error": str(e)})
		h, turnos, anterior = huella(clave), 0, peticion.get("previous_response_id")
		if anterior:
			# Todas las peticiones llegan al agente con la misma identidad: solo quien tiene la clave puede seguir la conversación
			turnos = tomar_conversacion(anterior, clave)
			if turnos is None:
				return self.responder_json(403, {"error": "Esa conversación no es tuya o ha caducado. Empieza una nueva."})
			if turnos >= MAX_TURNOS:
				return self.responder_json(429, {"error": "Esta conversación ha llegado a su límite de preguntas. Empieza una nueva."})
		ip = ip_cliente(self.headers, self.client_address[0])
		motivo, reserva = admitir(ip)
		if motivo:
			return self.responder_json(429, {"error": motivo})

		if modo() == "simulado":
			devolver(ip, reserva, marcas=False)  # No gasta tokens: que la demo grabada no consuma presupuesto
			return self.reproducir("simulado-estimacion.sse" if anterior else "simulado-pregunta.sse", h, turnos + 1, None)
		try:
			req = urllib.request.Request(AGENTE_URL, data=json.dumps(peticion).encode(), headers=cabeceras_agente())
			respuesta = urllib.request.urlopen(req, timeout=600)
		except Exception as e:  # Error del agente, agente apagado, sin red o sin credenciales: la pregunta no cuenta
			devolver(ip, reserva)
			if isinstance(e, urllib.error.HTTPError):
				self.log_error("El agente devolvió %s: %s", e.code, e.read()[:500])
				return self.responder_json(502, {"error": f"El agente ha devuelto un error {e.code}."})
			self.log_error("No se pudo llamar al agente: %r", e)
			return self.responder_json(502, {"error": "No se puede hablar con el agente. ¿Está levantado?"})
		with respuesta:
			self.copiar(iter(lambda: respuesta.read1(8192), b""), h, turnos + 1, reserva)

	def copiar(self, trozos, h: str, turnos: int, reserva: list | None):
		"""Pasa el stream al navegador tal cual y, de camino, apunta el id de la respuesta y los tokens.

		Si el navegador se va, se deja de reenviar pero se sigue leyendo hasta el final para apuntar el uso real
		(si no llega, se queda la reserva).
		"""
		try:
			self.empezar_stream()
			conectado = True
		except OSError:
			conectado = False
		resto = b""
		for trozo in trozos:
			if conectado:
				try:
					self.wfile.write(trozo)
					self.wfile.flush()
				except OSError:
					conectado = False
			*lineas, resto = (resto + trozo).split(b"\n")
			if len(resto) > 4_000_000:
				resto = b""  # Una línea sin fin no hace crecer la memoria (se queda la reserva)
			for linea in lineas:
				mirar_evento(linea, h, turnos, reserva)
		mirar_evento(resto, h, turnos, reserva)

	def reproducir(self, nombre: str, h: str, turnos: int, reserva: list | None):
		"""Reproduce un stream grabado con pausas parecidas a las reales, con ids únicos por petición."""
		unico = f"caresp_simulado_{secrets.token_hex(8)}_"
		def eventos():
			for evento in (CARPETA / "simulado" / nombre).read_text(encoding="utf-8").split("\n\n"):
				if evento.strip():
					time.sleep(0.02 if "output_text.delta" in evento else 0.5)
					yield (evento.replace("caresp_simulado_", unico) + "\n\n").encode()
		self.copiar(eventos(), h, turnos, reserva)

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


class Servidor(ThreadingHTTPServer):
	"""Como mucho MAX_CONEXIONES conexiones a la vez: las que sobran se cierran al llegar, sin crear hilo."""

	def __init__(self, *args, **kwargs):
		super().__init__(*args, **kwargs)
		self.huecos = threading.BoundedSemaphore(MAX_CONEXIONES)

	def process_request(self, request, client_address):
		if not self.huecos.acquire(blocking=False):
			try:
				request.setblocking(False)
				request.sendall(b"HTTP/1.0 503 Service Unavailable\r\nRetry-After: 30\r\nContent-Length: 0\r\n\r\n")
			except OSError:
				pass
			self.shutdown_request(request)
			return
		try:
			super().process_request(request, client_address)
		except Exception:
			self.huecos.release()
			raise

	def process_request_thread(self, request, client_address):
		try:
			super().process_request_thread(request, client_address)
		finally:
			self.huecos.release()


def acotar_limites():
	"""Una sola IP no debe poder agotar el tope global. Si la configuración lo permite, avisa y la acota (sin dejar de arrancar)."""
	global LIMITE_IP_DIA, LIMITE_POR_HORA
	ip_dia = max(1, min(LIMITE_IP_DIA, LIMITE_POR_DIA - 1))
	por_hora = max(1, min(LIMITE_POR_HORA, ip_dia))
	if (ip_dia, por_hora) != (LIMITE_IP_DIA, LIMITE_POR_HORA):
		print(f"AVISO: límites acotados a LIMITE_POR_HORA={por_hora} y LIMITE_IP_DIA={ip_dia} (LIMITE_POR_DIA={LIMITE_POR_DIA})", file=sys.stderr)
	LIMITE_IP_DIA, LIMITE_POR_HORA = ip_dia, por_hora


acotar_limites()

if __name__ == "__main__":
	_cargar()
	print(f"Límites guardados en: {FICHERO_LIMITES or 'solo memoria'}", file=sys.stderr)
	print(f"Estimador web en http://localhost:{PUERTO} · modo {modo()}")
	Servidor(("0.0.0.0", PUERTO), partial(Manejador, directory=str(CARPETA / "publico"))).serve_forever()
