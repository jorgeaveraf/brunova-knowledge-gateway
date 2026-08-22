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

## Google Workspace (v0.2)

El adapter usa Application Default Credentials de Cloud Run y firma remota con
IAM Credentials para crear credenciales delegadas, sin archivos de claves ni
secretos. Aunque los scopes OAuth coinciden con la configuración DWD existente,
la integración actual expone exclusivamente operaciones de solo lectura y la
capa de políticas no permite mutaciones.

Variables no secretas requeridas:

- `WORKSPACE_DELEGATED_USER` (por ejemplo, `brunova@brunova.mx`)
- `WORKSPACE_SERVICE_ACCOUNT_EMAIL` (la identidad de runtime de Cloud Run)

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
  `name` y `type`; no descarga contenido.

Las APIs de Drive, Docs, Sheets e IAM Service Account Credentials deben estar
habilitadas en el proyecto de GCP.
