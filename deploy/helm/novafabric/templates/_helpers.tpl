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

{{/*
ADR-0230 D3 — refuse to render an unsafe combination silently.

`serve.insecure` disables the non-loopback bind refusal, so the dashboard is
served over plain HTTP guarded only by a shared token that holds `admin` scope
under ADR-0228 — full, irreversible power over signed evidence, including
DELETE /api/runs/{id} and POST /api/compliance/pii/erase.

This fails at RENDER time on purpose. NOTES.txt prints after a successful
install, which is after the exposure already exists, and nobody reads it twice.
A render-time failure is the only feedback that arrives before the risk does.

The acknowledgement is a fixed awkward string rather than a boolean: the unsafe
path stays possible but effortful, and `true` is far too easy to set by reflex.
*/}}
{{- define "novafabric.assertSecurePosture" -}}
{{- $ack := default "" .Values.serve.acknowledgeInsecureExposure -}}
{{- $expected := "i-accept-serving-evidence-over-plain-http" -}}
{{- if .Values.serve.insecure -}}
{{- if ne $ack $expected -}}
{{- fail (printf "\n\nRefusing to render: serve.insecure=true exposes the NovaFabric dashboard over plain HTTP,\nguarded only by a shared token that carries `admin` scope (ADR-0228) and therefore full,\nirreversible power over signed evidence -- DELETE /api/runs/{id}, POST /api/compliance/pii/erase,\nPOST /api/seal/{id}/bypass, POST /api/admin/roles.\n\nThis combination matches none of ADR-0042's four named, tested and supported deployment tiers.\n\nTwo ways to proceed:\n\n  1. RECOMMENDED -- terminate TLS at the ingress and leave serve.insecure=false.\n     Or use the default mode: server (`nova server start`), which has OIDC/RBAC.\n\n  2. Accept the exposure explicitly, recording the choice in your own values file:\n\n       serve:\n         insecure: true\n         acknowledgeInsecureExposure: %s\n\nSee ADR-0230 and deploy/helm/novafabric/README.md." $expected) -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/*
ADR-0230 D5 — the default flip must not be silent on upgrade.

Before v0.102.0 the chart shipped `mode: dashboard` + `serve.insecure: true`. An
operator upgrading with a values file that never mentioned either now silently
gets a different component on a different port. Changing what a deployment runs
without saying so is not acceptable even when the new behaviour is safer, so a
values file that pins neither key is asked to state its intent once.

`upgradeAcknowledged` exists only to be set once during that upgrade; a fresh
install is unaffected because a fresh install has no previous behaviour to lose.
*/}}
{{- define "novafabric.assertUpgradeAcknowledged" -}}
{{- if and .Release.IsUpgrade (not .Values.upgradeAcknowledged) -}}
{{- if not (hasKey .Values "modePinnedByOperator") -}}
{{- fail "\n\nRefusing to upgrade: the chart defaults changed in v0.102.0 (ADR-0230).\n\n  mode:            dashboard  ->  server      (nova server start, with OIDC/RBAC)\n  serve.insecure:  true       ->  false       (no non-loopback plain-HTTP bind)\n\nThe old pair matched none of ADR-0042's supported deployment tiers: it served a\ndashboard capable of irreversible evidence operations over plain HTTP, behind one\nshared token. It was also documented as \"read-only\", which it has not been since v0.8.\n\nIf you relied on the old defaults, restore them explicitly:\n\n  mode: dashboard\n  serve:\n    insecure: true\n    acknowledgeInsecureExposure: i-accept-serving-evidence-over-plain-http\n  upgradeAcknowledged: true\n\nIf you want the new, safer defaults, confirm you have read this:\n\n  upgradeAcknowledged: true\n\nSee CHANGELOG.md and ADR-0230." -}}
{{- end -}}
{{- end -}}
{{- end -}}
