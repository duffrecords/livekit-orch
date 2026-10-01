{{/* Labels for an object of the component named by .name; . is (dict "name" … "root" $). */}}
{{- define "livekit-orch.labels" -}}
app: {{ .name }}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/part-of: livekit-orch
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .root.Chart.Name .root.Chart.Version }}
{{- end }}

{{/* In-cluster DNS name of the Service .name. */}}
{{- define "livekit-orch.host" -}}
{{ .name }}.{{ .root.Release.Namespace }}.svc.cluster.local
{{- end }}

{{/* Agent names, the default one first. */}}
{{- define "livekit-orch.agentNames" -}}
{{- $default := .Values.orchestrator.defaultAgent -}}
{{- if not (hasKey .Values.agents $default) -}}
{{- fail (printf "orchestrator.defaultAgent %q is not a key of agents" $default) -}}
{{- end -}}
{{- $names := list $default -}}
{{- range $name := keys .Values.agents | sortAlpha -}}
{{- if ne $name $default }}{{ $names = append $names $name }}{{ end -}}
{{- end -}}
{{- join "," $names -}}
{{- end }}

{{/* agent name = headless Service of its worker pods, for every agent. */}}
{{- define "livekit-orch.workerServices" -}}
{{- $pairs := list -}}
{{- range $name := keys .Values.agents | sortAlpha -}}
{{- $host := include "livekit-orch.host" (dict "name" (printf "agent-%s" $name) "root" $) -}}
{{- $pairs = append $pairs (printf "%s=%s:8081" $name $host) -}}
{{- end -}}
{{- join "," $pairs -}}
{{- end }}

{{/* LIVEKIT_API_KEY and LIVEKIT_API_SECRET from the existing Secret. */}}
{{- define "livekit-orch.keysEnv" -}}
- name: LIVEKIT_API_KEY
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secrets.livekitKeys }}
      key: LIVEKIT_API_KEY
- name: LIVEKIT_API_SECRET
  valueFrom:
    secretKeyRef:
      name: {{ .Values.secrets.livekitKeys }}
      key: LIVEKIT_API_SECRET
{{- end }}
