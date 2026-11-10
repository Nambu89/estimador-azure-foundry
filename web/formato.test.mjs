// Pruebas de formato.js. Ejecuta: node web/formato.test.mjs
import assert from "node:assert/strict";
import { numero, primerJSON, buscarTotal } from "./publico/formato.js";

assert.equal(numero("116,65 €"), 116.65);
assert.equal(numero("1.234,56 €"), 1234.56);
assert.equal(numero("1,234.56"), 1234.56);
assert.equal(numero("0.1598"), 0.1598);
assert.equal(numero("2.000.000"), 2000000);
assert.equal(numero("12,345"), 12345);
assert.equal(numero("**350,49 €**"), 350.49);
assert.equal(numero("sin cifra"), null);

assert.deepEqual(primerJSON('{"a":1}{"a":1}'), { a: 1 });
assert.deepEqual(primerJSON('{"q":"llaves } dentro"}'), { q: "llaves } dentro" });
assert.equal(primerJSON(""), null);
assert.equal(primerJSON("{roto"), null);

assert.equal(buscarTotal("| **Total** | | | | **350,49 €** |\n\n**Total mensual: 350,49 €**"), "350,49");
assert.equal(buscarTotal("El total mensual es de EUR 1.204,10."), "1.204,10");
assert.equal(buscarTotal("**Total mensual:** 722.91 EUR"), "722.91");
assert.equal(numero("722.91"), 722.91);
assert.equal(buscarTotal("- Total mensual (suma oficial): 358.37 EUR\n- El total no incluye la salida de 5 EUR"), "358.37");
assert.equal(buscarTotal("Sin importes"), null);

console.log("formato.js: todo bien");
