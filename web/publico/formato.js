// Funciones puras para leer la respuesta del agente. Sin DOM, para poder probarlas con node.

// Convierte «1.234,56 €», «116,65», «0.1598» o «1,234.56» en número. Devuelve null si no hay número.
export function numero(texto) {
	const limpio = String(texto).replace(/[^\d.,-]/g, "");
	if (!/\d/.test(limpio)) return null;
	const coma = limpio.lastIndexOf(",");
	const punto = limpio.lastIndexOf(".");
	let normal;
	if (coma >= 0 && punto >= 0) {
		// El separador que va último es el decimal
		normal = coma > punto ? limpio.replace(/\./g, "").replace(",", ".") : limpio.replace(/,/g, "");
	} else if (coma >= 0) {
		normal = /^-?\d{1,3}(,\d{3})+$/.test(limpio) ? limpio.replace(/,/g, "") : limpio.replace(",", ".");
	} else {
		normal = /^-?\d{1,3}(\.\d{3}){2,}$/.test(limpio) ? limpio.replace(/\./g, "") : limpio;
	}
	const valor = Number.parseFloat(normal);
	return Number.isFinite(valor) ? valor : null;
}

// Lee el primer objeto JSON de un texto. Agent Framework a veces manda los argumentos repetidos: «{…}{…}».
export function primerJSON(texto) {
	if (!texto) return null;
	let profundidad = 0;
	let enCadena = false;
	for (let i = 0; i < texto.length; i++) {
		const c = texto[i];
		if (enCadena) {
			if (c === "\\") i++;
			else if (c === '"') enCadena = false;
		} else if (c === '"') enCadena = true;
		else if (c === "{") profundidad++;
		else if (c === "}" && --profundidad === 0) {
			try {
				return JSON.parse(texto.slice(0, i + 1));
			} catch {
				return null;
			}
		}
	}
	return null;
}

// Busca el total mensual en el markdown de la respuesta («Total mensual: 350,49 €» o la fila «| **Total** | … | **350,49 €** |»).
export function buscarTotal(markdown) {
	// Primero las líneas de «total mensual» y luego cualquier «total», siempre la primera que aparezca (los supuestos van al final)
	const todas = String(markdown).split("\n");
	const lineas = [...todas.filter((l) => /total\s+mensual/i.test(l)), ...todas.filter((l) => /total/i.test(l))];
	for (const linea of lineas) {
		const encontrado = linea.match(/(\d[\d.,]*)\s*(?:€|EUR)/i) || linea.match(/(?:€|EUR)\s*(\d[\d.,]*)/i);
		if (encontrado) return encontrado[1].replace(/[.,]$/, "");
	}
	return null;
}
