{{/* Chart name, overridable. */}}
{{- define "day2.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Release-qualified base name for every object in this chart. */}}
{{- define "day2.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "day2.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "day2.labels" -}}
helm.sh/chart: {{ include "day2.chart" . }}
app.kubernetes.io/name: {{ include "day2.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/part-of: day2-control-plane
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{/* Selector labels for one component. Usage: (dict "ctx" . "component" "api") */}}
{{- define "day2.selectorLabels" -}}
app.kubernetes.io/name: {{ include "day2.name" .ctx }}
app.kubernetes.io/instance: {{ .ctx.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "day2.componentLabels" -}}
{{ include "day2.labels" .ctx }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{/*
Image reference for an application service. A digest always wins over a tag, so a
released deployment is immutable (CLAUDE.md rule 2). Kind sets an empty registry
and side-loads images, which is why the registry segment is conditional.
Usage: (dict "ctx" . "svc" .Values.api)
*/}}
{{- define "day2.image" -}}
{{- $global := .ctx.Values.image -}}
{{- $svc := .svc.image -}}
{{- $repo := printf "%s/%s" $global.repository $svc.name -}}
{{- if $global.registry -}}
{{- $repo = printf "%s/%s" $global.registry $repo -}}
{{- end -}}
{{- if $svc.digest -}}
{{- printf "%s@%s" $repo $svc.digest -}}
{{- else -}}
{{- printf "%s:%s" $repo (default $global.tag $svc.tag) -}}
{{- end -}}
{{- end -}}

{{/* Postgres image, always digest-pinned since it comes from a public registry. */}}
{{- define "day2.postgresImage" -}}
{{- $img := .Values.postgres.image -}}
{{- if $img.digest -}}
{{- printf "%s@%s" $img.repository $img.digest -}}
{{- else -}}
{{- printf "%s:%s" $img.repository $img.tag -}}
{{- end -}}
{{- end -}}

{{- define "day2.postgres.fullname" -}}
{{- printf "%s-postgres" (include "day2.fullname" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Secret holding the Postgres password: either the caller's or the chart's. */}}
{{- define "day2.postgres.secretName" -}}
{{- if .Values.postgres.auth.existingSecret -}}
{{- .Values.postgres.auth.existingSecret -}}
{{- else -}}
{{- include "day2.postgres.fullname" . -}}
{{- end -}}
{{- end -}}

{{/*
SQLAlchemy DSN with the password left as a shell variable: the credential reaches
the process from a Secret via env, and is never rendered into a manifest.
*/}}
{{- define "day2.databaseUrlTemplate" -}}
{{- $auth := .Values.postgres.auth -}}
{{- printf "postgresql+asyncpg://%s:$(DAY2_POSTGRES_PASSWORD)@%s:%v/%s" $auth.username (include "day2.postgres.fullname" .) .Values.postgres.service.port $auth.database -}}
{{- end -}}

{{/* Password env var, shared by every service that talks to Postgres. */}}
{{- define "day2.postgresPasswordEnv" -}}
- name: DAY2_POSTGRES_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ include "day2.postgres.secretName" . }}
      key: {{ .Values.postgres.auth.secretPasswordKey }}
{{- end -}}
