{{- define "batmon-ha.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "batmon-ha.fullname" -}}
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

{{- define "batmon-ha.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "batmon-ha.labels" -}}
helm.sh/chart: {{ include "batmon-ha.chart" . }}
{{ include "batmon-ha.selectorLabels" . }}
app.kubernetes.io/version: {{ .Values.image.tag | default .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{- define "batmon-ha.selectorLabels" -}}
app.kubernetes.io/name: {{ include "batmon-ha.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{- define "batmon-ha.pvcName" -}}
{{- .Values.persistence.existingClaim | default (printf "%s-data" (include "batmon-ha.fullname" .)) }}
{{- end }}
