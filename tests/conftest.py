"""Estado global de main.py y precios.py puesto a cero en cada test: tipos de cambio, fallos de la referencia y cupo de salidas."""
import asyncio
from collections import deque

import pytest

import main
import precios


@pytest.fixture(autouse=True)
def precios_limpio(monkeypatch):
	monkeypatch.setattr(main, "MEDIDORES", {})
	monkeypatch.setattr(main, "PROVISIONALES", {})
	monkeypatch.setattr(precios, "_tipos", {})
	monkeypatch.setattr(precios, "_fallos_tipo", {})
	monkeypatch.setattr(precios, "_ultimas_salidas", deque())
	# El semáforo y el cerrojo se atan al primer bucle de eventos con contención: uno nuevo por test
	monkeypatch.setattr(precios, "_salidas", asyncio.Semaphore(precios.MAX_SALIDAS_A_LA_VEZ))
	monkeypatch.setattr(precios, "_cerrojo_tipos", asyncio.Lock())
