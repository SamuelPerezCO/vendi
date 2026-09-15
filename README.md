# Vendi

Vendi es un CRM omnicanal hecho a la medida de **Tratamientos LB S.A.S.** para dejar de depender de Mercately, el CRM de terceros que usa hoy: una bandeja de entrada unificada para los canales de mensajería (WhatsApp, Messenger, Instagram, Facebook, TikTok), gestión de clientes, calendario, respuestas rápidas y estadísticas, todo dentro de un shell de una sola página con barra lateral de iconos. La empresa gana control sobre sus propios datos, soporte directo y crecimiento sin el techo ni los costos por agente de una plataforma externa.

## Funcionalidades

- **Inbox** — conversaciones reales filtradas por canal y asignación, con lista, chat en vivo (polling htmx), compositor con la regla de 24 horas de WhatsApp (fuera de la ventana ofrece enviar una plantilla), **Nuevo chat** para escribirle primero a un cliente, respuestas rápidas (texto o imagen) que se envían de un clic y panel de detalles del cliente.
- **CRM** — clientes con alta, edición, ficha y baja desde la tabla (nombre, teléfono con bandera de país, mail, canal), buscador, exportación a Excel, listas de clientes, calendario con el cliente visible en cada evento, y el equipo (usuarios) que un usuario maestro administra.
- **Embudos** — panel de embudos de venta con creación de nuevos embudos.
- **Automatizaciones** — flujos de chatbots y banner de Academy.
- **Mi comercio** — catálogo de productos con creación e importación.
- **Campañas, Estadísticas y Mensajería** — métricas de mensajería, plantillas de WhatsApp y respuestas rápidas propias (texto e imagen) para el compositor.

Las secciones sin pantalla propia todavía (Performance HUB, Integraciones, etc.) muestran un placeholder automáticamente; agregar una sección nueva es una línea en [core/nav.py](core/nav.py).

## Stack

- [Django 6.1](https://www.djangoproject.com/) (Python) con SQLite.
- [htmx](https://htmx.org/) para los paneles dinámicos — sin build de frontend.
- CSS y SVG propios en [static/](static/) y [templates/icons/](templates/icons/).

## Puesta en marcha

```powershell
python -m venv venv
venv\Scripts\activate        # en Linux/macOS: source venv/bin/activate
pip install -r requirements.txt
copy .env.example .env       # en Linux/macOS: cp .env.example .env
python manage.py migrate
python manage.py runserver
```

El paso del `.env` no es opcional: `MESSAGING_PROVIDER` es obligatorio y la
app no arranca sin él (ver [Mensajería](#mensajería-cambiar-de-proveedor)).
Para desarrollo local el `.env.example` ya trae `MESSAGING_PROVIDER=fake`.

Abre http://127.0.0.1:8000/ — la pantalla de bienvenida enlaza a Inbox, CRM y Embudos.

El Inbox no trae datos de ejemplo: se llena únicamente con clientes reales,
a medida que escriben por el proveedor configurado o los deja la automatización
que escribe en la misma base de datos. Ya no existe un generador de datos de
demostración; si una base heredó fixtures del antiguo `seed_conversations`
(contactos `+5730000000xx`, eventos "Evento de demostración.", el login
`asesor`), límpialos sin tocar a los clientes reales con:

```powershell
python manage.py reset_conversations --demo-only        # simulación: muestra qué borraría
python manage.py reset_conversations --demo-only --yes  # borra solo eso
```

`reset_conversations` a secas (con `--yes`) vacía el Inbox entero -- todas
las conversaciones, mensajes y contactos -- y es un simulacro hasta que se
pasa `--yes`, porque corre contra la base de producción y no hay deshacer.

Los tres comandos corren contra la base que diga `DATABASE_URL`, así que
todos nombran la base antes de tocarla y ninguno borra nada sin `--yes`. Para
apuntar a la base local en una máquina cuyo `.env` mira a Neon:

```bash
DATABASE_URL= python manage.py reset_conversations
```

## Salir a producción: dejar el CRM vacío

Antes de conectar el número real de WhatsApp, `go_live` vacía la aplicación y **conserva al equipo**: borra contactos, conversaciones, mensajes, etiquetas, eventos de calendario, listas, productos, plantillas, respuestas rápidas y las cuentas de prueba (las que solo existen como asignatario, p. ej. `asesor`), y deja intactas las cuentas que pueden iniciar sesión — las creadas en CRM > Equipo > Usuarios o con `crear_maestro` y cualquier superusuario.

```bash
python manage.py go_live          # simulación: dice qué borraría y no toca nada
python manage.py go_live --yes    # lo borra de verdad
```

Igual que `reset_conversations`: es simulación por defecto, nombra la base a la que apunta antes de tocarla y borra dentro de una sola transacción. `--keep-catalog` conserva productos, plantillas y respuestas rápidas (útil si las plantillas de WhatsApp ya están aprobadas por Meta). No borra los archivos ya subidos a Vercel Blob, solo las filas que apuntaban a ellos.

Cuál de los tres usar:

| Comando | Qué borra |
|---|---|
| `reset_conversations --demo-only` | solo los fixtures del antiguo generador (`+5730000000xx`, "Evento de demostración.", el login `asesor`) — el único seguro si ya hay clientes reales |
| `reset_conversations` | conversaciones, mensajes y contactos; deja etiquetas, plantillas, calendario y equipo |
| `go_live` | todo lo anterior más etiquetas, calendario, listas, catálogo y cuentas de prueba; deja solo al equipo |

Crea tu cuenta en **CRM > Equipo > Usuarios** *antes* de correrlo con `--yes`: si ninguna cuenta sobrevive, la simulación te avisa.

`reset_conversations` sigue existiendo para lo de siempre — vaciar solo el Inbox (conversaciones, mensajes y contactos) sin tocar etiquetas, plantillas ni calendario.

## Agentes (personas) y la pantalla Equipo

Un **agente** es a la vez un login y un asignatario: la misma identidad que
pasa la puerta de entrada es la que puede aparecer como responsable de una
conversación en el Inbox. Todos son usuarios de Django con contraseña real, y
**la base de datos es la única fuente de verdad**: quién entra, con qué
contraseña, si es maestro y si sigue activo se gestiona desde CRM > Equipo >
Usuarios, sin tocar el entorno ni volver a desplegar
([core/agents.py](core/agents.py)).

Al iniciar sesión se abre una sesión real de `django.contrib.auth` como ese
usuario. Eso es lo que hace que el filtro "Tu inbox" funcione y que cada
mensaje enviado registre quién lo escribió. En el Inbox, el desplegable junto
al estado de la conversación ("Abierta") cambia el agente asignado y guarda al
instante; "Sin asignar" la devuelve a la bandeja común.

### El primer maestro

Una base recién creada no tiene a nadie, y la pantalla de Usuarios solo la
abre un maestro. El primero se crea directamente en la base de datos:

```
python manage.py crear_maestro Samuel --name Samuel
```

pide la contraseña por terminal (no queda en el historial del shell) y crea
el usuario maestro. Sobre un usuario que ya existe no falla: le restablece la
contraseña, lo restaura si estaba desactivado y lo hace maestro, así que
también es la salida cuando ningún maestro puede entrar.

Nada de usuarios se lee del entorno. `APP_AGENTS`, `APP_LOGIN_USERNAME` y
`APP_LOGIN_PASSWORD`, que en versiones anteriores sembraban los primeros
maestros, ya no se usan: si siguen definidas, `manage.py check` las nombra
(`core.W004`) para que se borren.

Para fijar una contraseña a mano en la base sin compartirla,
`python manage.py hashear_clave` imprime su hash en tu equipo; esa línea es
la que va en `auth_user.password`.

### Usuarios (maestros y agentes)

Desde CRM > Equipo > Usuarios un **maestro** crea al resto del equipo. Un
usuario creado ahí inicia sesión por el mismo formulario, aparece en el
desplegable de asignación y en "Tu inbox", y puede marcarse también como
maestro. Los usuarios se desactivan (nunca se borran): su historial de
conversaciones y mensajes sigue apuntando a ellos. El último maestro que pueda
iniciar sesión no puede degradarse ni desactivarse: sin él nadie podría
volver a administrar el equipo.

El rol maestro vive en el grupo `Maestros` de Django, no en `is_staff`: ese
flag significa "puede entrar a /admin/", que es otra pregunta — el login
`asesor` que dejó el antiguo generador lo tiene y no por eso administra el
equipo.

## Mensajería: cambiar de proveedor

Toda la integración con WhatsApp vive en [messaging/](messaging/) detrás de una abstracción de proveedor ([messaging/providers/base.py](messaging/providers/base.py)). El proveedor activo lo decide **una sola variable**:

```
MESSAGING_PROVIDER=meta    # producción: la Cloud API de Meta
MESSAGING_PROVIDER=fake    # solo desarrollo local: simula envíos y recibos
```

La variable es **obligatoria**: sin ella la app no arranca. Antes `fake` era
el valor por defecto, y un despliegue al que se le olvidara la variable
corría feliz sobre el simulador -- palomitas moviéndose en pantalla, nada
llegando a un teléfono. Producción es clientes reales; no debe poder caer en
el simulador por accidente.

El webhook del proveedor `fake` (`/webhooks/messaging/fake/`) crea contactos y conversaciones y su única llave es `MESSAGING_FAKE_SECRET`, cuyo valor por defecto está publicado en este repositorio. Por eso solo responde donde los datos falsos tienen sentido: con `DEBUG=True` o bajo `manage.py test`. En un despliegue real devuelve 404, así que nadie puede meter clientes inventados en el Inbox ([messaging/providers/registry.py](messaging/providers/registry.py)). El webhook de Meta no cambia.

Para conectar la cuenta real de Meta:

1. Copia [.env.example](.env.example) a `.env` (está en `.gitignore`) y llena las credenciales del proveedor; expórtalas al entorno antes de `runserver` — los settings leen `os.environ` directamente.
2. El proveedor vive en [messaging/providers/meta.py](messaging/providers/meta.py) — el docstring del módulo describe exactamente qué endpoint, firma y formato de webhook usa. Para añadir otro proveedor basta un archivo hermano que implemente [messaging/providers/base.py](messaging/providers/base.py); nada fuera de ese archivo cambia: ni vistas, ni modelos, ni templates.
3. Cambia `MESSAGING_PROVIDER=meta` y registra la URL del webhook en la consola de Meta: `https://tu-dominio/webhooks/messaging/meta/` (Meta verifica primero con un GET; el endpoint ya responde el `hub.challenge`).

El webhook verifica la firma antes de tocar el payload (401 si es inválida), es idempotente por `provider_message_id` (los reintentos del proveedor no duplican mensajes) y siempre responde 200 tras autenticar, registrando errores en el log en lugar de provocar tormentas de reintentos. El envío de texto libre está bloqueado fuera de la ventana de 24 horas ([messaging/services.py](messaging/services.py)) — fuera de ella solo cabe `send_template`, igual que en la plataforma real.

## Lo que cuesta cada plantilla

Escribir a un cliente fuera de la ventana de 24 horas se hace con una plantilla, y Meta cobra cada envío según la categoría de la plantilla y el mercado del destinatario. El CRM lleva esa cuenta.

[messaging/meta_rates.py](messaging/meta_rates.py) trae la tarifa publicada por Meta — la vigente y la ya anunciada — y [messaging/pricing.py](messaging/pricing.py) elige la tarjeta por fecha, así que el precio cambia solo el día que entra en vigor una nueva. El mercado sale del indicativo del teléfono, con las dos trampas que cuestan dinero resueltas: gana el prefijo más largo (+507 empieza por +50, y +51 es Perú), y +1 no es un solo mercado (República Dominicana, Jamaica y Puerto Rico lo comparten con EE. UU. y Canadá pero facturan como resto de Latinoamérica).

El precio que el CRM calcula antes de enviar es una estimación. Meta dice lo que cobró de verdad en el recibo de entrega, y el CRM la corrige con eso: un envío que resultó gratis baja a cero, y una plantilla que Meta recategorizó se vuelve a tarifar con **su** categoría, usando la tarjeta vigente el día del envío y no la de hoy.

Para contrastar contra la contabilidad de Meta:

```bash
python manage.py meta_spend --month 2026-09    # por defecto, el mes en curso
```

Lee las analíticas de facturación de la cuenta de WhatsApp y las compara con lo que el CRM tiene guardado, señalando dónde difieren. Necesita `MESSAGING_PROVIDER=meta`, `META_WABA_ID` y un token con permiso de lectura sobre la cuenta.

Si tu cuenta paga otras tarifas (un contrato con un BSP, un precio promocional) o factura en otra moneda, `MESSAGING_TEMPLATE_RATES` se superpone fila por fila sobre la tarjeta de Meta; ver [.env.example](.env.example).

## Escribir en la base de datos desde fuera (n8n u otra automatización)

Esta base de datos es compartida: además de esta app, una automatización
inserta clientes, conversaciones y mensajes directamente en las tablas, sin
pasar por `messaging/services.py`. Eso funciona, pero hay que respetar el
contrato de abajo, porque **Django rellena sus valores por defecto en Python,
no en la base**: un `INSERT` externo no recibe ninguno. Las columnas de texto
opcionales son `NOT NULL` con `''` como valor vacío — nunca insertes `NULL`
en ellas.

La app ya no se cae con un valor desconocido (hay tests en
[messaging/tests_external_writer.py](messaging/tests_external_writer.py)),
pero *tolerar* no es *mostrar bien*: una conversación con un canal que no
existe sale con un icono genérico, y un mensaje con un estado que no existe
sale con el icono de alerta. El contrato es lo que hace que se vean bien.

### Reglas generales

- Todas las columnas de fecha son `timestamptz`; la app corre con `USE_TZ=True` y `TIME_ZONE='UTC'`. Manda siempre **UTC con offset** (`2026-09-04T15:04:05+00:00`). Una fecha sin zona se reinterpreta en la zona de tu sesión y desplaza la ventana de 24 h y todos los informes.
- Los valores de tipo enum van en **minúsculas, exactos y sin espacios**.
- Teléfonos: `+` + indicativo + dígitos, sin espacios ni guiones (`+573001112233`). El `wa_id` de WhatsApp (`573001112233`) hay que prefijarlo con `+`.

### `core_client`

| columna | valor |
|---|---|
| `first_name` | texto ≤80. El nombre del perfil, o el teléfono si no hay |
| `last_name`, `email`, `country` | `''` si no se conocen (`country` acepta ISO-3166 alfa-2 en mayúsculas, ej. `CO`) |
| `phone` | E.164 exacto, ≤20 |
| `channel` | `''` \| `whatsapp` \| `messenger` \| `instagram` \| `facebook` \| `tiktok` |
| `created_at` | `now()` |

Busca antes de insertar: `SELECT id FROM core_client WHERE phone = $1;`

### `messaging_conversation`

| columna | valor |
|---|---|
| `contact_id` | el `core_client.id` anterior |
| `channel` | `whatsapp` \| `messenger` \| `instagram-dm` \| `facebook` \| `instagram` \| `tiktok-dm` \| `tiktok-coment` |
| `status` | `open` \| `pending` \| `resolved` |
| `assigned_to_id` | `NULL` = «Sin asignar» |
| `last_message_at` | fecha del mensaje más reciente del hilo |
| `last_inbound_at` | fecha del **entrante** más reciente; sin esto el compositor queda cerrado |
| `unread_count` | entero ≥ 0 (hay CHECK); empieza en `0` |
| `created_at` | `now()` |

Reutiliza el hilo abierto antes de crear otro:

```sql
SELECT id FROM messaging_conversation
WHERE contact_id = $1 AND channel = $2 AND status <> 'resolved'
ORDER BY last_message_at DESC NULLS LAST
LIMIT 1;
```

### `messaging_message`

| columna | valor |
|---|---|
| `conversation_id` | una conversación **de ese mismo contacto** |
| `direction` | exactamente `inbound` o `outbound` |
| `body` | texto, `''` si no hay. Para media sin pie: `[imagen]` / `[video]` / `[audio]` / `[documento]` / `[sticker]` |
| `media_url` | `''` o una URL https, **≤200 caracteres** |
| `media_type` | `''` \| `image` \| `video` \| `audio` \| `document` \| `sticker` |
| `status` | `queued` \| `sent` \| `delivered` \| `read` \| `failed`. Para algo ya entregado: `delivered` |
| `provider_message_id` | el id real del proveedor (`wamid....`), ≤255, ÚNICO. Si de verdad no lo hay, `NULL` — nunca `''` (el segundo `''` viola el índice único) |
| `timestamp` | fecha del proveedor, UTC con offset |
| `sent_by_id` | `NULL`. Ojo: `NULL` en un saliente significa «automático» para el informe de Tiempos de Respuesta |

### Después de cada mensaje, en la misma transacción

Entrante:

```sql
UPDATE messaging_conversation
   SET last_message_at = $ts,
       last_inbound_at = $ts,
       unread_count    = unread_count + 1,
       status          = CASE WHEN status = 'resolved' THEN 'open' ELSE status END
 WHERE id = $conversation_id;
```

Saliente (no toques `last_inbound_at`, `unread_count` ni `status`):

```sql
UPDATE messaging_conversation
   SET last_message_at = $ts
 WHERE id = $conversation_id;
```

Envuelve cliente → conversación → mensaje → `UPDATE` en una sola transacción,
para que un fallo no deje un mensaje sin su contabilidad.

## Servicios en los que corre el CRM

En este momento Vendi corre sobre **servicios gratuitos**, suficientes para desarrollar y validar el MVP:

| Servicio | Para qué se usa | Plan |
|---|---|---|
| [Vercel](https://vercel.com) | Hosting de la aplicación: publica cada cambio de `main`, sirve los archivos estáticos y guarda las imágenes en Vercel Blob | Gratuito |
| [Neon](https://neon.com) | Base de datos PostgreSQL de producción: clientes, conversaciones, mensajes y usuarios | Gratuito |

Cuando el uso real supere los límites de esos planes, ambos servicios permiten pasar a un plan pago sin cambiar el código: basta con actualizar la cuenta.

## Deploy en Vercel

El proyecto usa el soporte nativo de Vercel para Django (detecta `manage.py` y el `WSGI_APPLICATION` de [config/settings.py](config/settings.py) automáticamente): conecta el repo en vercel.com o corre `vercel deploy`. El único paso propio es [vercel_build.sh](vercel_build.sh) (`buildCommand` en [vercel.json](vercel.json)): corre las migraciones en cada deploy de producción y deja en el log del build el origen público que irá en los links de imagen de WhatsApp. La recolección de estáticos la sigue haciendo Vercel solo.

En el dashboard del proyecto (Settings → Environment Variables) define, como mínimo:

- `SECRET_KEY` — cualquier string largo y aleatorio (sin esto usa un valor de desarrollo inseguro).
- `DEBUG=False`
- `MESSAGING_PROVIDER` — **obligatorio**, y en producción nunca `fake`: `meta`, con las credenciales de la Cloud API. Sin esta variable el despliegue falla al arrancar, a propósito.
- `DATABASE_URL` — Postgres (por ejemplo Vercel Postgres o Neon, desde la pestaña Storage). SQLite no sirve en producción porque las funciones serverless no tienen disco persistente.
- `ALLOWED_HOSTS` — opcional; el dominio del deploy y el alias de producción se confían automáticamente vía `VERCEL_URL` y `VERCEL_PROJECT_PRODUCTION_URL`, agrega aquí solo dominios propios (custom domains).
- `PUBLIC_BASE_URL` — el **único** origen público, `https://` + el dominio de producción (hoy `https://mvp-crm-lake.vercel.app`). Es lo que va en el link de imagen que se le entrega a WhatsApp en una respuesta rápida con foto: Meta lo descarga desde sus servidores, sin sesión, y todos los alias del proyecto salvo el dominio de producción están detrás del SSO de Vercel — un link a cualquiera de ellos hace que el envío falle unos segundos después de aceptado. Por defecto sale de `VERCEL_PROJECT_PRODUCTION_URL`; `manage.py check` avisa si queda vacío (`core.W002`) o apunta a un alias protegido (`core.W003`), y cada build imprime el valor resuelto.

Las migraciones corren solas en cada deploy de producción ([vercel_build.sh](vercel_build.sh)); un build de preview las salta porque comparte la base de producción. Si un `migrate` falla, falla el build, y no se despliega código que consulte tablas que la base no tiene.

Los archivos estáticos (`static/`) se recolectan y sirven automáticamente desde el CDN de Vercel — no requiere WhiteNoise ni configuración adicional. Los uploads de usuario (fotos de respuestas rápidas, cabeceras de plantillas, imágenes que llegan por WhatsApp) no pueden ir al filesystem de las funciones, que es de solo lectura: con un Blob store conectado (Storage → Blob; inyecta `BLOB_READ_WRITE_TOKEN`) van a Vercel Blob, y sin él van a la propia base de datos y se sirven desde `/archivos/<token>/…` ([core/storage.py](core/storage.py)). Conectar Blob después no rompe lo ya guardado.

En Vercel el proveedor que funciona tal cual es `meta`; `fake` no es una opción de producción — simula los envíos y no manda nada a ningún teléfono.

### Seguridad del webhook

La URL del webhook nombra al proveedor (`/webhooks/messaging/<proveedor>/`) para que, durante una migración entre proveedores, cada callback se siga interpretando con el proveedor que lo envió aunque el activo ya sea otro. Dos consecuencias que conviene tener presentes:

- El endpoint del proveedor `fake` **solo responde donde `MESSAGING_PROVIDER=fake`**. En un despliegue real devuelve 404: sin ese candado sería una forma anónima de escribir clientes inventados en la base de datos de producción, indistinguibles después de los reales.
- Cada proveedor real *sí* sigue siendo alcanzable siempre, así que su secreto es lo único que lo protege. Ninguno tiene valor por defecto: `META_APP_SECRET` y `MESSAGING_FAKE_SECRET` rechazan todo mientras estén vacíos. Un secreto escrito en el repositorio no protege nada.

## Tests

```powershell
python manage.py test
```

Cada sección tiene su propio archivo de tests en [core/](core/) (`tests.py`, `tests_crm.py`, `tests_embudos.py`, etc.); la capa de mensajería (idempotencia del webhook, rechazo de firmas, ventana de 24h) se prueba en [messaging/tests.py](messaging/tests.py).

## Qué contiene cada carpeta

Un *panel* es la parte de la pantalla que cambia al elegir una opción del menú.

| Carpeta | Qué contiene |
|---|---|
| [config/](config/) | Configuración de Django: ajustes, URLs y arranque. |
| [core/](core/) | La app principal: vistas, modelos y lógica de cada sección. |
| [core/management/](core/management/) | Contenedor de los comandos de consola de core. |
| [core/management/commands/](core/management/commands/) | Comandos: crear_maestro, hashear_clave y reset_usuarios. |
| [core/migrations/](core/migrations/) | Cambios de la base de datos de core. |
| [core/templatetags/](core/templatetags/) | Filtros propios que usan las plantillas HTML. |
| [docs/](docs/) | La documentación en PDF. |
| [docs/generar/](docs/generar/) | Contenido y scripts que arman el PDF. |
| [docs/generar/capturas/](docs/generar/capturas/) | Capturas de pantalla y posición de los recuadros rojos. |
| [messaging/](messaging/) | Mensajería: conversaciones, mensajes, webhook y costos de Meta. |
| [messaging/management/](messaging/management/) | Contenedor de los comandos de consola de mensajería. |
| [messaging/management/commands/](messaging/management/commands/) | Comandos: go_live, meta_spend, reset_conversations y sync_template_status. |
| [messaging/migrations/](messaging/migrations/) | Cambios de la base de datos de mensajería. |
| [messaging/providers/](messaging/providers/) | Conexión con WhatsApp: Meta y el simulador fake. |
| [static/](static/) | Archivos que el navegador descarga tal cual. |
| [static/css/](static/css/) | Estilos de cada sección. |
| [static/js/](static/js/) | JavaScript propio: navegación, calendario y gráficas. |
| [static/js/vendor/](static/js/vendor/) | Librerías externas: htmx, FullCalendar y ECharts. |
| [templates/](templates/) | HTML base de la app y pantalla de inicio de sesión. |
| [templates/icons/](templates/icons/) | Iconos SVG de la interfaz. |
| [templates/icons/brands/](templates/icons/brands/) | Logos de los canales: WhatsApp, Instagram, Messenger, Facebook y TikTok. |
| [templates/icons/flags/](templates/icons/flags/) | Banderas de país para los teléfonos. |
| [templates/illustrations/](templates/illustrations/) | Ilustraciones de las pantallas vacías. |
| [templates/legal/](templates/legal/) | Páginas públicas de privacidad y eliminación de datos. |
| [templates/partials/](templates/partials/) | Piezas HTML compartidas: barra lateral, menús y pie. |
| [templates/partials/automatizaciones/](templates/partials/automatizaciones/) | Piezas de la sección Automatizaciones. |
| [templates/partials/automatizaciones/panels/](templates/partials/automatizaciones/panels/) | Paneles de Automatizaciones. |
| [templates/partials/comercio/](templates/partials/comercio/) | Piezas de la sección Mi comercio. |
| [templates/partials/comercio/panels/](templates/partials/comercio/panels/) | Paneles de Mi comercio: productos, crear e importar. |
| [templates/partials/crm/](templates/partials/crm/) | Piezas de clientes, eventos y etiquetas. |
| [templates/partials/crm/panels/](templates/partials/crm/panels/) | Paneles del CRM: clientes, calendario, listas y usuarios. |
| [templates/partials/crm/usuarios/](templates/partials/crm/usuarios/) | Tabla y formulario de usuarios. |
| [templates/partials/embudos/](templates/partials/embudos/) | Piezas de la sección Embudos. |
| [templates/partials/embudos/panels/](templates/partials/embudos/panels/) | Paneles de Embudos. |
| [templates/partials/estadisticas/](templates/partials/estadisticas/) | Piezas de la sección Estadísticas. |
| [templates/partials/estadisticas/cards/](templates/partials/estadisticas/cards/) | Detalle de cada tarjeta: volumen y tiempos de respuesta. |
| [templates/partials/estadisticas/panels/](templates/partials/estadisticas/panels/) | Paneles de Estadísticas. |
| [templates/partials/inbox/](templates/partials/inbox/) | Piezas del Inbox: lista, chat, ficha, Nuevo chat y respuestas rápidas. |
| [templates/partials/mensajeria/](templates/partials/mensajeria/) | Piezas de Configuración de mensajería. |
| [templates/partials/mensajeria/panels/](templates/partials/mensajeria/panels/) | Paneles de mensajería: plantillas, bienvenida y asignación. |
| [templates/partials/mensajeria/respuestas/](templates/partials/mensajeria/respuestas/) | Tabla y formulario de respuestas rápidas. |
| [templates/partials/plantillas/](templates/partials/plantillas/) | Vista previa de una plantilla de WhatsApp. |
| [templates/partials/tags/](templates/partials/tags/) | Selector y etiquetas de colores. |
| [templates/sections/](templates/sections/) | La pantalla completa de cada sección. |
