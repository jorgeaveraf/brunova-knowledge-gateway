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

## Google Workspace (v0.4)

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
- `WORKSPACE_ALLOWED_SHARED_DRIVE_IDS` (IDs separados por comas)
- `WORKSPACE_ALLOWED_FOLDER_IDS` (IDs separados por comas)
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

- `GET /workspace/status`: valida autenticación y acceso mediante una consulta
  mínima a Drive.
- `GET /workspace/drive/list?limit=10`: devuelve hasta 100 archivos con solo
  `id`, `name` y `type`; no descarga contenido. El `id` permite solicitar el
  contenido autorizado en los endpoints de Docs y Sheets.
- `GET /workspace/docs/{document_id}`: devuelve metadata y texto con truncamiento
  controlado por `WORKSPACE_DOC_MAX_CHARS`.
- `GET /workspace/sheets/{spreadsheet_id}?range=A1:F10`: devuelve únicamente el
  rango A1 acotado solicitado. El parámetro `range` es obligatorio y la política
  rechaza rangos abiertos o mayores que `WORKSPACE_SHEET_MAX_CELLS`.

La lectura de metadata pasa por `DriveReadPolicy`; la lectura de contenido pasa
por `ContentReadPolicy`. No existen endpoints de escritura, edición, creación,
movimiento, borrado, permisos o compartición.

## Gobierno de acceso

`SourceAccessPolicy` falla cerrada cuando no hay carpetas ni Shared Drives
permitidos. `/workspace/drive/list` consulta únicamente esas ubicaciones. Para
Docs y Sheets, el gateway recupera primero metadata mínima de Drive y recorre
sus ancestros hasta `WORKSPACE_SOURCE_MAX_DEPTH`; solo después de autorizar la
ubicación solicita contenido. Los IDs bloqueados tienen precedencia sobre la
allowlist.

Cada request acepta `X-Correlation-ID` (caracteres seguros, máximo 128) o genera
un UUID. El ID se devuelve en el mismo header y en respuestas Workspace. Los
eventos de auditoría se emiten como JSON de una sola línea con timestamp,
servicio, actor, usuario delegado, acción, tipo e ID de recurso, resultado,
status HTTP, request ID y código de error cuando aplica. Nunca incluyen texto de
Docs, valores de Sheets, tokens, credenciales ni scopes.

Las APIs de Drive, Docs, Sheets e IAM Service Account Credentials deben estar
habilitadas en el proyecto de GCP.
