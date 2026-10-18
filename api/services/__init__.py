"""Servicios de la API: lógica de orquestación que no es ni `core/` ni una ruta.

La frontera: los endpoints son delgados (validan y delegan), `core/` es puro, y
aquí vive lo que necesita I/O compuesta (varias llamadas al bróker, caché,
traducción de formas). Se importa desde las rutas, nunca al revés.
"""
