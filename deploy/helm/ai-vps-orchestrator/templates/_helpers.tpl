{{- define "aivo.name" -}}{{ .Chart.Name | trunc 63 | trimSuffix "-" }}{{- end -}}
{{- define "aivo.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}{{ .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else -}}{{ printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" }}{{- end -}}
{{- end -}}
{{- define "aivo.labels" -}}
app.kubernetes.io/name: {{ include "aivo.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}
{{- define "aivo.selector" -}}
app.kubernetes.io/name: {{ include "aivo.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
{{- define "aivo.secretName" -}}{{ .Values.secrets.existingSecret | default (printf "%s-secrets" (include "aivo.fullname" .)) }}{{- end -}}
{{- define "aivo.backendImage" -}}{{ .Values.image.backend.repository }}:{{ .Values.image.backend.tag | default .Chart.AppVersion }}{{- end -}}
{{- define "aivo.frontendImage" -}}{{ .Values.image.frontend.repository }}:{{ .Values.image.frontend.tag | default .Chart.AppVersion }}{{- end -}}
{{- define "aivo.pvcName" -}}{{ .Values.media.persistence.existingClaim | default (printf "%s-media" (include "aivo.fullname" .)) }}{{- end -}}

{{/* Environment shared by every backend pod */}}
{{- define "aivo.backendEnv" -}}
- name: AIVO_PROVIDER
  value: {{ .Values.provider | quote }}
- name: AIVO_WORKER_IMAGE
  value: {{ .Values.workerImage | quote }}
- name: AIVO_MEDIA_DIR
  value: /var/lib/aivo/media
- name: AIVO_SCRATCH_DIR
  value: /scratch
- name: AIVO_SESSION_COOKIE_SECURE
  value: {{ ternary "true" "false" (or .Values.ingress.enabled .Values.httpRoute.enabled) | quote }}
{{- with .Values.spaces.region }}
- name: AIVO_SPACES_REGION
  value: {{ . | quote }}
{{- end }}
{{- with .Values.spaces.bucket }}
- name: AIVO_SPACES_BUCKET
  value: {{ . | quote }}
{{- end }}
{{- with .Values.spaces.endpoint }}
- name: AIVO_SPACES_ENDPOINT
  value: {{ . | quote }}
{{- end }}
{{- with .Values.secrets.adminEmail }}
- name: AIVO_ADMIN_EMAIL
  value: {{ . | quote }}
{{- end }}
{{- with .Values.secrets.mediaActiveKeyId }}
- name: AIVO_MEDIA_ACTIVE_KEY_ID
  value: {{ . | quote }}
{{- end }}
{{- if .Values.postgresql.enabled }}
- name: AIVO_DB_PASSWORD
  valueFrom: { secretKeyRef: { name: {{ include "aivo.fullname" . }}-postgres, key: password } }
- name: AIVO_DATABASE_URL
  value: "postgresql+asyncpg://aivo:$(AIVO_DB_PASSWORD)@{{ include "aivo.fullname" . }}-postgres:5432/aivo"
{{- else if .Values.externalDatabase.url }}
- name: AIVO_DATABASE_URL
  value: {{ .Values.externalDatabase.url | quote }}
{{- end }}
{{- $publicUrl := .Values.publicUrl | default (ternary (printf "https://%s" .Values.ingress.host) "" .Values.ingress.enabled) }}
{{- with $publicUrl }}
- name: AIVO_PUBLIC_URL
  value: {{ . | quote }}
{{- end }}
- name: AIVO_PASSWORD_LOGIN
  value: {{ .Values.passwordLogin | quote }}
{{- if .Values.sso.enabled }}
{{- if not (and .Values.sso.issuer .Values.sso.clientId) }}
{{- fail "sso.enabled requires sso.issuer and sso.clientId" }}
{{- end }}
- name: AIVO_OIDC_ISSUER
  value: {{ .Values.sso.issuer | quote }}
- name: AIVO_OIDC_CLIENT_ID
  value: {{ .Values.sso.clientId | quote }}
- name: AIVO_OIDC_NAME
  value: {{ .Values.sso.name | quote }}
- name: AIVO_OIDC_SCOPES
  value: {{ .Values.sso.scopes | quote }}
- name: AIVO_OIDC_AUTO_PROVISION
  value: {{ .Values.sso.autoProvision | quote }}
- name: AIVO_OIDC_LINK_BY_EMAIL
  value: {{ .Values.sso.linkByEmail | quote }}
- name: AIVO_OIDC_ALLOWED_DOMAINS
  value: {{ join "," .Values.sso.allowedDomains | quote }}
- name: AIVO_OIDC_ALLOWED_GROUPS
  value: {{ join "," .Values.sso.allowedGroups | quote }}
- name: AIVO_OIDC_ADMIN_GROUPS
  value: {{ join "," .Values.sso.adminGroups | quote }}
- name: AIVO_OIDC_GROUPS_CLAIM
  value: {{ .Values.sso.groupsClaim | quote }}
{{- end }}
{{- with .Values.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end -}}

{{- define "aivo.backendEnvFrom" -}}
- secretRef:
    name: {{ include "aivo.secretName" . }}
{{- end -}}

{{- define "aivo.backendVolumes" -}}
- name: media
  persistentVolumeClaim:
    claimName: {{ include "aivo.pvcName" . }}
- name: scratch
  emptyDir:
    medium: Memory
    sizeLimit: {{ .Values.scratch.sizeLimit }}
- name: tmp
  emptyDir: {}
{{- end -}}

{{- define "aivo.backendMounts" -}}
- name: media
  mountPath: /var/lib/aivo/media
- name: scratch
  mountPath: /scratch
- name: tmp
  mountPath: /tmp
{{- end -}}

{{- define "aivo.containerSecurity" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities: { drop: [ALL] }
{{- end -}}
