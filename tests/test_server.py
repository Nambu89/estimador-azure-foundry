"""Tests del proxy web (web/server.py): límites, conversación atada a una clave, tokens y conexiones. Sin red."""
import email.message
import importlib.util
import io
import itertools
import json
import socket
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

RAIZ = Path(__file__).parent.parent
CLAVE_A, CLAVE_B = "a" * 32, "b" * 32
# Variables que leen web/server.py al importarse: fuera, para que el .env del shell no cambie los tests
VARIABLES = ["SIMULADO", "AGENTE_URL", "LIMITE_POR_HORA", "LIMITE_IP_DIA", "LIMITE_POR_DIA", "LIMITE_TOKENS_DIA",
	"RESERVA_TOKENS", "MAX_TURNOS", "FICHERO_LIMITES", "WEBSITE_SITE_NAME"]
SIN_PROXY = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # El proxy del sistema no debe meterse en localhost


def _cargar_modulo(nombre):
	spec = importlib.util.spec_from_file_location(nombre, RAIZ / "web" / "server.py")
	modulo = importlib.util.module_from_spec(spec)
	spec.loader.exec_module(modulo)
	return modulo


@pytest.fixture
def server(monkeypatch, tmp_path):
	"""Un web/server.py nuevo en cada test, con contadores vacíos y persistencia en un fichero temporal."""
	for v in VARIABLES:
		monkeypatch.delenv(v, raising=False)
	modulo = _cargar_modulo("server_web")
	monkeypatch.setattr(modulo, "FICHERO_LIMITES", str(tmp_path / "limites.json"))
	return modulo


def _cabeceras(**valores):
	m = email.message.Message()
	for k, v in valores.items():
		for x in (v if isinstance(v, list) else [v]):
			m[k.replace("_", "-")] = x
	return m


def _esperar_huecos(srv, total, plazo=2):
	"""El hueco se libera en el finally del hilo: se espera a que vuelvan todos (sin un sleep fijo)."""
	fin = time.time() + plazo
	while srv.huecos._value != total and time.time() < fin:
		time.sleep(0.01)
	return srv.huecos._value == total


def _evento(tipo, rid="caresp_x", tokens=None, tipo_al_final=False):
	respuesta = {"id": rid, **({"usage": {"total_tokens": tokens}} if tokens else {})}
	datos = {"response": respuesta, "type": tipo} if tipo_al_final else {"type": tipo, "response": respuesta}
	return b"data: " + json.dumps(datos).encode()


def test_ip_cliente_usa_la_ultima_entrada_de_x_forwarded_for(server):
	# Lo que manda el cliente va delante; la plataforma añade al final la IP real (con puerto)
	assert server.ip_cliente(_cabeceras(X_Forwarded_For="6.6.6.6, 203.0.113.7:51234"), "10.0.0.1") == "203.0.113.7"
	assert server.ip_cliente(_cabeceras(X_Client_IP="6.6.6.6"), "10.0.0.1") == "10.0.0.1"  # X-Client-IP ya no cuenta
	assert server.ip_cliente(_cabeceras(X_Forwarded_For="no-es-una-ip"), "10.0.0.1") == "10.0.0.1"
	assert server.ip_cliente(_cabeceras(X_Forwarded_For="fe80::1%" + "z" * 5000), "10.0.0.1") == "10.0.0.1"
	assert server.ip_cliente(_cabeceras(X_Forwarded_For="fe80::1%eth0"), "10.0.0.1") == "10.0.0.1"  # scope_id


def test_ipv6_se_agrupa_por_64(server):
	una = server.ip_cliente(_cabeceras(X_Forwarded_For=["1.1.1.1", "[2001:db8::1]:443"]), "10.0.0.1")
	otra = server.ip_cliente(_cabeceras(X_Forwarded_For="2001:db8::ffff"), "10.0.0.1")
	assert una == otra == "2001:db8::/64"
	# IPv4 escrita como IPv6: cuenta como su IPv4, no todas en «::/64»
	assert server.ip_cliente(_cabeceras(X_Forwarded_For="[::ffff:203.0.113.7]:443"), "10.0.0.1") == "203.0.113.7"
	assert server.ip_cliente(_cabeceras(X_Forwarded_For="::ffff:198.51.100.9"), "10.0.0.1") == "198.51.100.9"


def test_limites_incoherentes_se_acotan_sin_tumbar_el_arranque(monkeypatch, capsys):
	"""main.json desplegaba LIMITE_POR_HORA=30; con un assert, la web no arrancaba en App Service."""
	plantilla = json.loads((RAIZ / "web" / "infra" / "main.json").read_text(encoding="utf-8"))
	monkeypatch.setenv("LIMITE_POR_HORA", str(plantilla["parameters"]["limitePorHora"]["defaultValue"]))
	capsys.readouterr()
	_cargar_modulo("server_plantilla")
	assert "AVISO" not in capsys.readouterr().err  # lo que despliega la plantilla es coherente
	monkeypatch.setenv("LIMITE_POR_HORA", "30")
	monkeypatch.setenv("LIMITE_POR_DIA", "20")
	modulo = _cargar_modulo("server_acotado")
	assert (modulo.LIMITE_POR_HORA, modulo.LIMITE_IP_DIA) == (19, 19)
	assert "AVISO" in capsys.readouterr().err
	monkeypatch.setenv("LIMITE_POR_DIA", "0")
	modulo = _cargar_modulo("server_cerrado")
	assert (modulo.LIMITE_POR_HORA, modulo.LIMITE_IP_DIA) == (1, 1)  # suelo de 1, nunca 0 ni negativos


def test_una_ip_no_agota_el_tope_global(server, monkeypatch):
	monkeypatch.setattr(server, "LIMITE_POR_HORA", 100)
	monkeypatch.setattr(server, "LIMITE_IP_DIA", 3)
	monkeypatch.setattr(server, "LIMITE_POR_DIA", 5)
	assert [server.admitir("1.1.1.1")[0] for _ in range(3)] == [None] * 3
	assert "tu límite" in server.admitir("1.1.1.1")[0]
	assert server.admitir("2.2.2.2")[0] is None  # a otro visitante aún le queda tope global
	assert len(server._peticiones["*"]) == 4  # el rechazo por IP no gastó tope global


def test_la_reserva_cuenta_las_preguntas_en_curso(server, monkeypatch):
	monkeypatch.setattr(server, "LIMITE_TOKENS_DIA", 2000)
	monkeypatch.setattr(server, "RESERVA_TOKENS", 1000)
	assert server.admitir("1.1.1.1")[0] is None
	assert server.admitir("2.2.2.2")[0] is None
	assert "presupuesto" in server.admitir("3.3.3.3")[0]  # dos preguntas en curso ya ocupan el presupuesto


def test_el_uso_real_sustituye_a_la_reserva(server, monkeypatch):
	monkeypatch.setattr(server, "RESERVA_TOKENS", 1000)
	_, reserva = server.admitir("1.1.1.1")
	server.mirar_evento(_evento("response.completed", tokens=250), "h", 1, reserva)
	server.mirar_evento(_evento("response.completed", tokens=999), "h", 1, reserva)  # solo cuenta la primera
	assert server.tokens_de_hoy(time.time()) == 250


def test_si_el_navegador_se_va_se_siguen_apuntando_los_tokens(server, monkeypatch):
	class Roto(io.RawIOBase):
		def write(self, b):
			raise BrokenPipeError

	monkeypatch.setattr(server, "RESERVA_TOKENS", 1000)
	_, reserva = server.admitir("1.1.1.1")
	manejador = server.Manejador.__new__(server.Manejador)
	manejador.wfile, manejador.empezar_stream = Roto(), lambda: None
	manejador.copiar(iter([_evento("response.created") + b"\n", _evento("response.completed", tokens=321) + b"\n"]), "h", 1, reserva)
	assert server.tokens_de_hoy(time.time()) == 321


def test_si_el_agente_falla_la_pregunta_no_cuenta_para_el_dia(server):
	for _ in range(server.LIMITE_POR_HORA + 1):
		_, reserva = server.admitir("1.1.1.1")
		if reserva is None:
			break
		server.devolver("1.1.1.1", reserva)
	assert not server._tokens  # la reserva sale de la cola: no crece con cada fallo
	assert not server._peticiones["*"] and not server._peticiones["1.1.1.1|d"]
	assert len(server._peticiones["1.1.1.1|h"]) == server.LIMITE_POR_HORA  # la marca por hora se queda: no se repite sin tope


def test_devolver_tambien_se_guarda(server, monkeypatch):
	_, reserva = server.admitir("1.1.1.1")
	server.devolver("1.1.1.1", reserva)
	nuevo = _cargar_modulo("server_web4")
	monkeypatch.setattr(nuevo, "FICHERO_LIMITES", server.FICHERO_LIMITES)
	nuevo._cargar()
	assert nuevo.tokens_de_hoy(time.time()) == 0 and not nuevo._peticiones["*"]


def test_devolver_quita_su_reserva_y_no_otra_igual(server, monkeypatch):
	monkeypatch.setattr(server, "time", SimpleNamespace(time=lambda: 1000.0, sleep=time.sleep))  # mismo instante
	_, a = server.admitir("1.1.1.1")
	_, b = server.admitir("2.2.2.2")
	server.devolver("2.2.2.2", b)
	assert len(server._tokens) == 1 and server._tokens[0] is a


def test_si_falla_la_escritura_no_quedan_temporales(server, monkeypatch, tmp_path):
	carpeta = tmp_path / "es-una-carpeta"
	carpeta.mkdir()
	monkeypatch.setattr(server, "FICHERO_LIMITES", str(carpeta))  # os.replace sobre una carpeta falla
	server.admitir("1.1.1.1")
	assert list(tmp_path.iterdir()) == [carpeta]


def test_una_pregunta_en_curso_sobrevive_a_un_reinicio(server, monkeypatch):
	monkeypatch.setattr(server, "RESERVA_TOKENS", 777)
	server.admitir("1.1.1.1")  # el proceso cae antes de que llegue el uso real
	nuevo = _cargar_modulo("server_web3")
	monkeypatch.setattr(nuevo, "FICHERO_LIMITES", server.FICHERO_LIMITES)
	nuevo._cargar()
	assert nuevo.tokens_de_hoy(time.time()) == 777 and len(nuevo._peticiones["1.1.1.1|d"]) == 1


def test_los_limites_diarios_sobreviven_a_un_reinicio(server, monkeypatch):
	monkeypatch.setattr(server, "LIMITE_IP_DIA", 1)
	_, reserva = server.admitir("1.1.1.1")
	server.apuntar_tokens(reserva, 1234)
	nuevo = _cargar_modulo("server_web2")
	monkeypatch.setattr(nuevo, "FICHERO_LIMITES", server.FICHERO_LIMITES)
	monkeypatch.setattr(nuevo, "LIMITE_IP_DIA", 1)
	nuevo._cargar()
	assert len(nuevo._peticiones["*"]) == 1
	assert nuevo.tokens_de_hoy(time.time()) == 1234
	assert "tu límite" in nuevo.admitir("1.1.1.1")[0]  # la misma IP sigue bloqueada tras el reinicio


def test_un_fichero_danado_no_rompe_el_arranque(server, capsys):
	Path(server.FICHERO_LIMITES).write_text("{roto")
	server._cargar()
	assert "AVISO" in capsys.readouterr().err


def test_las_claves_por_ip_estan_acotadas(server, monkeypatch):
	monkeypatch.setattr(server, "MAX_CLAVES", 10)
	for i in range(20):
		server.admitir(f"10.0.0.{i}")
	assert len(server._peticiones) <= 11
	assert "tráfico" in server.admitir("10.0.1.1")[0]


def test_las_claves_viejas_se_purgan(server, monkeypatch):
	monkeypatch.setattr(server, "MAX_CLAVES", 10)
	for i in range(20):
		server._peticiones[f"10.0.0.{i}|d"].append(time.time() - 90000)  # de hace más de un día
		server._peticiones[f"10.0.0.{i}|h"].append(time.time() - 7200)  # las de hora caducan en una hora
	assert server.admitir("10.0.1.1")[0] is None
	assert "10.0.0.1|d" not in server._peticiones and "10.0.0.1|h" not in server._peticiones


def test_eventos_con_tipos_raros_no_rompen_el_stream(server):
	_, reserva = server.admitir("1.1.1.1")
	for raro in (b'data: {"type": "response.completed", "response": "x"}',
			b'data: ["response.completed"]',
			b'data: {"type": "response.completed", "response": {"usage": "x"}}',
			b'data: {"type": "response.created", "response": {"id": {"a": 1}}}',
			b'data: {"type": "response.completed", "response": {"usage": {"total_tokens": true}}}',
			b'data: {"type": "response.completed", "response": {"usage": {"total_tokens": "9", "input_tokens": 5, "output_tokens": null}}}'):
		server.mirar_evento(raro, "h", 1, reserva)
	assert server.tokens_de_hoy(time.time()) == 5  # solo cuentan los enteros (True no)
	assert not server._conversaciones


@pytest.mark.parametrize("tipo, cuenta", [("response.incomplete", True), ("response.failed", True), ("response.created", False)])
def test_que_eventos_apuntan_tokens(server, monkeypatch, tipo, cuenta):
	"""incomplete (corte por max_tokens) y failed traen el uso real; created no, y si apuntase taparía al de completed."""
	monkeypatch.setattr(server, "RESERVA_TOKENS", 1000)
	_, reserva = server.admitir("1.1.1.1")
	server.mirar_evento(_evento(tipo, tokens=300), "h", 1, reserva)
	assert server.tokens_de_hoy(time.time()) == (300 if cuenta else 1000)


def test_una_linea_sin_fin_no_impide_apuntar_los_tokens(server, monkeypatch):
	class Nulo(io.RawIOBase):
		def write(self, b):
			return len(b)

	monkeypatch.setattr(server, "RESERVA_TOKENS", 1000)
	_, reserva = server.admitir("1.1.1.1")
	manejador = server.Manejador.__new__(server.Manejador)
	manejador.wfile, manejador.empezar_stream = Nulo(), lambda: None
	manejador.copiar(iter([b"x" * 4_000_001, b"\n" + _evento("response.completed", tokens=42) + b"\n"]), "h", 1, reserva)
	assert server.tokens_de_hoy(time.time()) == 42


def test_la_conversacion_solo_la_sigue_quien_tiene_la_clave(server):
	h = server.huella(CLAVE_A)
	server.mirar_evento(_evento("response.created", tipo_al_final=True), h, 1, None)  # el orden del JSON da igual
	assert server.tomar_conversacion("caresp_x", CLAVE_B) is None
	assert server.tomar_conversacion("caresp_inventado", CLAVE_A) is None
	assert server.tomar_conversacion("caresp_x", CLAVE_A) == 1
	assert server.tomar_conversacion("caresp_x", CLAVE_A) == 1  # no se consume: un stream cortado se puede reintentar


@pytest.mark.parametrize("cuerpo", [b"[]", b'"hola"', b"[" * 10000 + b"]" * 10000, b"[" * 15000, b"{roto", b'{"input": "hola"}',
	b'{"input": "hola", "clave": "no-es-hexadecimal-y-tiene-largo"}'])
def test_validar_rechaza_cuerpos_raros_y_claves_malas(server, cuerpo):
	with pytest.raises(ValueError):
		server.validar(cuerpo)


def test_conexiones_limitadas_y_liberadas(server):
	srv = server.Servidor(("127.0.0.1", 0), lambda *a: server.Manejador(*a, directory=str(RAIZ / "web" / "publico")))
	threading.Thread(target=srv.serve_forever, daemon=True).start()
	try:
		for _ in range(server.MAX_CONEXIONES + 5):  # más que huecos: si se perdiera alguno, fallaría
			with SIN_PROXY.open(f"http://127.0.0.1:{srv.server_address[1]}/api/estado", timeout=5) as r:
				assert r.status == 200
		assert _esperar_huecos(srv, server.MAX_CONEXIONES)
		for _ in range(server.MAX_CONEXIONES):
			assert srv.huecos.acquire(blocking=False)
		a, b = socket.socketpair()
		srv.process_request(a, ("127.0.0.1", 1))  # sin hueco: 503 y se cierra sin crear hilo
		assert b.recv(100).startswith(b"HTTP/1.0 503")
	finally:
		srv.shutdown()
		srv.server_close()


@pytest.fixture
def web(server, monkeypatch):
	"""Servidor de verdad en un puerto libre. Devuelve preguntar(cuerpo, tipo) -> (estado, cuerpo)."""
	srv = server.Servidor(("127.0.0.1", 0), lambda *a: server.Manejador(*a, directory=str(RAIZ / "web" / "publico")))
	threading.Thread(target=srv.serve_forever, daemon=True).start()
	url = f"http://127.0.0.1:{srv.server_address[1]}/api/responses"

	def preguntar(cuerpo, tipo="application/json"):
		req = urllib.request.Request(url, data=json.dumps(cuerpo).encode(), headers={"Content-Type": tipo})
		try:
			with SIN_PROXY.open(req, timeout=10) as r:
				return r.status, r.read()
		except urllib.error.HTTPError as e:
			return e.code, e.read()

	preguntar.srv = srv
	yield preguntar
	srv.shutdown()
	srv.server_close()


def _id_de(sse):
	return next(json.loads(l[5:])["response"]["id"] for l in sse.split(b"\n") if l.startswith(b"data:") and b'"response.created"' in l)


def test_punta_a_punta_simulado_con_dos_visitantes(server, web, monkeypatch):
	monkeypatch.setattr(server, "SIMULADO", True)
	monkeypatch.setattr(server, "time", SimpleNamespace(time=time.time, sleep=lambda s: None))  # sin las pausas de la reproducción
	estado, sse_a = web({"input": "hola", "clave": CLAVE_A})
	assert estado == 200
	_, sse_b = web({"input": "hola", "clave": CLAVE_B})  # otro visitante entre medias
	id_a, id_b = _id_de(sse_a), _id_de(sse_b)
	assert id_a != id_b
	assert web({"input": "sigue", "clave": CLAVE_B, "previous_response_id": id_a})[0] == 403
	assert web({"input": "sigue", "clave": CLAVE_A, "previous_response_id": id_a})[0] == 200
	assert web({"input": "hola", "clave": CLAVE_A}, "text/plain")[0] == 415
	assert server.tokens_de_hoy(time.time()) == 0  # la demo grabada no gasta presupuesto


def test_punta_a_punta_tope_de_turnos(server, web, monkeypatch):
	monkeypatch.setattr(server, "SIMULADO", True)
	monkeypatch.setattr(server, "time", SimpleNamespace(time=time.time, sleep=lambda s: None))  # sin las pausas de la reproducción
	monkeypatch.setattr(server, "MAX_TURNOS", 2)
	_, sse = web({"input": "hola", "clave": CLAVE_A})
	_, sse = web({"input": "sigue", "clave": CLAVE_A, "previous_response_id": _id_de(sse)})
	segundo = _id_de(sse)
	for _ in range(2):  # el rechazo se repite igual: no se convierte en un 403
		estado, cuerpo = web({"input": "otra", "clave": CLAVE_A, "previous_response_id": segundo})
		assert estado == 429 and "conversación" in json.loads(cuerpo)["error"]


def test_punta_a_punta_agente_caido(server, web, monkeypatch):
	with socket.socket() as s:  # un puerto en el que no escucha nadie
		s.bind(("127.0.0.1", 0))
		puerto = s.getsockname()[1]
	monkeypatch.setattr(server, "AGENTE_URL", f"http://127.0.0.1:{puerto}/responses")
	server.registrar_respuesta("caresp_x", server.huella(CLAVE_A), 1)
	estado, _ = web({"input": "sigue", "clave": CLAVE_A, "previous_response_id": "caresp_x"})
	assert estado == 502
	assert not server._peticiones["*"] and not server._tokens
	assert server.tomar_conversacion("caresp_x", CLAVE_A) == 1  # la conversación sigue sirviendo
	monkeypatch.setattr(server, "LIMITE_TOKENS_DIA", 0)
	estado, cuerpo = web({"input": "sigue", "clave": CLAVE_A, "previous_response_id": "caresp_x"})
	assert estado == 429 and "presupuesto" in json.loads(cuerpo)["error"]
	assert server.tomar_conversacion("caresp_x", CLAVE_A) == 1


def test_plazo_de_lectura_corta_a_quien_gotea(server, web, monkeypatch, capfd):
	monkeypatch.setattr(server, "PLAZO_PETICION", 0.5)
	inicio = time.time()
	with socket.create_connection(web.srv.server_address, timeout=5) as c:
		c.sendall(b"POST /api/responses HTTP/1.1\r\nContent-Type: application/json\r\nContent-Length: 100\r\n\r\n{")
		assert c.recv(100) == b""  # se cierra sin responder
	assert time.time() - inicio < 3
	assert _esperar_huecos(web.srv, server.MAX_CONEXIONES)
	err = capfd.readouterr().err
	assert '" 400 ' not in err and "Traceback" not in err  # sin un 400 sobre un socket muerto


class AgenteFalso(BaseHTTPRequestHandler):
	"""Agente SSE en localhost: response.created con id nuevo, deltas con pausa y response.completed con usage."""
	estado, pausa, deltas, tokens = 200, 0.0, 2, 1234
	ids = itertools.count()

	def do_POST(self):
		self.rfile.read(int(self.headers["Content-Length"]))
		if self.estado != 200:
			self.send_response(self.estado)
			self.send_header("Content-Length", "0")
			self.end_headers()
			return
		rid = f"caresp_falso_{next(self.ids)}"
		self.send_response(200)
		self.send_header("Content-Type", "text/event-stream")
		self.end_headers()
		self.wfile.write(b"event: response.created\n" + _evento("response.created", rid) + b"\n\n")
		for _ in range(self.deltas):
			time.sleep(self.pausa)
			self.wfile.write(b'data: {"type": "response.output_text.delta", "delta": "x"}\n\n')
			self.wfile.flush()
		self.wfile.write(_evento("response.completed", rid, tokens=self.tokens) + b"\n\n")

	def log_message(self, *a):
		pass


@pytest.fixture
def agente(server, monkeypatch):
	"""Levanta AgenteFalso y apunta AGENTE_URL a él. Devuelve la clase para cambiar su comportamiento."""
	clase = type("Agente", (AgenteFalso,), {})
	srv = ThreadingHTTPServer(("127.0.0.1", 0), clase)
	threading.Thread(target=srv.serve_forever, daemon=True).start()
	monkeypatch.setattr(server, "AGENTE_URL", f"http://127.0.0.1:{srv.server_address[1]}/responses")
	yield clase
	srv.shutdown()
	srv.server_close()


def test_agente_real_tope_de_turnos_y_tokens(server, web, agente, monkeypatch):
	monkeypatch.setattr(server, "MAX_TURNOS", 2)
	estado, sse = web({"input": "hola", "clave": CLAVE_A})
	assert estado == 200 and b"response.completed" in sse
	assert server.tokens_de_hoy(time.time()) == 1234  # el uso real, no la reserva
	_, sse = web({"input": "sigue", "clave": CLAVE_A, "previous_response_id": _id_de(sse)})
	estado, _ = web({"input": "otra", "clave": CLAVE_A, "previous_response_id": _id_de(sse)})
	assert estado == 429


def test_un_stream_largo_no_lo_corta_el_plazo(server, web, agente, monkeypatch):
	monkeypatch.setattr(server, "PLAZO_PETICION", 0.5)
	agente.pausa, agente.deltas = 0.3, 5  # 1,5 s de stream, el triple del plazo de lectura
	estado, sse = web({"input": "hola", "clave": CLAVE_A})
	assert estado == 200 and b"response.completed" in sse


def test_agente_con_error_http_da_502(server, web, agente):
	agente.estado = 500
	estado, cuerpo = web({"input": "hola", "clave": CLAVE_A})
	assert estado == 502 and "500" in json.loads(cuerpo)["error"]
	assert not server._tokens and not server._peticiones["*"]
