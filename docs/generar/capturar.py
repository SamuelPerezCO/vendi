#!/usr/bin/env python
"""Take the documentation screenshots and record where each red box goes.

Needs a running dev server with demo data (MESSAGING_PROVIDER=meta) and a
master user. Saves capturas/<pantalla>.png at 1440x900 and
capturas/recuadros.json with, per screen, the rectangle of every element the
document points at, as fractions of the image (so the PDF can draw the red
boxes on top at any size).

    uv run --with playwright python capturar.py http://127.0.0.1:8766 admin 'clave'
"""
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE, USER, PASSWORD = sys.argv[1:4]
OUT = Path(__file__).resolve().parent / "capturas"
VW, VH = 1440, 900


def boxes(page, spec):
    """spec: list of (id, [selectors...], pad). First selector that matches wins."""
    found = []
    for key, selectors, pad in spec:
        for sel in selectors:
            el = page.query_selector(sel)
            bb = el.bounding_box() if el else None
            if bb and bb["width"] > 2 and bb["height"] > 2:
                x = max(0, bb["x"] - pad)
                y = max(0, bb["y"] - pad)
                w = min(VW - x, bb["width"] + 2 * pad)
                h = min(VH - y, bb["height"] + 2 * pad)
                found.append({"id": key, "x": x / VW, "y": y / VH, "w": w / VW, "h": h / VH})
                break
        else:
            print("   ! no encontrado:", key, selectors)
    return found


def settle(page, seconds=1.2):
    page.wait_for_load_state("networkidle")
    time.sleep(seconds)


def main():
    result = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": VW, "height": VH}, device_scale_factor=2)
        page.goto(BASE + "/login/")
        page.fill("input[name=username]", USER)
        page.fill("input[name=password]", PASSWORD)
        page.click("button[type=submit]")
        settle(page)

        def shot(name, spec):
            path = OUT / f"{name}.png"
            found = boxes(page, spec)
            page.screenshot(path=str(path))
            result[name] = {"file": f"capturas/{name}.png", "boxes": found}
            print("ok", name, [b["id"] for b in found])

        # 1. Welcome screen: the interface shell.
        shot("bienvenida", [
            ("barra", ["nav.sidebar", ".sidebar", "aside"], 4),
            ("accesos", [".welcome__cards"], 6),
        ])

        # 2. Inbox with Laura's conversation open.
        page.goto(BASE + "/s/inbox/")
        settle(page)
        row = page.locator(".conv-row", has_text="Laura Gómez").first
        row.click()
        settle(page, 1.8)
        shot("inbox", [
            ("nuevo-chat", [".inbox-nav__head .btn-primary"], 3),
            ("entornos", [".inbox-nav__group"], 2),
            ("lista", ["#conv-list", ".list-panel__body"], 0),
            ("etiquetas", [".chat-thread__tagbar", ".chat-thread__tags"], 3),
            ("asignacion", [".chat-thread .assign", ".assign"], 3),
            ("chat", ["#chat-messages", ".chat-thread__messages"], -4),
            ("respuestas", [".composer__quick"], 3),
            ("ficha", [".details"], 2),
        ])

        # 3. Quick replies picker open in the composer.
        page.click(".composer__quick:not(.composer__quick--template)")
        time.sleep(1.2)
        shot("inbox-respuestas", [
            ("selector", [".quickreplies__pop", ".quickreplies__list"], 3),
            ("con-imagen", [".quickreplies__item:has(.quickreplies__badge--image)", ".quickreplies__badge--image"], 3),
            ("imagen-enviada", [".msg__image", ".msg__media"], 4),
        ])
        page.keyboard.press("Escape")

        # 4. Nuevo chat dialog.
        page.goto(BASE + "/s/inbox/")
        settle(page)
        page.click(".inbox-nav__head .btn-primary")
        time.sleep(1.5)
        shot("nuevo-chat", [
            ("dialogo", ["#newchat-modal"], 2),
            ("buscar", ["#newchat-filter", "#newchat-client"], 4),
        ])

        # 5. Clientes.
        page.goto(BASE + "/s/crm/?view=clientes")
        settle(page)
        shot("crm-clientes", [
            ("crear", [".crm-panel__toolbar .btn-primary"], 3),
            ("buscar", ["#client-search", ".crm-panel__toolbar .field"], 3),
            ("excel", [".crm-actions"], 3),
            ("tabla", ["#client-table .table", "#client-table"], 0),
            ("conversar", [".wa-link"], 3),
            ("acciones", [".row-actions"], 3),
        ])

        # 6. Calendario.
        page.goto(BASE + "/s/crm/?view=mi-calendario")
        settle(page, 2.0)
        shot("crm-calendario", [
            ("crear", [".calendario__side .btn-primary"], 3),
            ("mini", [".mini-cal"], 2),
            ("semana", [".cal-grid"], 0),
            ("evento", [".fc-event:has(.cal-event__client)", ".cal-event__body"], 3),
        ])

        # 7. Respuestas rápidas.
        page.goto(BASE + "/s/mensajeria/?view=respuestas-rapidas")
        settle(page)
        shot("respuestas-rapidas", [
            ("crear", [".plantillas__create"], 3),
            ("tabla", ["#reply-table .table", "#reply-table"], 0),
            ("miniatura", [".reply-thumb"], 4),
            ("activa", ["#reply-table .switch", "#reply-table .switch__knob"], 4),
        ])

        # 8. Usuarios.
        page.goto(BASE + "/s/crm/?view=usuarios")
        settle(page)
        shot("crm-usuarios", [
            ("crear", [".crm-panel__toolbar .btn-primary"], 3),
            ("tabla", ["#user-table .table", "#user-table"], 0),
            ("maestro", [".tag-pill--purple"], 3),
            ("acciones", ["#user-table tbody tr:nth-child(2) .row-actions", "#user-table .row-actions"], 3),
        ])

        # 9. Estadísticas: the message volume detail.
        page.goto(BASE + "/s/estadisticas/?view=mensajeria")
        settle(page)
        page.locator(".stat-card").first.click()
        settle(page, 3.0)
        shot("estadisticas", [
            ("periodo", [".stats-filters", ".daterange"], 3),
            ("indicadores", [".kpi-row"], 3),
            ("grafica", [".chart-card"], 2),
        ])
        browser.close()

    (OUT / "recuadros.json").write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print("recuadros.json escrito")


if __name__ == "__main__":
    main()
