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
