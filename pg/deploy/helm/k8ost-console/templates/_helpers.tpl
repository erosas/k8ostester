{{- define "k8ost-console.fullname" -}}
{{- default (printf "%s-k8ost-console" .Release.Name) .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "k8ost-console.sa" -}}
{{- default (include "k8ost-console.fullname" .) .Values.serviceAccount.name -}}
{{- end -}}

{{- define "k8ost-console.image" -}}
{{- printf "%s:%s" .Values.image.repository (.Values.image.tag | default .Chart.AppVersion) -}}
{{- end -}}

{{- define "k8ost-console.labels" -}}
app.kubernetes.io/name: k8ost-console
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "k8ost-console.selectorLabels" -}}
app.kubernetes.io/name: k8ost-console
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* The console's operate/read permissions — its blast radius. Shared by the
     namespaced Role and the cluster-wide ClusterRole. */}}
{{- define "k8ost-console.rules" -}}
- apiGroups: ["postgresql.cnpg.io"]
  resources: ["clusters"]
  verbs: ["get", "list", "watch", "patch", "create"]   # patch = upgrade/rotate/expand; create = PITR restore
- apiGroups: ["postgresql.cnpg.io"]
  resources: ["backups"]
  verbs: ["get", "list", "create"]
- apiGroups: ["postgresql.cnpg.io"]
  resources: ["scheduledbackups", "poolers"]
  verbs: ["get", "list"]
- apiGroups: [""]
  resources: ["pods"]
  verbs: ["get", "list", "watch", "delete", "patch"]   # topology + kill-pod; patch = label for the partition fault
- apiGroups: [""]
  resources: ["pods/exec"]
  verbs: ["get", "create"]                             # psql/df/vacuum via the API exec stream
- apiGroups: [""]
  resources: ["services"]
  verbs: ["get", "list"]
- apiGroups: [""]
  resources: ["secrets"]
  verbs: ["get", "patch"]                              # connect info + rotation
- apiGroups: [""]
  resources: ["persistentvolumeclaims"]
  verbs: ["get"]                                       # expand-storage expandability check
- apiGroups: ["storage.k8s.io"]
  resources: ["storageclasses"]
  verbs: ["get", "list"]                               # ""
- apiGroups: ["networking.k8s.io"]
  resources: ["networkpolicies"]
  verbs: ["get", "create", "delete"]                  # partition fault
{{- end -}}

{{/* Additional create rights for the Builder's Deploy (the "lab" grant). */}}
{{- define "k8ost-console.labRules" -}}
- apiGroups: ["postgresql.cnpg.io"]
  resources: ["clusters", "poolers", "scheduledbackups"]
  verbs: ["create", "update", "patch", "delete"]
- apiGroups: [""]
  resources: ["secrets", "configmaps", "serviceaccounts"]
  verbs: ["create", "update", "patch"]
- apiGroups: ["apps"]
  resources: ["deployments"]
  verbs: ["create", "update", "patch"]
- apiGroups: ["rbac.authorization.k8s.io"]
  resources: ["roles", "rolebindings"]
  verbs: ["create", "update", "patch"]
- apiGroups: ["monitoring.coreos.com"]
  resources: ["prometheusrules", "podmonitors"]
  verbs: ["create", "update", "patch"]
{{- end -}}
