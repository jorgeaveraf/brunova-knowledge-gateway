# Brunova Knowledge Gateway

Gateway seguro para agentes de Brunova.

Responsabilidad:

- exponer capacidades controladas para agentes;
- aplicar políticas antes de interactuar con sistemas externos;
- centralizar adaptadores hacia sistemas empresariales.

Primera capacidad prevista:

- Google Workspace.

Arquitectura:

Agents
↓
Knowledge Gateway
↓
External Systems

## Google Workspace (v0.6)

El adapter usa Application Default Credentials de Cloud Run y firma remota con
IAM Credentials para crear credenciales delegadas, sin archivos de claves ni
secretos. Aunque los scopes OAuth coinciden con la configuración DWD existente,
la integración actual expone exclusivamente operaciones de solo lectura y la
capa de políticas no permite mutaciones.

Variables no secretas requeridas:

- `WORKSPACE_DELEGATED_USER` (por ejemplo, `brunova@brunova.mx`)
- `WORKSPACE_SERVICE_ACCOUNT_EMAIL` (la identidad de runtime de Cloud Run)
- `WORKSPACE_DOC_MAX_CHARS` (límite de texto devuelto por documento)
- `WORKSPACE_SHEET_MAX_CELLS` (máximo de celdas por rango solicitado)
- `WORKSPACE_SOURCE_REGISTRY_PATH` (por defecto, `app/config/sources.yaml`)
- `WORKSPACE_BLOCKED_SOURCE_IDS` (recursos, carpetas o drives bloqueados)
- `WORKSPACE_SOURCE_MAX_DEPTH` (profundidad máxima para resolver ancestros)
- `WORKSPACE_AUDIT_ENABLED` (`true` o `false`)

La cuenta de runtime necesita `iam.serviceAccounts.signBlob` sobre sí misma
(normalmente mediante `roles/iam.serviceAccountTokenCreator`). En Google
Workspace Domain Wide Delegation, su Client ID debe tener autorizados:

- `https://www.googleapis.com/auth/drive`
- `https://www.googleapis.com/auth/documents`
- `https://www.googleapis.com/auth/spreadsheets`

Endpoints:

- `GET /sources`: lista metadata no sensible de todas las fuentes registradas.
- `GET /sources/{source_id}`: devuelve metadata no sensible de una fuente.
- `GET /sources/{source_id}/files?limit=10`: resuelve una fuente explícita,
  aplica `SourceAccessPolicy` y devuelve exclusivamente archivos de esa fuente.
- `GET /workspace/status`: valida autenticación y acceso mediante una consulta
  mínima a Drive.
- `GET /workspace/drive/list?limit=10`: devuelve hasta 100 archivos con `id`,
  `name`, `type` y metadata semántica de la fuente; no descarga contenido. El
  `id` permite solicitar el contenido autorizado en los endpoints de Docs y
  Sheets. Se conserva temporalmente por compatibilidad y está marcado como
  deprecado en OpenAPI; las integraciones nuevas deben seleccionar `source_id`.
- `GET /workspace/docs/{document_id}`: devuelve metadata y texto con truncamiento
  controlado por `WORKSPACE_DOC_MAX_CHARS`, además de la fuente y su
  clasificación.
- `GET /workspace/sheets/{spreadsheet_id}?range=A1:F10`: devuelve únicamente el
  rango A1 acotado solicitado. El parámetro `range` es obligatorio y la política
  rechaza rangos abiertos o mayores que `WORKSPACE_SHEET_MAX_CELLS`. La
  respuesta incluye la fuente y su clasificación.

La lectura de metadata pasa por `DriveReadPolicy`; la lectura de contenido pasa
por `ContentReadPolicy`. No existen endpoints de escritura, edición, creación,
movimiento, borrado, permisos o compartición.

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
- `status`: `active` o `disabled`.

Para registrar una fuente nueva, se agrega una entrada completa con un `id` y
un `location_id` únicos, se elige una de las cuatro clasificaciones y se valida
la suite de tests antes de desplegar. El registro no contiene secretos. Los
owners permanecen en configuración y no se exponen en endpoints ni auditoría.

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
adapter lea contenido. Una fuente `disabled` se rechaza. La clasificación aporta
contexto de gobierno: no concede acceso a clientes, no publica, no comparte y no
implementa RBAC.

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
aplica. `source_classification` es el campo explícito de v0.6 y
`classification` se conserva por compatibilidad con consumidores v0.5. Nunca
incluyen owners, texto de Docs, valores de Sheets, tokens, credenciales ni
scopes.

## Preparación para discovery

`app/source_discovery/interface.py` define únicamente contratos internos para
`CandidateSource`, `SourceProposal`, `DiscoveryResult` y `SourceDiscovery`. No
hay implementación conectada a Google Workspace, sincronización automática ni
mutación del registry en v0.6.

Las APIs de Drive, Docs, Sheets e IAM Service Account Credentials deben estar
habilitadas en el proyecto de GCP.
