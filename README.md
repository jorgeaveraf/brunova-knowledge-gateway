# Brunova Knowledge Gateway

Gateway seguro para agentes de Brunova.

Responsabilidad:

- exponer capacidades controladas para agentes;
- aplicar políticas antes de interactuar con sistemas externos;
- centralizar adaptadores hacia sistemas empresariales.

Capacidades empresariales:

- Google Workspace.
- HubSpot CRM mediante el Remote MCP oficial, detrás del gobierno Brunova.
- n8n mediante su MCP, con acceso completo a toda capability expuesta por n8n.

Arquitectura v0.26.0:

Agents
↓
MCP Server / HTTP API
↓
Gateway Authentication y Principal Resolution
↓
Scoped Authorization y MCP Tool Filtering
↓
Knowledge Gateway / Source Registry y Policies
↓
Adapters
↓
External Systems

Para HubSpot, el último tramo es:

```text
Management Agent → Brunova MCP → Governed HubSpot Adapter
                 → HubSpot Remote MCP → HubSpot CRM
```

Las propuestas pendientes siguen una rama separada y no autoritativa:

```text
Source Discovery → Source Proposal Store (YAML versionado en Cloud Storage)
                                      ↓
                              Management/Human review
                                      ↓
                         cambio versionado de sources.yaml
```

## Google Workspace (v0.9)

El adapter usa Application Default Credentials de Cloud Run y firma remota con
IAM Credentials para crear credenciales delegadas, sin archivos de claves ni
secretos. Los scopes OAuth coinciden con la configuración DWD existente. Las
lecturas siguen gobernadas por sus políticas actuales y las únicas mutaciones
expuestas son operaciones semánticas, source-scoped y autorizadas mediante
capabilities.

Variables no secretas requeridas:

- `WORKSPACE_DELEGATED_USER` (por ejemplo, `brunova@brunova.mx`)
- `WORKSPACE_SERVICE_ACCOUNT_EMAIL` (la identidad de runtime de Cloud Run)
- `WORKSPACE_DOC_MAX_CHARS` (límite de texto devuelto por documento)
- `WORKSPACE_SHEET_MAX_CELLS` (máximo de celdas por rango solicitado)
- `WORKSPACE_SOURCE_REGISTRY_PATH` (por defecto, `app/config/sources.yaml`)
- `WORKSPACE_BLOCKED_SOURCE_IDS` (recursos, carpetas o drives bloqueados)
- `WORKSPACE_SOURCE_MAX_DEPTH` (profundidad máxima para resolver ancestros)
- `WORKSPACE_AUDIT_ENABLED` (`true` o `false`)
- `SOURCE_PROPOSAL_BUCKET` (bucket dedicado de Cloud Storage)
- `SOURCE_PROPOSAL_OBJECT` (por defecto, `source_proposals.yaml`)
- `MCP_ALLOWED_HOSTS` (hosts exactos permitidos para Streamable HTTP)
- `MCP_ALLOWED_ORIGINS` (opcional; solo para clientes MCP en navegador)

La variable secreta `BRUNOVA_GATEWAY_TOKEN` también es obligatoria en runtime,
pero no forma parte de la configuración versionada. Debe inyectarse desde Secret
Manager como se describe en la sección de autenticación.

La cuenta de runtime necesita `iam.serviceAccounts.signBlob` sobre sí misma
(normalmente mediante `roles/iam.serviceAccountTokenCreator`). En Google
Workspace Domain Wide Delegation, su Client ID debe tener autorizados:

- `https://www.googleapis.com/auth/drive`
- `https://www.googleapis.com/auth/documents`
- `https://www.googleapis.com/auth/spreadsheets`

Para consultar el historial operacional, la misma identidad de runtime utiliza
ADC sin delegación de Workspace y necesita `roles/logging.viewer` en el
proyecto. Esta autorización permite al adapter consultar Cloud Logging; el
consumidor MCP nunca recibe acceso directo a logs.

Endpoints:

- `GET /sources`: lista metadata no sensible de todas las fuentes registradas.
- `GET /sources/{source_id}`: devuelve metadata no sensible de una fuente.
- `GET /sources/{source_id}/files?limit=10`: resuelve una fuente explícita,
  aplica `SourceAccessPolicy` y devuelve exclusivamente archivos de esa fuente.
- `GET /sources/{source_id}/docs/{document_id}`: valida que el documento
  pertenezca a la fuente indicada antes de leerlo.
- `GET /sources/{source_id}/sheets/{spreadsheet_id}?range=A1:F10`: valida
  pertenencia y `ContentReadPolicy` antes de leer el rango.
- `GET /sources/discover?limit=25`: detecta candidatos de Shared Drives y
  carpetas raíz, excluyendo ubicaciones registradas o bloqueadas. Solo propone.
- `GET /workspace/status`: valida autenticación y acceso mediante una consulta
  mínima a Drive.
- `GET /workspace/drive/list?limit=10`: devuelve hasta 100 archivos con `id`,
  `name`, `type` y metadata semántica de la fuente; no descarga contenido. El
  `id` permite solicitar el contenido autorizado en los endpoints de Docs y
  Sheets. Se conserva temporalmente por compatibilidad y está marcado como
  deprecado en OpenAPI; las integraciones nuevas deben seleccionar `source_id`.
- `GET /workspace/docs/{document_id}`: devuelve metadata y texto con truncamiento
  controlado por `WORKSPACE_DOC_MAX_CHARS`, además de la fuente y su
  clasificación. Se conserva temporalmente y está deprecado.
- `GET /workspace/sheets/{spreadsheet_id}?range=A1:F10`: devuelve únicamente el
  rango A1 acotado solicitado. El parámetro `range` es obligatorio y la política
  rechaza rangos abiertos o mayores que `WORKSPACE_SHEET_MAX_CELLS`. La
  respuesta incluye la fuente y su clasificación. Se conserva temporalmente y
  está deprecado.

La lectura de metadata pasa por `DriveReadPolicy`; la lectura de contenido pasa
por `ContentReadPolicy`. Las mutaciones no se exponen como endpoints REST ni
como APIs crudas de Google: solo existen como tools MCP gobernados. No existe
modificación de ownership, publicación pública ni administración arbitraria de
permisos.

## Source Registry y clasificación

`app/config/sources.yaml` es la fuente versionada de ubicaciones autorizadas. La
versión 1 requiere estos campos para cada entrada:

- `id`: identificador semántico único en `snake_case`;
- `name`: nombre legible de la fuente;
- `system`: actualmente solo `google_workspace`;
- `location_type`: `folder` o `shared_drive`;
- `location_id`: ID real de la carpeta o Shared Drive;
- `classification`: una de `management_only`, `internal_delivery`,
  `client_shareable` o `public`;
- `owner`: lista no vacía de responsables;
- `status`: `active` o `disabled`;
- `source_type`: `knowledge_source` o `archive_destination`;
- `capabilities`: matriz explícita `read`, `create`, `update`, `move`, `delete`
  `share` y `convert`. Las mutaciones son `false` por defecto.

Para registrar una fuente nueva, se agrega una entrada completa con un `id` y
un `location_id` únicos, se elige una de las cuatro clasificaciones y se valida
la suite de tests antes de desplegar. El registro no contiene secretos. Los
owners permanecen en configuración y no se exponen en endpoints ni auditoría.
Las respuestas de `GET /sources`, `GET /sources/{source_id}` y `list_sources`
sí incluyen la matriz booleana `capabilities`, que representa el contrato
operacional aprobado del Gateway. No representa ACLs, permisos ni roles de
Google Workspace y no expone `location_id`, owners o configuración de seguridad.

El consumo explícito sigue este flujo:

```text
source_id
    ↓
SourceRegistry
    ↓
SourceDefinition
    ↓
SourceAccessPolicy
    ↓
GoogleWorkspaceAdapter
```

El adapter recibe una fuente ya resuelta y autorizada. No consulta el YAML ni
descubre ubicaciones por su cuenta. El registry sigue siendo administrado de
forma humana y versionada; Google Workspace nunca modifica `sources.yaml`.

`ClassificationPolicy` convierte una fuente activa en contexto semántico.
`SourceAccessPolicy` usa el registro para autorizar la ubicación antes de que el
adapter lea contenido. La lectura también requiere `capabilities.read`. Una
fuente `disabled` se rechaza. La clasificación aporta
contexto de gobierno: no concede acceso a clientes, no publica, no comparte y no
implementa RBAC.

Las lecturas source-scoped agregan una segunda comprobación: el recurso debe
tener como ancestro la carpeta registrada o pertenecer al Shared Drive
seleccionado. El acceso técnico de Google no es suficiente. Un mismatch devuelve
`resource_not_in_source` antes de solicitar contenido a Docs o Sheets.

## Gobierno de acceso

`SourceAccessPolicy` falla cerrada cuando no hay fuentes activas. El endpoint de
Drive consulta únicamente las ubicaciones activas del registro. Para Docs y
Sheets, el gateway recupera primero metadata mínima de Drive y recorre sus
ancestros hasta `WORKSPACE_SOURCE_MAX_DEPTH`; solo después de autorizar la
ubicación solicita contenido. Los IDs bloqueados tienen precedencia sobre el
registro.

Cada request acepta `X-Correlation-ID` (caracteres seguros, máximo 128) o genera
un UUID. El ID se devuelve en el mismo header y en respuestas Workspace. Los
eventos de auditoría se emiten como JSON de una sola línea con timestamp,
servicio, actor, usuario delegado, acción, tipo e ID de recurso, resultado,
status HTTP, request ID, `source_id`, clasificación y código de error cuando
aplica. `correlation_id` conserva explícitamente el mismo identificador seguro
del request para consultas operacionales. `source_classification` es el campo explícito de v0.6 y
`classification` se conserva por compatibilidad con consumidores v0.5. Nunca
incluyen owners, texto de Docs, valores de Sheets, tokens, credenciales ni
scopes.

## Source Discovery

El modelo distingue tres conceptos:

- **Source Registry:** fuentes aprobadas, clasificadas y versionadas en
  `sources.yaml`.
- **Source Discovery:** Shared Drives y carpetas principales que existen pero
  todavía no están registradas.
- **Documents:** artefactos contenidos dentro de una fuente aprobada; no son
  candidatos a fuente por sí mismos.

`SourceDiscovery` consulta de forma read-only Shared Drives visibles y carpetas
raíz del usuario delegado. No hace crawling profundo, búsqueda por nombre de
documentos ni lectura de contenido. Genera `CandidateSource` y
`SourceProposal`: la propuesta incluye clasificación conservadora, confianza y
razones para que un agente la compare contra conocimiento canónico antes de una
decisión humana.

Las respuestas públicas no contienen IDs de ubicación, IDs propuestos,
permisos, configuración interna, documentos ni contenido. Discovery nunca crea
entradas, cambia clasificaciones ni modifica `sources.yaml`: propone y el
registry se aprueba únicamente mediante un cambio humano versionado.

## Source Governance Support

Cada candidato expone un `candidate_id` opaco y estable, derivado de su identidad
de ubicación sin revelar el ID de Drive. El ID permite que un agente solicite
detalles seguros mediante `get_source_candidate_details` sin que el Gateway
mantenga estado de discovery entre requests. El detalle vuelve a ejecutar una
consulta raíz acotada y devuelve nombre, tipo, clasificación sugerida, confianza
y razones; nunca devuelve usuarios, permisos, documentos o contenido.

`create_source_proposal` genera una intención inmutable con `proposal_id`,
identificador opaco del candidato, nombre sugerido, tipo de ubicación,
clasificación, confianza, razones, timestamp, request ID y el único estado
permitido: `pending_review`. La intención se persiste en un documento YAML de
Cloud Storage y la respuesta pública conserva el recibo mínimo con
`proposal_id`, estado y request ID.

El archivo `app/config/source_proposals.yaml` documenta y valida el esquema
vacío. En producción no se escribe el filesystem efímero de Cloud Run: el store
usa ADC con la misma Service Account del runtime para leer y actualizar el
objeto configurado. Cada escritura lleva una precondición de generación para no
sobrescribir cambios concurrentes y el bucket mantiene Object Versioning. La
Service Account solo necesita `roles/storage.objectUser` en ese bucket.

`list_source_proposals` devuelve únicamente resúmenes pendientes y
`get_source_proposal` expone el detalle seguro necesario para revisión. Ninguna
respuesta contiene IDs internos de Drive, permisos, usuarios, documentos,
contenido o el request ID persistido.

Crear una propuesta no aprueba, aplica ni agrega una fuente. No existe estado
`approved` o `applied`, ni operación que modifique `sources.yaml`. El flujo es:

```text
Discovery → Candidate → Pending proposal → Human approval → Versioned registry change
```

La interpretación, comparación contra conocimiento canónico y aprobación
pertenecen al Management Agent y a la persona responsable, no al Gateway.

Configuración inicial recomendada del store:

```bash
gcloud storage buckets create gs://<bucket-dedicado> \
  --project=brunova-ai-platform \
  --location=us-central1 \
  --uniform-bucket-level-access

gcloud storage buckets update gs://<bucket-dedicado> --versioning

gcloud storage buckets add-iam-policy-binding gs://<bucket-dedicado> \
  --member=serviceAccount:brunova-knowledge-agent@brunova-ai-platform.iam.gserviceaccount.com \
  --role=roles/storage.objectUser
```

El objeto puede iniciar con `version: 1` y `proposals: []`. No contiene secretos
ni modifica el Source Registry.

## Governed Source Operations

`ContentMutationPolicy` es independiente de las políticas de lectura. Una
mutación de management requiere simultáneamente:

- `source_id` presente en el Source Registry versionado;
- fuente activa y no bloqueada;
- capability exacta habilitada (`create`, `update`, `move`, `delete` o `share`);
- `approval_reference` externa, con formato seguro;
- pertenencia source-scoped del artefacto y, para move, del destino.

Para un principal developer, la autoridad delegada es la intersección de su
provider, fuente asignada, capability del principal y capability de la fuente;
no requiere `approval_reference`. Una referencia recibida nunca amplía ese
scope. Management conserva el requisito externo anterior.

La presencia en `sources.yaml` representa la aprobación de la fuente. El
Gateway no interpreta ni aprueba la referencia de decisión: solo exige su
presencia y la conserva en auditoría.

Las operaciones soportadas están deliberadamente acotadas:

- crear un Google Doc o Google Sheet nativo vacío en la raíz de la fuente;
- anexar hasta 4,000 caracteres mediante la operación legacy;
- resolver artefactos por nombre/ruta exactos a referencias opacas source-bound;
- copiar Google Docs o Sheets y renombrar artifacts mediante capabilities y aprobación;
- inspeccionar, editar y validar estructura documental de forma semántica;
- inspeccionar, editar y validar Google Sheets mediante operaciones semánticas
  acotadas;
- mover un Google Doc entre carpetas pertenecientes a la misma fuente;
- enviar un artefacto source-scoped a la papelera recuperable de Drive;
- conceder acceso `reader` a una dirección de correo explícita sobre un
  artefacto source-scoped, sin crear enlaces públicos ni enviar notificación.

El root registrado de una fuente no puede eliminarse ni compartirse como si
fuera un artefacto contenido.

No hay `batchUpdate` crudo expuesto al consumidor, eliminación permanente,
ownership, publicación pública ni modificación
automática del Source Registry. El
contenido enviado en `change` nunca se registra en auditoría. La audiencia
normalizada de share se conserva en la auditoría interna, pero se excluye de
`get_operation_history`.

## MCP

El endpoint Streamable HTTP está montado en `/mcp` con MCP Python SDK 2.x,
respuestas JSON, modo stateless para clientes legacy y protección contra DNS
rebinding mediante `MCP_ALLOWED_HOSTS`. La autenticación de consumidores se
aplica en el middleware del Gateway antes de llegar al transporte MCP.

Tools disponibles:

- `list_sources`: metadata no sensible del registry;
- `get_operation_history`: historial seguro de `create_source_artifact`,
  `update_source_artifact`, `move_source_artifact`, `delete_source_artifact`,
  `share_source_artifact`, `convert_source_artifact`, producción documental y
  mutaciones de tabs, filtrable por fuente u operación y limitado a 50 resultados;
- `discover_source_candidates`: propone Shared Drives y carpetas raíz no
  registradas, sin ejecutar cambios;
- `get_source_candidate_details`: devuelve detalle seguro de un candidato por
  su identificador opaco;
- `create_source_proposal`: persiste una intención `pending_review` auditable,
  sin aprobar ni aplicar cambios;
- `list_source_proposals`: lista resúmenes seguros del registro durable;
- `get_source_proposal`: devuelve el detalle seguro de una propuesta pendiente;
- `create_source_artifact`: crea un Google Doc o Google Sheet nativo en una fuente
  con capability `create` y la autorización correspondiente al principal;
- `update_source_artifact`: anexa texto acotado a un Doc source-scoped con
  capability `update`;
- `move_source_artifact`: mueve artefactos nativos u Office dentro de la misma
  fuente o a un `archive_destination` aprobado, con capability `move`;
- `convert_source_artifact`: importa XLSX/XLSM como Google Sheet, DOCX como
  Google Doc y PPTX como Google Slides mediante conversión nativa de Drive;
  acepta `artifact_ref` y devuelve referencias opacas para encadenar el flujo;
- `resolve_source_artifact`: consulta el estado actual del corpus Drive de la
  fuente, resuelve recursivamente nombre exacto o logical path y devuelve un
  handle cifrado, autenticado y ligado a esa fuente; no usa índice de artefactos;
- `copy_source_artifact`: copia un Google Doc o Google Sheet nativo sin modificar
  el original, preservando estructura y formato mediante Drive copy;
- `rename_source_artifact`: renombra un artefacto source-scoped;
- `inspect_document_structure`: devuelve revisión, tabs, índices, párrafos,
  headings, listas, tablas/celdas, headers, footers, imágenes, page setup y
  placeholders mediante un contrato acotado; cada tab se identifica con una
  referencia opaca ligada al documento, nunca con su ID interno de Google;
- `inspect_document_tab`: devuelve estructura, segmentos y placeholders de una
  sola tab seleccionada mediante `tab_ref`;
- `create_document_tab`: crea una tab con capability `update`, aprobación y
  `required_revision_id`;
- `rename_document_tab`: renombra una tab source-scoped sin exponer su ID;
- `delete_document_tab`: elimina únicamente una tab hoja y nunca la última tab
  del documento;
- `edit_source_document`: ejecuta únicamente operaciones semánticas allowlisted
  de texto, estilos, listas, tablas y headers/footers con
  `WriteControl.requiredRevisionId`; en documentos multi-tab exige `tab_ref` y
  mantiene las operaciones dentro de esa tab;
- `validate_document_structure`: quality gate read-only para headings,
  placeholders, tablas, header/footer, contenido mínimo, residuos Markdown y
  revisión esperada, con requisitos por tab y paridad estructural entre pares;
- `inspect_spreadsheet_structure`: devuelve título, locale/timezone y metadata
  acotada de tabs y grids; los IDs internos se sustituyen por `sheet_ref` opacos;
- `edit_source_spreadsheet`: acepta operaciones semánticas allowlisted para
  valores, append, clear, tabs, filas/columnas y formato básico;
- `validate_spreadsheet_structure`: verifica tabs, dimensiones, headers,
  rangos requeridos, celdas no vacías y presencia opcional de fórmulas;
- `delete_source_artifact`: envía un artefacto autorizado a la papelera de
  Drive con capability `delete`;
- `share_source_artifact`: concede acceso `reader` a una audiencia de correo
  explícita con capability `share`;
- `inspect_source_artifacts`: identifica metadata segura de artefactos nativos
  y Office dentro de una fuente aprobada y con lectura habilitada;
- `list_source_documents`: documentos autorizados de una fuente, con filtro
  opcional por nombre;
- `retrieve_document`: lectura source-scoped de Google Docs;
- `retrieve_sheet_range`: lectura source-scoped y acotada de Google Sheets.

Los tools llaman exclusivamente operaciones de `app/knowledge.py`; no acceden a
Google APIs directamente. El `request_id` de MCP se usa como correlation ID y
cada tool emite la misma auditoría estructurada de HTTP. Los errores de policy se
propagan como tool errors legibles. Solo los tools con capability de mutación y
autorización de management o scope developer escriben en Google Workspace;
ninguno modifica el Source Registry. Las tres
herramientas de proposals solo persisten o consultan intenciones pendientes.

## Office Artifact Awareness

`inspect_source_artifacts` lista como máximo 100 artefactos reconocidos de la
fuente seleccionada. Distingue `native_artifact` para Google Docs, Sheets y
Slides, y `office_artifact` para XLSX, XLSM, DOCX y PPTX. La respuesta contiene
solo `name`, `type`, `mime_type`, `extension`, `size`, `modified_time` y
`source_id`; no contiene IDs de Drive, contenido, propietarios, permisos ni
usuarios.

La extensión se deriva de un MIME type Office conocido, no del nombre del
archivo. La inspección por sí misma no modifica nada. La conversión y el
archivado existen como tools separados y solo se ejecutan con capability y la
autorización aplicable al tipo de principal.

## Artifact lifecycle

`convert_source_artifact` descarga el binario Office de forma transitoria y lo
vuelve a cargar indicando el MIME type Google-native. Drive realiza la
importación; el Gateway no interpreta celdas, no reconstruye documentos y no
reemplaza el original. La operación devuelve metadata del artefacto original y
del nuevo artefacto nativo.

Un registro con `source_type: archive_destination` puede recibir artefactos
mediante `move_source_artifact(destination_source_id=...)`, pero se excluye de
listados, retrieval y autorización implícita de conocimiento. No hay destinos
de archivo inferidos: deben existir de forma explícita y versionada en el Source
Registry, estar activos y habilitar `move`.

Los XLSM se importan como Google Sheets nativos, pero las macros VBA no deben
considerarse ejecutables ni preservadas funcionalmente. Después de convertir
XLSX/XLSM, el `artifact_ref` resultante puede encadenarse directamente con
inspección, lectura por rango, edición, validación, rename y move.

## Structured Spreadsheet Production

La producción de Sheets exige un `artifact_ref` source-bound y MIME nativo
`application/vnd.google-apps.spreadsheet`. XLSX/XLSM deben convertirse antes de
editarse. Todos los rangos deben ser A1 explícitos y cerrados; no se aceptan
rangos completos como `A:A`, filas abiertas ni whole-sheet writes implícitos.

`set_values` y `append_rows` usan `RAW` por defecto. `USER_ENTERED` debe pedirse
explícitamente para interpretar fórmulas como `=SUM(A1:A10)`. El formato básico
incluye bold, italic, font size, alineación horizontal, number format y colores
de fondo/texto. No se soportan todavía charts, pivots, conditional formatting,
slicers, protected ranges, named ranges ni data validation avanzada.

Los límites actuales son 50 operaciones por request, como máximo
`WORKSPACE_SHEET_MAX_CELLS` celdas escritas o afectadas por un rango, 1,000
filas/columnas por operación y 10 altas/renames/bajas de tabs por request. El
payload nunca acepta requests crudos de Sheets API y auditoría no registra
valores ni fórmulas.

Google Sheets API v4 no ofrece en esta superficie una precondición práctica
equivalente a `WriteControl.requiredRevisionId` de Docs. Por eso inspección y
edición declaran `concurrency_control: none`: se valida toda la lista antes de
llamar al provider, pero una falla durante una secuencia que mezcla Values API
y `batchUpdate` puede dejar aplicadas operaciones anteriores.

## Structured Document Production

La producción documental v1 reutiliza artefactos aprobados sin exponer IDs de
Drive. `resolve_source_artifact` emite un `artifact_ref` cifrado y autenticado,
ligado al `source_id`; una referencia manipulada o usada con otra fuente falla
cerrada. La clave se deriva con separación criptográfica del token del Gateway,
por lo que no existe otro secreto, base de datos ni estado de referencias.
Rotar `BRUNOVA_GATEWAY_TOKEN` invalida referencias emitidas anteriormente.

`copy_source_artifact`, `rename_source_artifact` y `edit_source_document`
requieren las capabilities `create` o `update` según la operación, además de
aprobación externa para management o scope delegado para developer. Cada
edición acepta entre 1 y 50 operaciones tipadas: insertar,
eliminar o reemplazar texto; estilos de párrafo y texto; listas; tablas,
filas/columnas/celdas y estilos básicos de celda; creación y edición indexada de
headers/footers. El cliente nunca puede enviar requests arbitrarios de Google
Docs. Debe inspeccionar primero y reenviar el `revision_id`; una revisión
obsoleta produce `document_revision_conflict` sin aplicar el batch.

El quality gate es de solo lectura y no constituye aprobación. Verifica
estructura esperada sin devolver permisos, credenciales, contenido completo ni
IDs internos. Copiar un Google Doc conserva la topología nativa del original;
convertir DOCX crea un artefacto Google-native nuevo y conserva intacto el
Office original. La fidelidad concreta de una conversión sigue dependiendo del
importador nativo de Google Drive y debe confirmarse mediante inspección y
quality gate.

## Governed Google Docs Tab Operations

La producción documental v0.19 utiliza referencias `tab_…` cifradas y
autenticadas, ligadas al `source_id` y al artefacto. `inspect_document_structure`
devuelve título, orden, parent opaco, nivel, párrafos y tablas de cada tab. Un
handle manipulado, usado con otra fuente o aplicado a otro documento falla
cerrado.

`create_document_tab`, `rename_document_tab` y `delete_document_tab` requieren
capability `update`, Approval Reference y `required_revision_id`; se auditan como
mutaciones gobernadas. Delete rechaza la última tab y tabs con descendientes.
`edit_source_document` acepta un scope `tab_ref` y convierte internamente las
referencias opacas a los IDs que Google requiere. En un documento multi-tab no
permite operaciones sin scope ni operaciones que intenten salir del scope.

El quality gate acepta requisitos genéricos por título de tab y pares de paridad
estructural. Puede verificar tabs requeridas, headings y secciones esperadas,
Document Control mediante labels proporcionados por el consumidor, tablas,
header/footer, contenido mínimo, placeholders y Markdown residual. La paridad
compara niveles de headings, cantidad de listas y formas de tablas; no compara
significado, calidad lingüística ni equivalencia de traducción.

No existe traducción automática, decisión de idioma fuente, sincronización
autónoma ni lógica específica de BKOS dentro del Gateway. Para copiar contenido
o estructura entre tabs, el Management Agent combina inspección tab-scoped y
ediciones semánticas gobernadas; el Gateway no ofrece un clon semántico que
pueda inventar o alterar contenido.

## Historial operacional

`get_operation_history` consulta los eventos estructurados del propio Gateway
mediante un adapter read-only y ADC de Cloud Run. No ofrece acceso general a
Cloud Logging ni una ruta administrativa paralela. La consulta acepta:

- `source_id` opcional, que debe existir y continuar autorizado;
- `operation` opcional, restringida a las mutaciones gobernadas conocidas;
- `limit`, entre 1 y 50, con valor predeterminado 10.

Ejemplo de resultado MCP:

```json
{
  "operations": [
    {
      "timestamp": "2026-08-22T19:53:06Z",
      "operation": "create_source_artifact",
      "source_id": "brunova_template",
      "result": "success",
      "approval_reference": "decision-reference",
      "request_id": "request-correlation-id",
      "correlation_id": "request-correlation-id"
    }
  "request_id": "history-query-request-id"
}
```

El modelo de salida usa una lista explícita de campos permitidos. Descarta IDs
de Drive, resource IDs, contenido, usuarios, delegated user, headers, tokens,
credenciales y cualquier otro campo del evento original. La consulta misma
requiere autenticación MCP, aplica el Source Registry y `SourceAccessPolicy`, y
genera su propio evento de auditoría.

## Autenticación del Gateway

Todas las capacidades HTTP y MCP están protegidas por defecto mediante:

```http
Authorization: Bearer <token administrado fuera del repositorio>
```

Solo `/health`, `/docs`, `/redoc`, `/openapi.json` y el callback exacto
`/auth/hubspot/callback` permanecen públicos. `POST /events/agent-signals` está
exento del bearer token del Gateway, pero aplica su propia autenticación OIDC de
Pub/Sub. Esta lista es explícita: cualquier endpoint futuro queda protegido
automáticamente.
El middleware autentica al consumidor antes de ejecutar handlers, policies,
adapters o tools MCP. La comparación del token es constante y el valor nunca se
registra en auditoría.

La credencial identifica acceso al Knowledge Gateway; no es una credencial de
Google Workspace. El consumidor no recibe credenciales de Service Account,
tokens Google, configuración DWD ni scopes OAuth. ADC, impersonation y llamadas
a Google continúan ocurriendo exclusivamente dentro del Gateway.

Una solicitud sin header obtiene `401` con `missing_authentication`; una
credencial incorrecta obtiene `401` con `invalid_authentication`. Si el secreto
no está configurado, el Gateway falla cerrado con `503`. Los eventos de
autenticación incluyen resultado, tipo de consumidor y correlation ID, pero no
headers ni credenciales.

### Inyección con Secret Manager

Crear el secreto y agregar su valor mediante entrada segura, sin colocarlo en
el comando ni en archivos locales:

```bash
gcloud secrets create brunova-gateway-token \
  --project=brunova-ai-platform \
  --replication-policy=automatic

gcloud secrets versions add brunova-gateway-token \
  --project=brunova-ai-platform \
  --data-file=-
```

Después, asociarlo directamente con Cloud Run:

```bash
gcloud run services update brunova-knowledge-gateway \
  --project=brunova-ai-platform \
  --region=us-central1 \
  --update-secrets=BRUNOVA_GATEWAY_TOKEN=brunova-gateway-token:latest
```

La identidad de runtime debe tener `roles/secretmanager.secretAccessor` sobre
ese secreto. La rotación se realiza agregando una nueva versión y desplegando
una revisión nueva; el valor no pasa por Git ni por la imagen Docker.

### Principals developers y acceso acotado

El token legacy conserva exactamente el acceso gobernado de management. Un
developer usa un token individual diferente, resuelto a un principal con:

- estado `active` o `revoked` y expiración opcional;
- providers explícitos (`workspace`, `hubspot`, `n8n`, `openwa`);
- `source_id` asignados explícitamente;
- capabilities `read`, `create`, `update`, `move`, `delete`, `share` y
  `convert`.

La autorización efectiva es siempre la intersección entre principal, source y
requisito de la tool. La clasificación de una source nunca asigna acceso. En
v1, las tools de discovery, propuestas, historial global y gobierno son solo
management. `tools/list` se filtra por principal y `tools/call` vuelve a validar
la misma política. Las referencias opacas continúan ligadas al `source_id`, y
el source vuelve a validarse contra el principal antes de ejecutar el adapter.

Los perfiles se leen en cada request desde un archivo JSON montado desde Secret
Manager. El archivo contiene únicamente hashes SHA-256 de tokens generados con
alta entropía; nunca tokens en texto plano. Su esquema es:

```json
{
  "version": 1,
  "principals": [
    {
      "id": "dev_example",
      "type": "developer",
      "status": "active",
      "token_sha256": "<64 caracteres hexadecimales>",
      "providers": {"workspace": true, "hubspot": false, "n8n": false, "openwa": false},
      "sources": ["<source_id exacto del registry>"],
      "capabilities": {
        "read": true, "create": true, "update": true, "move": true,
        "delete": false, "share": false, "convert": false
      },
      "metadata": {"purpose": "delivery"}
    }
  ]
}
```

Montar el secreto como archivo permite que una nueva versión `latest` se
refleje sin rotar el token management ni reconstruir la imagen:

```bash
gcloud run services update brunova-knowledge-gateway \
  --project=brunova-ai-platform \
  --region=us-central1 \
  --update-secrets=/secrets/principals/registry.json=brunova-principals:latest \
  --update-env-vars=BRUNOVA_PRINCIPALS_FILE=/secrets/principals/registry.json
```

Para revocar una identidad, cambiar solo su `status` a `revoked` y agregar una
nueva versión de `brunova-principals`. El middleware relee el archivo en cada
request. Las respuestas de un token revocado o expirado usan el error externo
seguro `invalid_authentication`; auditoría conserva la causa interna.

El token individual debe generarse con un CSPRNG, guardarse en un secreto
separado accesible al operador y al developer previsto, y convertirse a hash
antes de crear el registro. No se imprime durante deploy ni se guarda en Git,
logs, variables de imagen o respuestas del Gateway. El operador autorizado lo
recupera directamente desde ese secreto y configura cualquier cliente MCP con:

```text
URL: https://<gateway-host>/mcp/
Header: Authorization: Bearer <developer-token>
```

El cliente no necesita Obsidian ni recibe credenciales de Google, HubSpot o
n8n. Los eventos de auditoría agregan `principal_id` y `principal_type` a cada
operación, además de provider, source, tool, resultado y correlation ID cuando
aplican; nunca incluyen el bearer token o el payload documental.

## HubSpot Remote MCP

El Gateway es el único cliente de `https://mcp.hubspot.com`. Los Management
Agents conservan una sola conexión MCP con Brunova y nunca reciben el client
secret, access token ni refresh token de HubSpot.

Variables no secretas:

- `HUBSPOT_MCP_CLIENT_ID`: Client ID del MCP Auth App.
- `HUBSPOT_MCP_APP_ID`: ID informativo del app, cuando aplique.
- `HUBSPOT_MCP_SERVER_URL`: `https://mcp.hubspot.com`.
- `HUBSPOT_MCP_REDIRECT_URI`: callback HTTPS registrado en HubSpot.
- `HUBSPOT_OAUTH_STATE_BUCKET`: bucket existente de estado del Gateway.
- `HUBSPOT_OAUTH_STATE_PREFIX`: por defecto `oauth/hubspot`.
- `HUBSPOT_OAUTH_STATE_TTL_SECONDS`: por defecto 600.

`HUBSPOT_MCP_CLIENT_SECRET` es secreto. Debe almacenarse en Google Secret
Manager e inyectarse directamente como variable de Cloud Run. Nunca se agrega
a `.env.example`, salvo con valor vacío, ni se incorpora en la imagen.

El flujo operativo es:

1. Un operador autenticado abre `GET /auth/hubspot/connect`.
2. El Gateway crea state single-use y PKCE S256, los guarda temporalmente en
   `oauth/hubspot/pending/` y redirige a HubSpot.
3. HubSpot regresa exclusivamente a `GET /auth/hubspot/callback`, que valida y
   consume el state antes de intercambiar el code.
4. El refresh token se cifra antes de persistirse en
   `oauth/hubspot/connection.json`; el access token queda solo en memoria.
5. `GET /auth/hubspot/status`, protegido por el token Brunova, devuelve solo
   estado y metadata segura de cuenta.

Cada refresh toma un lease mediante `if_generation_match`. La rotación escribe
el refresh token nuevo con una segunda precondición de generación, evitando que
dos instancias de Cloud Run consuman simultáneamente el mismo token single-use.
Un refresh inválido marca la conexión como `reauthorization_required`.

El catálogo downstream se consulta mediante `hubspot_list_tools`; cada tool se
clasifica como `read`, `mutation` o `unknown`. Las lecturas explícitamente
permitidas se exponen con prefijo `hubspot_`. `hubspot_manage_crm_objects`
requiere `explicit_intent=true` y un `approval_reference` externo. Las tools
desconocidas fallan cerradas hasta ser clasificadas en código y tests. Auditoría
registra provider, tool, clasificación, resultado, correlation ID y approval
cuando aplica, sin incluir payload CRM completo ni credenciales.

La autorización humana inicial se realiza después del deploy: abrir
`/auth/hubspot/connect` con `Authorization: Bearer <token Brunova>`, seleccionar
la cuenta, revisar permisos y autorizar. Después consultar
`/auth/hubspot/status`, ejecutar `hubspot_get_user_details`, listar el catálogo y
hacer una consulta read-only controlada. La primera validación no debe ejecutar
`hubspot_manage_crm_objects`.

### Configuración de un cliente MCP

El cliente apunta al endpoint Streamable HTTP y obtiene el token desde su propio
secret store o environment seguro:

```json
{
  "url": "https://<gateway-host>/mcp/",
  "headers": {
    "Authorization": "Bearer ${BRUNOVA_GATEWAY_TOKEN}"
  }
}
```

`BRUNOVA_GATEWAY_TOKEN` no debe guardarse en el repositorio, archivos de
configuración versionados, código del agente ni logs. Cloud Run puede permitir
la invocación a nivel de infraestructura porque la aplicación aplica esta capa
de autenticación antes de toda capacidad; esto evita requerir una identidad GCP
independiente para cada consumidor.

El SDK MCP 2.x requiere Python 3.10 o posterior. La imagen de Cloud Run usa
Python 3.12.

## n8n MCP full access

El Gateway usa el SDK MCP oficial como cliente downstream. La configuración
completa se inyecta en `N8N_MCP_JSON` desde un único secreto de Google Secret
Manager; producción no lee `.env`. `N8N_DISCOVERY_TTL_SECONDS` (60 por defecto)
y `N8N_TIMEOUT_SECONDS` (30 por defecto) son configuración no sensible.

Discovery conserva nombre, descripción, input schema y metadata segura. Cada
tool se proyecta como `n8n_<downstream_tool_name>`; si ese nombre colisiona con
una tool nativa se usa `n8n_downstream_<downstream_tool_name>`. `n8n_list_tools`
fuerza discovery actual y `n8n_status` expone únicamente estado, conteo y
versiones de protocolo/servidor cuando están disponibles.

No existe allowlist, clasificación read/mutation, activation flag ni policy
engine n8n en el Gateway. Todo lo que n8n MCP exponga queda disponible. El TTL
permite altas y bajas sin deploy; un error de discovery vacía el catálogo n8n
sin afectar `/health`, Workspace, HubSpot o las tools nativas.

Auditoría registra provider `n8n`, tool downstream, resultado, request y
correlation ID, `approval_reference` recibido como metadata MCP y duración. No
registra argumentos, respuestas completas, URL, headers ni credenciales.

Configuración productiva:

```bash
gcloud secrets create brunova-n8n-mcp-json \
  --project=brunova-ai-platform --replication-policy=automatic
gcloud secrets versions add brunova-n8n-mcp-json \
  --project=brunova-ai-platform --data-file=-
gcloud run services update brunova-knowledge-gateway \
  --project=brunova-ai-platform --region=us-central1 \
  --update-secrets=N8N_MCP_JSON=brunova-n8n-mcp-json:latest \
  --update-env-vars=N8N_DISCOVERY_TTL_SECONDS=60,N8N_TIMEOUT_SECONDS=30
```

Las APIs de Drive, Docs, Sheets, IAM Service Account Credentials y Cloud Logging
deben estar habilitadas en el proyecto de GCP.

## OpenWA MCP

El Gateway se conecta directamente al MCP curado de OpenWA mediante Streamable
HTTP. `OPENWA_MCP_URL` contiene el endpoint completo (en producción,
`https://wa.brunova.mx/mcp`); `OPENWA_APIKEY` se inyecta desde Secret Manager.
`OPENWA_DISCOVERY_TTL_SECONDS` y `OPENWA_TIMEOUT_SECONDS` controlan discovery y
timeouts sin acoplar el adapter a un host concreto.

Cada tool descubierta se proyecta como `openwa_<downstream_tool_name>`. El
catálogo conserva nombre, descripción, input schema, anotaciones MCP y metadata
segura. `openwa_list_tools` fuerza discovery y `openwa_status` devuelve solo
conectividad, inicialización, conteos read/write y versiones seguras. Una caída
de OpenWA no afecta Workspace, HubSpot, n8n ni `/health`.

Las anotaciones downstream son la clasificación autoritativa. Las tools con
`readOnlyHint=true` se ejecutan como lecturas. Cualquier otra tool se trata como
escritura y requiere una `approval_reference` externa válida en metadata MCP;
esa referencia se audita, pero los argumentos, cuerpos de mensajes, media,
respuestas completas, URL y credenciales no se registran. El Gateway no añade
un proxy REST ni una segunda allowlist de operaciones OpenWA.

Solo management recibe OpenWA en v4. Los principals developer mantienen
`openwa: false` por defecto; `tools/list` no muestra ninguna tool OpenWA y
`tools/call` revalida el provider antes de hacer discovery o una llamada
downstream.

Configuración productiva:

```bash
gcloud secrets create brunova-openwa-mcp-api-key \
  --project=brunova-ai-platform --replication-policy=automatic
gcloud secrets versions add brunova-openwa-mcp-api-key \
  --project=brunova-ai-platform --data-file=-
gcloud run services update brunova-knowledge-gateway \
  --project=brunova-ai-platform --region=us-central1 \
  --update-secrets=OPENWA_APIKEY=brunova-openwa-mcp-api-key:latest \
  --update-env-vars=OPENWA_MCP_URL=https://wa.brunova.mx/mcp,OPENWA_DISCOVERY_TTL_SECONDS=60,OPENWA_TIMEOUT_SECONDS=30
```

## Agent Signal Inbox v1

`POST /events/agent-signals` es el consumer exclusivo del envelope push real de
Pub/Sub. Valida el ID token de Google contra `AGENT_SIGNAL_PUSH_AUDIENCE`, exige
issuer Google, `email_verified=true` y coincidencia exacta con
`AGENT_SIGNAL_PUSH_SERVICE_ACCOUNT`. No acepta `BRUNOVA_GATEWAY_TOKEN` como
autenticación alternativa.

Cada `signal_id` se guarda como un JSON independiente bajo
`gs://$AGENT_SIGNAL_BUCKET/$AGENT_SIGNAL_PREFIX<signal_id>.json`. La creación usa
`if_generation_match=0`, por lo que una redelivery conserva un solo item. Cada
cambio de estado usa la generación leída como precondición optimista. Los
estados y transiciones permitidas son:

```text
pending -> claimed -> completed
   |          |  \
   |          |   -> pending (release)
   |          -> dismissed
   -> dismissed
```

Una claim dura 30 minutos por defecto; al leer, listar o reclamar, una claim
expirada vuelve a `pending`. `completed` y `dismissed` se eliminan después de 30
días mediante cleanup lazy durante list/status. `pending` y `claimed` se
conservan. Ambos periodos son configurables sin crear scheduler o lock service.

El contrato común admite `signal_id`, `signal_type`, `priority`, `occurred_at`,
`source`, `reason`, `references` y `metadata`. La extensión allowlisted de
`whatsapp_attention_required` valida `contact`, `conversation` y un `preview`
máximo de 500 caracteres. `contact.hubspot_contact_id` es enriquecimiento
opcional: ausente, `null`, vacío o whitespace se normaliza a `null` y solo se
proyecta a `references` cuando contiene un identificador real. El Gateway no
recupera conversaciones, media ni CRM, y recibir una señal no ejecuta ninguna
tool downstream.

Las tools `list_agent_signals`, `get_agent_signal`, `claim_agent_signal`,
`complete_agent_signal`, `dismiss_agent_signal`, `release_agent_signal` y
`agent_signal_status` son exclusivamente management. El historial seguro se
consulta aparte mediante `get_agent_signal_operation_history`, sin mezclarlo con
el historial source-scoped de Workspace. Estas tools no aparecen en
`tools/list` de developer y una invocación directa vuelve a fallar cerrada. Los
eventos de auditoría contienen provider, operación, principal, `signal_id`,
`signal_type`, transición, resultado y correlation ID; nunca preview, teléfono,
chat ni body completo.

Los payloads permanentemente inválidos se auditan sin contenido y reciben 204
para evitar retry infinito. Un fallo de persistencia recibe 5xx, de modo que
Pub/Sub redelivere. Una DLQ puede agregarse en un incremento posterior.

Configuración no secreta esperada:

```bash
AGENT_SIGNAL_BUCKET=brunova-ai-platform-36958632716-knowledge-gateway-state
AGENT_SIGNAL_PREFIX=agent-signals/items/
AGENT_SIGNAL_CLAIM_LEASE_SECONDS=1800
AGENT_SIGNAL_COMPLETED_RETENTION_DAYS=30
AGENT_SIGNAL_PUSH_AUDIENCE=https://<gateway-host>/events/agent-signals
AGENT_SIGNAL_PUSH_SERVICE_ACCOUNT=brunova-signal-push@brunova-ai-platform.iam.gserviceaccount.com
```

La configuración gobernada de la subscription existente (sin crear una
segunda) es conceptualmente:

```bash
gcloud pubsub subscriptions update brunova-management-agent-signals \
  --project=brunova-ai-platform \
  --push-endpoint="https://<gateway-host>/events/agent-signals" \
  --push-auth-service-account="brunova-signal-push@brunova-ai-platform.iam.gserviceaccount.com" \
  --push-auth-token-audience="https://<gateway-host>/events/agent-signals"
```

Antes de ejecutar ese cambio se debe comprobar backlog, retry, ack deadline,
retención e IAM live. El Inbox no implementa wake-up, daemon local,
notificaciones desktop ni polling de Codex.
