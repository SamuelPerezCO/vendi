# Documentación

`Vendi-documentacion.pdf` es la documentación de la Fase 1 de Vendi, el CRM a
la medida de Tratamientos LB S.A.S. Está en español y en lenguaje sencillo:
qué es, el problema que resuelve frente a Mercately, lo que se logró en la
Fase 1, qué hace la aplicación, cómo funciona por dentro, cómo guarda los
datos, los requisitos, las pantallas señaladas con recuadros rojos, los
servicios en los que corre, qué contiene cada carpeta y cómo se ejecuta.

## Cómo regenerar el PDF

```bash
cd docs/generar
uv run --with reportlab python documentacion.py ../Vendi-documentacion.pdf
```

- `generar/documentacion.json`: todo el contenido (texto, tablas, lista de
  avance, diagramas y qué pantallas mostrar con sus recuadros). Para cambiar
  una frase, edita este archivo y vuelve a correr el comando.
- `generar/documentacion.py`: arma el documento.
- `generar/render.py`: estilos, tablas, diagramas e imágenes que usa el
  documento.
- `generar/capturas/`: las capturas de pantalla (1440×900) y, en
  `recuadros.json`, la posición de cada recuadro rojo. Para renovarlas,
  arranca la app con `MESSAGING_PROVIDER=meta`, crea algunos datos de ejemplo
  y vuelve a capturar con `capturar.py`, que guarda las imágenes y las
  posiciones juntas.

Los archivos `academico.json`, `chapters.json`, `front.json` y `assemble.py`
son las fuentes de la documentación técnica extensa, que ya no se publica en
el repositorio.
