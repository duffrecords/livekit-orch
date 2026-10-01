CLUSTER     := livekit-orch
NAMESPACE   := livekit
RELEASE     := livekit-orch
CHART       := charts/livekit-orch
AGENT_IMAGE := livekit-orch/agent-echo:dev
VOICE_IMAGE := livekit-orch/agent-voice:dev
ORCH_IMAGE  := livekit-orch/orchestrator:dev
ROOM        ?= spike
IDENTITY    ?= user1
WEB_PORT    ?= 8000
# orchestrator API as seen from the host
API         ?= http://localhost:8080
AGENT       ?= echo
KEDA_VERSION := 2.21.0
# extra arguments for helm, e.g. HELM_ARGS="-f my-values.yaml"
HELM_ARGS   ?=

-include .env
export LIVEKIT_URL LIVEKIT_API_KEY LIVEKIT_API_SECRET

# The voice agent needs an external speech/LLM provider, so it is built and
# deployed only when .env holds a key for one. Without a key everything else
# works as before, with the keyless echo agent.
PROVIDER_KEYS := OPENAI_API_KEY|DEEPGRAM_API_KEY|ANTHROPIC_API_KEY
VOICE := $(shell grep -qsE '^($(PROVIDER_KEYS))=.+' .env && echo 1)

# Address LiveKit advertises to host clients: the host's primary LAN address,
# detected at deploy time (never committed). Falls back to loopback when there
# is no network; loopback works in Chrome but not Firefox. Override with
# `make deploy NODE_IP=...` or NODE_IP in .env.
NODE_IP ?= $(shell python3 -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('192.0.2.1', 9)); print(s.getsockname()[0])" 2>/dev/null || echo 127.0.0.1)

# Values only known on this machine. The local image IDs go into pod
# annotations, so an image rebuilt under the same tag rolls its pods on the
# next `make deploy`.
image_id = $(shell docker image inspect -f '{{.Id}}' $(1) 2>/dev/null)
HELM_SET = --set livekit.nodeIP=$(NODE_IP) \
	--set-string orchestrator.image.id=$(call image_id,$(ORCH_IMAGE)) \
	--set-string agents.echo.image.id=$(call image_id,$(AGENT_IMAGE))
ifeq ($(VOICE),1)
HELM_SET += --set agents.voice.enabled=true \
	--set-string agents.voice.image.id=$(call image_id,$(VOICE_IMAGE))
endif
HELM_SET += $(HELM_ARGS)

.PHONY: up down env cluster addons image namespace secret deploy voice-config manifests lint session sessions stop workers scale top load smoke test token dispatch web logs status

up: env cluster addons image secret deploy ## create the cluster and deploy everything

env: ## generate .env with random local LiveKit keys if it doesn't exist
	@test -f .env || { \
		printf 'LIVEKIT_API_KEY=local%s\nLIVEKIT_API_SECRET=%s\nLIVEKIT_URL=ws://localhost:7880\n' \
			"$$(openssl rand -hex 4)" "$$(openssl rand -hex 24)" > .env; \
		echo "generated .env"; }

cluster:
	@kind get clusters | grep -qx $(CLUSTER) || kind create cluster --config kind-config.yaml

# Pulls images from registry.k8s.io and ghcr.io, and the KEDA chart from
# kedacore.github.io (`--repo`, so nothing is added to the local Helm repo
# list). The metrics-server version is pinned in
# deploy/addons/metrics-server/kustomization.yaml.
addons: ## install metrics-server and KEDA
	kubectl apply -k deploy/addons/metrics-server
	helm upgrade --install keda keda --repo https://kedacore.github.io/charts --version $(KEDA_VERSION) \
		--namespace keda --create-namespace --wait --timeout 5m
	kubectl -n kube-system rollout status deploy/metrics-server --timeout=180s

image:
	docker build -t $(AGENT_IMAGE) agents/echo
	docker build -t $(ORCH_IMAGE) orchestrator
	kind load docker-image $(AGENT_IMAGE) $(ORCH_IMAGE) --name $(CLUSTER)
ifeq ($(VOICE),1)
	docker build -t $(VOICE_IMAGE) agents/voice
	kind load docker-image $(VOICE_IMAGE) --name $(CLUSTER)
endif

# re-invoke make so a freshly generated .env is picked up
secret:
	@$(MAKE) --no-print-directory _secret

_secret:
	@test -n "$(LIVEKIT_API_KEY)" -a -n "$(LIVEKIT_API_SECRET)" || { echo "LIVEKIT_API_KEY/SECRET not set in .env"; exit 1; }
	@$(MAKE) --no-print-directory namespace
	@kubectl -n $(NAMESPACE) create secret generic livekit-keys \
		--from-literal=LIVEKIT_API_KEY="$(LIVEKIT_API_KEY)" \
		--from-literal=LIVEKIT_API_SECRET="$(LIVEKIT_API_SECRET)" \
		--from-literal=LIVEKIT_KEYS="$(LIVEKIT_API_KEY): $(LIVEKIT_API_SECRET)" \
		--dry-run=client -o yaml | kubectl apply -f -

namespace:
	@kubectl create namespace $(NAMESPACE) --dry-run=client -o yaml | kubectl apply -f - >/dev/null

# The chart is the only source of the manifests (`make manifests` prints
# them). A changed NODE_IP (e.g. after switching networks) changes LiveKit's
# pod template, so re-running this restarts it.
deploy: namespace ## install or upgrade the Helm release
ifeq ($(VOICE),1)
	@$(MAKE) --no-print-directory voice-config
	helm upgrade --install $(RELEASE) $(CHART) --namespace $(NAMESPACE) $(HELM_SET) \
		--set-string agents.voice.configVersion="$$(kubectl -n $(NAMESPACE) get secret/provider-keys configmap/agent-voice-config \
			-o jsonpath='{range .items[*]}{.metadata.resourceVersion}-{end}')"
else
	helm upgrade --install $(RELEASE) $(CHART) --namespace $(NAMESPACE) $(HELM_SET)
	@kubectl -n $(NAMESPACE) delete secret/provider-keys configmap/agent-voice-config --ignore-not-found
endif
	kubectl -n $(NAMESPACE) rollout status deploy/livekit --timeout=120s
	kubectl -n $(NAMESPACE) rollout status deploy/agent-echo --timeout=180s
	kubectl -n $(NAMESPACE) rollout status deploy/orchestrator --timeout=120s
ifeq ($(VOICE),1)
	kubectl -n $(NAMESPACE) rollout status deploy/agent-voice --timeout=180s
endif

# Provider keys and VOICE_* settings go from .env into a Secret and a ConfigMap
# through stdin, so a key never appears on a command line or in the release's
# values. The workers read them at startup only: `deploy` passes the objects'
# versions to the chart, which rolls the workers when either has changed.
voice-config:
	@grep -E '^($(PROVIDER_KEYS))=.+' .env | kubectl -n $(NAMESPACE) create secret generic provider-keys \
		--from-env-file=/dev/stdin --dry-run=client -o yaml | kubectl apply -f - >/dev/null
	@grep -E '^VOICE_[A-Z_]+=.+' .env | kubectl -n $(NAMESPACE) create configmap agent-voice-config \
		--from-env-file=/dev/stdin --dry-run=client -o yaml | kubectl apply -f - >/dev/null

manifests: ## print the Kubernetes manifests the chart renders
	@helm template $(RELEASE) $(CHART) --namespace $(NAMESPACE) $(HELM_SET)

# 192.0.2.1 is a documentation address standing in for NODE_IP
lint: ## check the chart, with and without the voice agent
	helm lint $(CHART) --set livekit.nodeIP=192.0.2.1
	@helm template $(RELEASE) $(CHART) --set livekit.nodeIP=192.0.2.1 >/dev/null
	@helm template $(RELEASE) $(CHART) --set livekit.nodeIP=192.0.2.1 --set agents.voice.enabled=true >/dev/null

session: ## start a session with AGENT; prints its ID, URL and token
	@curl -fsS -X POST $(API)/sessions -H 'Content-Type: application/json' \
		-d '{"agent": "$(AGENT)", "identity": "$(IDENTITY)"}' | jq .

sessions: ## list live and recently ended sessions
	@curl -fsS '$(API)/sessions?include=ended' | jq .

stop: ## stop session ID
	@test -n "$(ID)" || { echo "usage: make stop ID=s-..."; exit 1; }
	@curl -fsS -X DELETE -o /dev/null -w '%{http_code}\n' $(API)/sessions/$(ID)

workers: ## worker pods and the jobs each one is running
	@curl -fsS $(API)/workers | jq -r '.workers[] | "\(.agent)  \(.address)  jobs=\(.jobs)  load=\(.load)\(if .full then "  FULL" else "" end)"'
	@kubectl -n $(NAMESPACE) get pods -l app=agent-$(AGENT)

# `kubectl scale` would be undone by the autoscaler within seconds; raising its
# floor is the way to hold more workers. `make deploy` resets it to 1.
scale: ## keep at least N worker pods
	@test -n "$(N)" || { echo "usage: make scale N=3"; exit 1; }
	helm upgrade $(RELEASE) $(CHART) --namespace $(NAMESPACE) --reuse-values \
		--set agents.$(AGENT).scaling.minReplicas=$(N) >/dev/null
	@kubectl -n $(NAMESPACE) get scaledobject agent-$(AGENT)

top: ## CPU and memory per pod
	kubectl -n $(NAMESPACE) top pods

load: ## start COUNT sessions and watch the workers scale up and back down
	API=$(API) AGENT=$(AGENT) NAMESPACE=$(NAMESPACE) scripts/load.sh

smoke: ## end-to-end API check against the running cluster
	API=$(API) scripts/smoke.sh

test: lint ## unit tests and chart lint (no cluster or API keys needed)
	cd orchestrator && uv run pytest -q
	cd agents/voice && uv run pytest -q

# token and dispatch talk to LiveKit directly, bypassing the orchestrator;
# kept for debugging.
token: ## print a join token for ROOM / IDENTITY
	@lk token create --join --room $(ROOM) --identity $(IDENTITY) --valid-for 1h

dispatch: ## send AGENT to ROOM
	lk dispatch create --agent-name $(AGENT) --room $(ROOM)

web: ## serve the browser test client
	python3 -m http.server $(WEB_PORT) --bind 127.0.0.1 --directory web

status:
	kubectl -n $(NAMESPACE) get pods -o wide
	lk room list

logs:
	kubectl -n $(NAMESPACE) logs deploy/livekit --tail=50
	kubectl -n $(NAMESPACE) logs deploy/agent-$(AGENT) --tail=50
	kubectl -n $(NAMESPACE) logs deploy/orchestrator --tail=50

down: ## delete the cluster
	kind delete cluster --name $(CLUSTER)
