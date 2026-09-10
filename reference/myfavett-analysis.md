# Análisis local de myFaveTT 1.12.63

Inspección previa a modificar extension/, 2026-09-08. Referencia leída: manifest.json, b.js, c.js, s.js, rl.html, style.css y r.js. Código minificado en una línea: los símbolos siguientes permiten localizar cada conclusión con búsqueda literal. No se ejecutó la extensión propietaria ni se presupone que TikTok conserve hoy este contrato. Implementación Kurukin independiente.

## A. Sidebar

c.js actúa solamente en el frame superior de www.tiktok.com y las rutas /, /explore y /foryou. Crea aside#myfaveTT-container, agrega iframe#myfaveTT con name=sidebar y URL local rl.html; añade pushed-by-myfaveTT al body y antepone el aside. style.css define ancho 390px, fixed, top/left 0, height 100vh y z-index 1001. El body reserva espacio mediante border-left transparente; #app-header reduce su ancho con calc. El iframe ocupa la cuadrícula, sin borde. Hay overflow-x:hidden global.

rl.html carga r.js, que crea dos iframes remotos: UI y resync. Kurukin no reproduce esa UI remota. b.js abre una pestaña TikTok al pulsar el icono; no hace toggle. En s.js, io() cancela, elimina aside/clase y cierra canales/listeners. El comando UI tipo 3 llama io(). No hay toggle reutilizable equivalente al solicitado.

## B. Ejecución de página

c.js agrega script type=module cuyo src apunta a s.js por chrome.runtime.getURL. Ese script se ejecuta en el mundo de la página. s.js lee los globals de TikTok y ejecuta fetch; comunica con sidebar por postMessage. r.js retransmite entre iframe remoto, iframe local y página; BroadcastChannel conecta además b.js con r.js. Kurukin usa scripts declarativos MAIN y un puente con esquemas estrictos, sin interceptar fetch/XHR.

## C. Contexto real

En s.js, we.st() llena le (app), ue (biz), de (domains). App válido exige wid, region, language. Orden:

1. window.SIGI_STATE.AppContext.appContext.
2. JSON del script#SIGI_STATE, mismo camino.
3. window.__$UNIVERSAL_DATA$__.__DEFAULT_SCOPE__["webapp.app-context"], hasta tres intentos con 1000ms.
4. JSON de script#__UNIVERSAL_DATA_FOR_REHYDRATION__, asignado **directamente** a le; la referencia no desenvuelve __DEFAULT_SCOPE__ aquí.
5. Sí existe fetch relativo /node-webapp/api/common-app-context con credentials:include; exige statusCode===0 y asigna el JSON directamente.

Biz: SIGI_STATE.BizContext.bizContext (global/script), scope universal webapp.biz-context y JSON rehydration directo. Usa os como criterio. Domains: ue.domains, después script#api-domains; exige mTApi y rootApi. ke tiene alternativas v1/v2/v3, pero Se usa exclusivamente ke.v4(), constante https://www.tiktok.com. No deriva authorItems del dominio capturado.

Kurukin admite también el scope dentro del script de rehydration: adaptación explícita al contenedor, no afirmación sobre el extractor minificado. No inventa wid, región, idioma ni timestamps. OS y coverFormat conservan los fallbacks de referencia.

## D. Usuario y autor objetivo

we.lt representa al usuario **logueado**, desde le.user: uid, nickName, secUid, uniqueId. El flujo following obtiene autores desde /api/user/list/, transforma userList mediante $e (selecciona id, uniqueId, nickname, avatarThumb, privateAccount, secUid de t.user y videoCount de t.stats) y pasa el objeto autor a ii(t) / ni(t). ni usa t.secUid, no we.lt.secUid. La referencia no implementa un extractor genérico del perfil visitado: su sidebar se carga en rutas iniciales y procesa autores seguidos. El endpoint self (/api/user/detail/) consulta el uniqueId logueado, no resuelve un target arbitrario.

Kurukin toma username de /@username y busca coincidencia exacta de uniqueId (ignorando mayúsculas) en los datos públicos de perfil. Se conserva el principio correcto del extractor anterior: nunca usar el primer secUid encontrado. Se excluye app/user context de la búsqueda para evitar tratar al viewer como target. La sesión se comprueba con contexto explícito del viewer o controles públicos visibles; nunca con cookies ni con la existencia del usuario de perfil.

## E. authorItems: petición exacta

Se("authorItems") combina me() + ye() + parámetros específicos. ht añade count, ft cursor, vt secUid. bt serializa URL. Los valores null/undefined se serializan como cadena vacía; booleanos como true/false. No hay headers explícitos, firma generada ni X-Bogus en este constructor.

Endpoint fijo: https://www.tiktok.com/api/post/item_list/

| Common param | Valor/fuente de referencia |
| --- | --- |
| aid | "1988" |
| app_name | "tiktok_web" |
| browser_language | navigator.language |
| browser_name | navigator.appCodeName |
| browser_online | navigator.onLine |
| browser_platform | navigator.platform |
| browser_version | navigator.appVersion |
| channel | "tiktok_web" |
| cookie_enabled | navigator.cookieEnabled |
| device_platform | "web_pc" |
| focus_state | true, constante |
| history_len | window.history.length |
| is_fullscreen | matchMedia("(display-mode: fullscreen)").matches |
| is_page_visible | true, constante |
| referer | document.referrer |
| screen_height | screen.height |
| screen_width | screen.width |
| tz_name | Intl.DateTimeFormat().resolvedOptions().timeZone |
| verifyFp | captura de s_v_web_id mediante /s_v_web_id=(\\w+)/ en document.cookie |
| data_collection_enabled | true |
| user_is_login | true |
| clientABVersions | window universal: app-context.abTestVersion.versionName dividido por coma + Object.values(seo.abtest.parameters.clientABVersions) + seo.abtest.vidList; filtra falsy y une con coma |
| app_language | le.language |
| device_id | le.wid |
| os | ue.os; fallback "mac" si userAgent contiene Mac, en otro caso "windows" |
| priority_region | le.user.region |
| region | le.region |
| webcast_language | le.language |
| WebIdLastTime | le.webIdCreatedTime |
| odinId | le.odinId |

| Específico | Valor/fuente |
| --- | --- |
| language | le.language |
| from_page | "user" |
| coverFormat | ue.videoCoverSettings.format o 2 |
| enable_cache | false |
| video_encoding | "dash" |
| needPinnedItemIds | true |
| post_item_list_request_type | 0 |
| count | 16 en ni; 30 aparece solo en comparación De |
| cursor | "0" inicialmente; luego h.cursor |
| secUid | t.secUid del autor |

verifyFp se obtiene únicamente dentro de MAIN para la petición, jamás para detectar login. Es la única lectura de cookie de Kurukin; no se transporta el contenido de cookies. Los identificadores y query quedan en memoria de página y solo viajan al endpoint TikTok correspondiente, no a un servidor propio, UI, debug o almacenamiento.

### Paginación y respuesta exactas

ni(t), generador interno: cursor="0", hasMore=true; llama Un(7000) antes de cada petición y construye Se("authorItems").ht(16).ft(cursor).vt(t.secUid). Un usa Mn compartido inicializado con Date.now(): espera lo que falta para 7000ms desde el inicio anterior y actualiza Mn. No es necesariamente una pausa de 7s posterior a recibir cada respuesta. Espera también conexión online, pausa/cancelación y shouldScrollMore(). Fetch GET implícito con {credentials:"include"} en MAIN.

ce(response,"Fru804") comprueba HTTP, content-length, MIME, parsea JSON y devuelve {err,json}; consume itemList, statusCode, cursor, hasMore. Ce normaliza items, Le comprueba descargabilidad y Ne excluye imagePost. El generador entrega el lote y asigna cursor/hasMore. **hasMore se asigna pero no se usa como condición de salida** en ese bucle; termina si cursor es "0", "-1" o falsy, con una espera final de 10s. El generador exterior usa maxScroll (fallback 1000), contabiliza items y downstream descarga. No confundir este flujo con el scroll DOM alternativo aún presente en s.js.

Errores originales: Zn maneja respuesta vacía/bloqueada, captcha (HTTP 200 con contentLength 0), JSON con statusCode no cero e items defectuosos; puede reintentar, pausar o saltar autor. 429 espera 60s en los primeros 30 eventos y luego 200s. No se reproduce esa política extensa. Kurukin detiene 403/429/challenge/status, respeta hasMore=false, detecta cursores repetidos, limita a 200 resultados y solo admite dos retries transitorios (red/502/503/504), con 7000ms conservadores. El primer request puede salir inmediatamente; entre respuestas y nueva petición espera 7000ms completos, una política más lenta explícita.

## F. b.js y webRequest: trazado de dependencia

b.js onSendHeaders escucha GET a https://*.tiktok.com/api/post/item_list/*; ignora initiator chrome-extension, exige Accept y descarta Accept con csv. Publica tipo 13/direction 0 con headers completos, URL base y URL con params mediante BroadcastChannel myfaveTT. No modifica headers ni bloquea solicitudes.

r.js recibe ese canal y reenvía a parent. s.js ao(), direction 0/type 13, llama De(payload). De descarta peticiones cuyo secUid/cursor coinciden con el scan propio (re.R.J.q / re.R.V.G). Compara origin capturado con mTApi/rootApi, registra eventos y, en divergencias, envía mensaje 43 con officialUrl/myUrl/capturedHeaders. Su única memoria de dominio capturado es je._t, leída/escrita dentro de De; no interviene en Se, ke.v4 ni ni. El constructor de comparación usa secUid="foo" y count=30. La expresión posterior i.l||n.authorItemsCanBeFetched no altera esos valores.

Conclusión comprobada: webRequest sirve para observación/comparación/diagnóstico. authorItems no depende de él; Kurukin no pide webRequest ni captura headers. Tampoco se conserva la exportación diagnóstica extensa de la referencia.

## Validación de la reconstrucción

La suite incluye dos pruebas que leen s.js sin modificarlo y ejecutan únicamente su constructor de URL en una VM con contexto ficticio. Comparan los 40 parámetros serializados con Kurukin, tanto con contexto completo como con opcionales ausentes. Pasan ambas. No se incorpora código propietario a la implementación ni al ZIP. El resto de la suite verifica el contrato público y los límites de paginación propios; no equivale a una petición real aceptada por TikTok.
