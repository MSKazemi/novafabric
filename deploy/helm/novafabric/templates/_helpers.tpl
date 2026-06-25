{{/* Expand the name of the chart. */}}
{{- define "novafabric.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* Fully qualified app name. */}}
{{- define "novafabric.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{- define "novafabric.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "novafabric.labels" -}}
helm.sh/chart: {{ include "novafabric.chart" . }}
{{ include "novafabric.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "novafabric.selectorLabels" -}}
app.kubernetes.io/name: {{ include "novafabric.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "novafabric.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "novafabric.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/* Postgres service name (bundled instance). */}}
{{- define "novafabric.postgresName" -}}
{{- printf "%s-postgres" (include "novafabric.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/* Resolved database host: bundled Postgres service or external host. */}}
{{- define "novafabric.dbHost" -}}
{{- if .Values.postgres.enabled }}
{{- include "novafabric.postgresName" . }}
{{- else }}
{{- required "externalDatabase.host is required when postgres.enabled is false" .Values.externalDatabase.host }}
{{- end }}
{{- end }}

{{/* Name of the secret holding the DB password. */}}
{{- define "novafabric.dbSecretName" -}}
{{- if and (not .Values.postgres.enabled) .Values.externalDatabase.existingSecret }}
{{- .Values.externalDatabase.existingSecret }}
{{- else }}
{{- printf "%s-db" (include "novafabric.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- define "novafabric.dbSecretPasswordKey" -}}
{{- if and (not .Values.postgres.enabled) .Values.externalDatabase.existingSecret -}}
{{- .Values.externalDatabase.existingSecretPasswordKey -}}
{{- else -}}
password
{{- end -}}
{{- end -}}

{{/* Shared environment for the nova container and its migration init container. */}}
{{- define "novafabric.env" -}}
{{- $user := .Values.externalDatabase.username -}}
{{- $db := .Values.externalDatabase.database -}}
{{- $port := .Values.externalDatabase.port | toString -}}
{{- if .Values.postgres.enabled -}}
{{- $user = .Values.postgres.auth.username -}}
{{- $db = .Values.postgres.auth.database -}}
{{- $port = "5432" -}}
{{- end -}}
- name: PGPASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "novafabric.dbSecretName" . }}
      key: {{ include "novafabric.dbSecretPasswordKey" . }}
- name: PGHOST
  value: {{ include "novafabric.dbHost" . | quote }}
- name: PGPORT
  value: {{ $port | quote }}
- name: PGUSER
  value: {{ $user | quote }}
- name: PGDATABASE
  value: {{ $db | quote }}
- name: NOVAFABRIC_METADATA_BACKEND
  value: "postgres"
- name: NOVAFABRIC_SERVER_BACKEND
  value: "postgres"
- name: NOVAFABRIC_METADATA_DSN
  value: "postgresql://$(PGUSER):$(PGPASSWORD)@$(PGHOST):$(PGPORT)/$(PGDATABASE)"
- name: NOVAFABRIC_POSTGRES_DSN
  value: "postgresql://$(PGUSER):$(PGPASSWORD)@$(PGHOST):$(PGPORT)/$(PGDATABASE)"
- name: NOVAFABRIC_HOME
  value: "/data/nova"
- name: NOVAFABRIC_EVIDENCE_DIR
  value: "/data/capsules/evidence"
{{- with .Values.extraEnv }}
{{- toYaml . | nindent 0 }}
{{- end }}
{{- end }}
