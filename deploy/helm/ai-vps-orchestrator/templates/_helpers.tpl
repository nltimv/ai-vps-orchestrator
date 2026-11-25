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
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}
{{- define "aivo.selector" -}}
app.kubernetes.io/name: {{ include "aivo.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
{{- define "aivo.secretName" -}}{{ .Values.secrets.existingSecret | default (printf "%s-secrets" (include "aivo.fullname" .)) }}{{- end -}}
{{- define "aivo.backendImage" -}}{{ .Values.image.backend.repository }}:{{ .Values.image.backend.tag | default .Chart.AppVersion }}{{- end -}}
{{- define "aivo.frontendImage" -}}{{ .Values.image.frontend.repository }}:{{ .Values.image.frontend.tag | default .Chart.AppVersion }}{{- end -}}
{{/* Worker image for GPU machines: pinned, or the same release as the backend. Empty for the development
     chart ("latest"): the backend then picks the worker of its own baked-in version. */}}
{{- define "aivo.workerImage" -}}
{{- if .Values.workerImage -}}{{ .Values.workerImage }}
{{- else if .Values.image.worker.tag -}}{{ .Values.image.worker.repository }}:{{ .Values.image.worker.tag }}
{{- else if ne .Chart.AppVersion "latest" -}}{{ .Values.image.worker.repository }}:{{ .Chart.AppVersion }}
{{- end -}}
{{- end -}}
{{- define "aivo.pvcName" -}}{{ .Values.media.persistence.existingClaim | default (printf "%s-media" (include "aivo.fullname" .)) }}{{- end -}}

{{/* Environment shared by every backend pod */}}
{{- define "aivo.backendEnv" -}}
- name: AIVO_PROVIDER
  value: {{ .Values.provider | quote }}
{{- with include "aivo.workerImage" . }}
- name: AIVO_WORKER_IMAGE
  value: {{ . | quote }}
{{- end }}
- name: AIVO_WORKER_IMAGE_REPOSITORY
  value: {{ .Values.image.worker.repository | quote }}
{{- with .Values.runpod }}
{{- with .countries }}
- name: AIVO_RUNPOD_COUNTRIES
  value: {{ join "," . | quote }}
{{- end }}
{{- with .dataCenters }}
- name: AIVO_RUNPOD_DATA_CENTERS
  value: {{ join "," . | quote }}
{{- end }}
{{- with .networkVolumeId }}
- name: AIVO_RUNPOD_NETWORK_VOLUME_ID
  value: {{ . | quote }}
{{- end }}
{{- with .globalVolumeId }}
- name: AIVO_RUNPOD_GLOBAL_VOLUME_ID
  value: {{ . | quote }}
{{- end }}
{{- with .gpuTypes }}
- name: AIVO_RUNPOD_GPU_TYPES
  value: {{ join "," . | quote }}
{{- end }}
- name: AIVO_RUNPOD_DISK_GB
  value: {{ .diskGb | quote }}
- name: AIVO_RUNPOD_REGION
  value: {{ .region | quote }}
{{- with .workerApiUrl }}
- name: AIVO_WORKER_API_URL
  value: {{ . | quote }}
{{- end }}
{{- end }}
- name: AIVO_MEDIA_DIR
  value: /var/lib/aivo/media
- name: AIVO_SCRATCH_DIR
  value: /scratch
- name: AIVO_SESSION_COOKIE_SECURE
  value: {{ ternary "true" "false" (or .Values.ingress.enabled .Values.httpRoute.enabled) | quote }}
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
