// Frontend del estimador: habla con /api/responses (server.py) y pinta la respuesta del agente.
import { marked } from "https://cdn.jsdelivr.net/npm/marked@15.0.12/lib/marked.esm.js";
import DOMPurify from "https://cdn.jsdelivr.net/npm/dompurify@3.2.6/dist/purify.es.mjs";
import { numero, primerJSON, buscarTotal } from "./formato.js";

const $ = (selector) => document.querySelector(selector);
const conversacion = $("#conversacion");
const inicio = $("#inicio");
const formulario = $("#compositor");
const pregunta = $("#pregunta");
const enviar = $("#enviar");
const nueva = $("#nueva");
const anuncio = $("#anuncio");

// Del verde fósforo al verde bosque, para los servicios de la estimación
const VERDES = ["#5cff9d", "#2ed47a", "#2e9e62", "#1f7a4a", "#1d5e3e", "#3f6f58", "#5e8a75"];
const NOMBRES = {
	microsoft_docs_search: "learn.buscar",
	microsoft_docs_fetch: "learn.leer",
	buscar_precios: "precios.buscar",
	calcular_estimacion: "precios.calcular",
};

let anterior = null; // previous_response_id: así el agente recuerda la conversación
let ocupado = false;

// Los enlaces de la respuesta se abren fuera, sin dar acceso a esta ventana
DOMPurify.addHook("afterSanitizeAttributes", (nodo) => {
	if (nodo.tagName === "A") {
		nodo.setAttribute("target", "_blank");
		nodo.setAttribute("rel", "noopener noreferrer");
	}
});
const html = (markdown) => DOMPurify.sanitize(marked.parse(markdown));

fetch("api/estado")
	.then((r) => r.json())
	.then(({ modo }) => {
		const textos = { local: "agente local", foundry: "Foundry · Sweden Central", simulado: "modo simulado" };
		const chapa = $("#modo");
		chapa.textContent = textos[modo] ?? modo;
		chapa.dataset.modo = modo;
		chapa.hidden = false;
	})
	.catch(() => {});

// ---------- Entrada ----------

formulario.addEventListener("submit", (e) => {
	e.preventDefault();
	const texto = pregunta.value.trim();
	if (!texto || ocupado) return;
	pregunta.value = "";
	preguntar(texto);
});

pregunta.addEventListener("keydown", (e) => {
	// Enter envía, Mayús+Enter salta de línea (y no enviamos a mitad de una composición con IME)
	if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
		e.preventDefault();
		formulario.requestSubmit();
	}
});

for (const boton of document.querySelectorAll(".ejemplo")) {
	boton.addEventListener("click", () => preguntar(boton.textContent.trim()));
}

nueva.addEventListener("click", () => {
	if (ocupado) return;
	anterior = null;
	conversacion.replaceChildren(inicio);
	inicio.hidden = false;
	nueva.hidden = true;
	pregunta.focus();
});

// ---------- Conversación ----------

async function preguntar(texto) {
	ocupado = true;
	enviar.setAttribute("aria-disabled", "true");
	inicio.hidden = true;
	nueva.hidden = false;

	const usuario = document.createElement("div");
	usuario.className = "mensaje usuario";
	usuario.textContent = texto;
	conversacion.append(usuario);

	const vista = crearMensaje();
	conversacion.append(vista.raiz);
	vista.raiz.scrollIntoView({ behavior: "smooth", block: "start" });

	try {
		const respuesta = await fetch("api/responses", {
			method: "POST",
			headers: { "Content-Type": "application/json" },
			body: JSON.stringify({ input: texto, ...(anterior && { previous_response_id: anterior }) }),
		});
		if (!respuesta.ok) {
			const { error } = await respuesta.json().catch(() => ({}));
			throw new Error(error || `El servidor ha respondido ${respuesta.status}.`);
		}
		const id = await leerStream(respuesta.body, vista);
		if (id) anterior = id;
	} catch (error) {
		vista.fallo(error.message);
	} finally {
		vista.terminar();
		ocupado = false;
		enviar.removeAttribute("aria-disabled");
	}
}

// Lee los eventos SSE del protocolo Responses. Devuelve el id de la respuesta si terminó bien.
async function leerStream(cuerpo, vista) {
	const lector = cuerpo.pipeThrough(new TextDecoderStream()).getReader();
	let pendiente = "";
	let id = null;
	while (true) {
		const { value, done } = await lector.read();
		if (done) break;
		pendiente += value;
		const bloques = pendiente.split("\n\n");
		pendiente = bloques.pop();
		for (const bloque of bloques) {
			const datos = bloque.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5)).join("\n");
			if (!datos.trim()) continue;
			const evento = JSON.parse(datos);
			const fin = vista.evento(evento);
			if (fin === "completada") id = evento.response.id;
		}
	}
	if (!id && !vista.fallido) vista.fallo("La respuesta se ha cortado antes de terminar. Prueba otra vez.");
	return id;
}

// Crea el mensaje del agente y devuelve las funciones que lo van rellenando
function crearMensaje() {
	const raiz = document.createElement("article");
	raiz.className = "mensaje agente";
	raiz.setAttribute("aria-busy", "true");
	raiz.innerHTML = `
		<details class="traza" open>
			<summary><span class="pulso"></span><span class="traza-estado">Pensando…</span><span class="crono">0 s</span></summary>
			<ol class="pasos"></ol>
		</details>
		<div class="prosa"></div>`;
	const traza = raiz.querySelector(".traza");
	const estado = raiz.querySelector(".traza-estado");
	const crono = raiz.querySelector(".crono");
	const pasos = raiz.querySelector(".pasos");
	const prosa = raiz.querySelector(".prosa");

	const empiezo = performance.now();
	const segundos = () => Math.round((performance.now() - empiezo) / 1000);
	const reloj = setInterval(() => (crono.textContent = `${segundos()} s`), 1000);

	const porLlamada = new Map(); // call_id → <li>, para pintar el resultado en su paso
	const cuenta = {};
	let texto = "";
	let marco = 0; // requestAnimationFrame pendiente del texto en streaming

	function paso(item) {
		const li = document.createElement("li");
		li.className = "paso";
		li.dataset.estado = "vivo";
		li.innerHTML = `<span class="paso-herramienta"></span><span class="paso-detalle"></span><span class="paso-resultado"></span>`;
		li.querySelector(".paso-herramienta").textContent = NOMBRES[item.name] ?? item.name;
		pasos.append(li);
		porLlamada.set(item.call_id ?? item.id, li);
		return li;
	}

	function detalle(item) {
		const args = primerJSON(item.arguments) ?? {};
		if (args.query) return `«${args.query}»`;
		if (args.url) return args.url.replace(/^https:\/\//, "");
		if (args.servicio) return [args.servicio, args.sku_contiene, args.producto_contiene, args.region].filter(Boolean).join(" · ");
		if (args.lineas) return `${args.lineas.length} líneas`;
		return "";
	}

	function resultado(item) {
		const li = porLlamada.get(item.call_id);
		if (!li) return;
		const salida = primerJSON(item.output) ?? {};
		let texto = "hecho";
		if ("total_encontrados" in salida) texto = `${salida.total_encontrados} medidores`;
		else if (salida.medidores?.length === 0) {
			texto = "sin resultados";
			li.dataset.estado = "vacio";
		} else if (salida.total_mensual) texto = `total ${salida.total_mensual.replace(".", ",")} ${salida.moneda === "EUR" ? "€" : salida.moneda}`;
		else if (salida.error) texto = "error";
		if (li.dataset.estado !== "vacio") li.dataset.estado = "hecho";
		li.querySelector(".paso-resultado").textContent = texto;
	}

	function pintar() {
		if (marco) return;
		marco = requestAnimationFrame(() => {
			// Solo seguimos el texto si el usuario ya estaba abajo; si ha subido a leer, no le movemos
			const abajo = innerHeight + scrollY >= document.body.scrollHeight - 160;
			prosa.innerHTML = html(texto);
			if (abajo) scrollTo({ top: document.body.scrollHeight });
			marco = 0;
		});
	}

	return {
		raiz,
		fallido: false,

		// Devuelve "completada" cuando llega response.completed
		evento(e) {
			switch (e.type) {
				case "response.output_item.added": {
					const item = e.item;
					if (item.type === "reasoning") estado.textContent = "Pensando…";
					if (item.type === "mcp_call" || item.type === "function_call") {
						estado.textContent = item.type === "mcp_call" ? "Consultando Microsoft Learn…" : "Consultando precios oficiales…";
						paso(item);
					}
					if (item.type === "message") estado.textContent = "Escribiendo la respuesta…";
					break;
				}
				case "response.output_item.done": {
					const item = e.item;
					if (item.type === "mcp_call" || item.type === "function_call") {
						const li = porLlamada.get(item.call_id ?? item.id) ?? paso(item);
						li.querySelector(".paso-detalle").textContent = detalle(item);
						if (item.type === "mcp_call") {
							// La plataforma deja vacíos los argumentos de MCP: enseñamos qué página de Learn ha encontrado
							const paginas = primerJSON(item.output)?.results ?? [];
							if (!li.querySelector(".paso-detalle").textContent && paginas[0]?.title) li.querySelector(".paso-detalle").textContent = paginas[0].title;
							li.dataset.estado = item.error ? "vacio" : "hecho";
							li.querySelector(".paso-resultado").textContent = item.error ? "error" : paginas.length ? `${paginas.length} páginas` : "leído";
						}
						cuenta[item.name] = (cuenta[item.name] ?? 0) + 1;
					}
					if (item.type === "function_call_output") resultado(item);
					break;
				}
				case "response.output_text.delta":
					texto += e.delta;
					pintar();
					break;
				case "response.output_text.done":
					texto = e.text;
					break;
				case "response.completed":
					// Si queda un repintado pendiente, borraría la tabla ya realzada
					cancelAnimationFrame(marco);
					marco = 0;
					prosa.innerHTML = html(texto);
					realzar(prosa, texto);
					return "completada";
				case "response.failed":
					this.fallo(e.response?.error?.message ? `El agente ha fallado: ${e.response.error.message}` : "El agente ha fallado.");
					break;
			}
		},

		fallo(mensaje) {
			this.fallido = true;
			const aviso = document.createElement("p");
			aviso.className = "error";
			aviso.textContent = mensaje;
			raiz.append(aviso);
		},

		terminar() {
			clearInterval(reloj);
			raiz.removeAttribute("aria-busy");
			const n = pasos.children.length;
			const learn = (cuenta.microsoft_docs_search ?? 0) + (cuenta.microsoft_docs_fetch ?? 0);
			const precios = cuenta.buscar_precios ?? 0;
			const partes = [`${segundos()} s`];
			if (learn) partes.unshift(`Learn ×${learn}`);
			if (precios) partes.unshift(`precios ×${precios}`);
			estado.textContent = this.fallido ? "Sin terminar" : n ? `${n} pasos` : "Respuesta";
			crono.textContent = partes.join(" · ");
			if (n) traza.open = false;
			else if (!this.fallido) traza.hidden = true; // Una pregunta de aclaración no necesita traza
			anuncio.textContent = this.fallido ? "La respuesta ha fallado." : "Respuesta lista.";
		},
	};
}

// ---------- La estimación ----------

// Convierte la tabla de costes en fichas legibles y añade el total destacado con el reparto por servicio.
function realzar(prosa, markdown) {
	const total = buscarTotal(markdown);
	const servicios = [];
	let ancla = null; // Delante de qué elemento va el total destacado
	const tabla = prosa.querySelector("table");
	if (tabla) ancla = prepararTabla(tabla, servicios);
	if (!total) return;
	// Sin tabla (a veces el modelo la escribe como lista), el total va delante de su sección
	ancla ??= [...prosa.children].find((el) => /total mensual/i.test(el.textContent)) ?? null;

	// La línea «Total mensual: …» ya se ve en grande, así que sobra
	for (const p of prosa.querySelectorAll("p")) {
		if (/^total mensual/i.test(p.textContent.trim()) && p.textContent.length < 60) p.remove();
	}
	const bloque = crearTotal(total, servicios);
	if (ancla) ancla.before(bloque);
	else prosa.append(bloque);
	contar(bloque.querySelector(".total-cifra span"), total);
}

// Marca columnas y fila total, la envuelve para el estilo y apunta el coste de cada servicio. Devuelve el envoltorio.
function prepararTabla(tabla, servicios) {
	const cabeceras = [...tabla.querySelectorAll("th")].map((th) => th.textContent.trim());
	let columnaCoste = cabeceras.findIndex((c) => /coste|mensual/i.test(c));
	if (columnaCoste < 0) columnaCoste = cabeceras.length - 1;

	for (const fila of tabla.querySelectorAll("tbody tr")) {
		const celdas = [...fila.children];
		celdas.forEach((td, i) => {
			if (cabeceras[i]) td.dataset.etiqueta = cabeceras[i];
			if (/cantidad|precio|coste|importe/i.test(cabeceras[i] ?? "") && numero(td.textContent) !== null) td.classList.add("num");
			if (i === columnaCoste) td.classList.add("coste");
		});
		if (/total/i.test(celdas[0]?.textContent ?? "")) {
			fila.classList.add("fila-total");
			continue;
		}
		const valor = numero(celdas[columnaCoste]?.textContent ?? "");
		if (valor !== null) servicios.push({ nombre: celdas[0].textContent.trim(), valor, celda: celdas[0] });
	}
	tabla.querySelectorAll("th").forEach((th) => /cantidad|precio|coste|importe/i.test(th.textContent) && th.classList.add("num"));

	const envoltorio = document.createElement("div");
	envoltorio.className = "tabla";
	tabla.replaceWith(envoltorio);
	envoltorio.append(tabla);
	return envoltorio;
}

// El bloque del total; con servicios añade la barra de reparto
function crearTotal(total, servicios) {
	// El reparto es solo visual: los importes que se leen son los del agente, no se recalcula nada
	const suma = servicios.reduce((s, x) => s + x.valor, 0) || 1;
	const bloque = document.createElement("section");
	bloque.className = "total";
	bloque.setAttribute("aria-label", "Total mensual estimado");
	bloque.innerHTML = `
		<div>
			<p class="total-etiqueta">Coste mensual estimado</p>
			<p class="total-cifra"><span></span><small>€ / mes</small></p>
			<p class="total-nota">Pago por uso, sin impuestos ni descuentos.</p>
		</div>
		<div class="reparto">
			<div class="reparto-barra" aria-hidden="true"></div>
			<ul class="reparto-leyenda"></ul>
		</div>`;
	const barra = bloque.querySelector(".reparto-barra");
	const leyenda = bloque.querySelector(".reparto-leyenda");
	servicios.forEach((s, i) => {
		const color = VERDES[i % VERDES.length];
		const parte = document.createElement("span");
		parte.style.cssText = `flex:${s.valor};background:${color};animation-delay:${i * 80}ms`;
		barra.append(parte);
		const li = document.createElement("li");
		li.innerHTML = `<i></i><span></span><b></b>`;
		li.querySelector("i").style.background = color;
		li.querySelector("span").textContent = s.nombre;
		li.querySelector("b").textContent = `${Math.round((s.valor / suma) * 100)} %`;
		leyenda.append(li);
		const muestra = document.createElement("i");
		muestra.className = "muestra";
		muestra.style.background = color;
		s.celda.prepend(muestra);
	});
	if (!servicios.length) bloque.querySelector(".reparto").remove();
	return bloque;
}

// Cuenta desde cero hasta el total; al final deja el texto exacto del agente
function contar(nodo, total) {
	const destino = numero(total);
	if (destino === null || matchMedia("(prefers-reduced-motion: reduce)").matches) {
		nodo.textContent = total;
		return;
	}
	const formato = new Intl.NumberFormat("es-ES", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
	const empiezo = performance.now();
	const paso = (ahora) => {
		const t = Math.min((ahora - empiezo) / 900, 1);
		nodo.textContent = t < 1 ? formato.format(destino * (1 - (1 - t) ** 3)) : total;
		if (t < 1) requestAnimationFrame(paso);
	};
	requestAnimationFrame(paso);
}
