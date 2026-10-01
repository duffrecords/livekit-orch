CLUSTER     := livekit-orch
NAMESPACE   := livekit
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

.PHONY: up down env cluster addons image secret node-ip deploy voice session sessions stop workers scale top load smoke test token dispatch web logs status

up: env cluster addons image secret deploy ## create the cluster and deploy everything

env: ## generate .env with random local LiveKit keys if it doesn't exist
	@test -f .env || { \
		printf 'LIVEKIT_API_KEY=local%s\nLIVEKIT_API_SECRET=%s\nLIVEKIT_URL=ws://localhost:7880\n' \
			"$$(openssl rand -hex 4)" "$$(openssl rand -hex 24)" > .env; \
		echo "generated .env"; }

cluster:
	@kind get clusters | grep -qx $(CLUSTER) || kind create cluster --config kind-config.yaml

# Pulls images from registry.k8s.io and ghcr.io. The metrics-server version is
# pinned in deploy/addons/metrics-server/kustomization.yaml.
addons: ## install metrics-server and KEDA
	kubectl apply -k deploy/addons/metrics-server
	kubectl apply --server-side -f https://github.com/kedacore/keda/releases/download/v$(KEDA_VERSION)/keda-$(KEDA_VERSION).yaml
	kubectl -n kube-system rollout status deploy/metrics-server --timeout=180s
	kubectl -n keda rollout status deploy/keda-operator --timeout=180s
	kubectl -n keda rollout status deploy/keda-metrics-apiserver --timeout=180s
	kubectl -n keda rollout status deploy/keda-admission --timeout=180s

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
	kubectl apply -f deploy/namespace.yaml
	@kubectl -n $(NAMESPACE) create secret generic livekit-keys \
		--from-literal=LIVEKIT_API_KEY="$(LIVEKIT_API_KEY)" \
		--from-literal=LIVEKIT_API_SECRET="$(LIVEKIT_API_SECRET)" \
		--from-literal=LIVEKIT_KEYS="$(LIVEKIT_API_KEY): $(LIVEKIT_API_SECRET)" \
		--dry-run=client -o yaml | kubectl apply -f -

# LiveKit only reads NODE_IP at startup, so restart it when the address changes
# (e.g. after switching networks, re-run `make deploy`).
node-ip:
	@kubectl apply -f deploy/namespace.yaml >/dev/null
	@cur=$$(kubectl -n $(NAMESPACE) get configmap livekit-node -o jsonpath='{.data.NODE_IP}' 2>/dev/null); \
	kubectl -n $(NAMESPACE) create configmap livekit-node --from-literal=NODE_IP=$(NODE_IP) \
		--dry-run=client -o yaml | kubectl apply -f - >/dev/null; \
	if [ -n "$$cur" ] && [ "$$cur" != "$(NODE_IP)" ]; then \
		echo "advertised address changed; restarting livekit"; \
		kubectl -n $(NAMESPACE) rollout restart deploy/livekit; fi

deploy: node-ip
	kubectl apply -f deploy/
	kubectl -n $(NAMESPACE) rollout status deploy/livekit --timeout=120s
	kubectl -n $(NAMESPACE) rollout status deploy/agent-echo --timeout=180s
	kubectl -n $(NAMESPACE) rollout status deploy/orchestrator --timeout=120s
ifeq ($(VOICE),1)
	@$(MAKE) --no-print-directory voice
else
	@kubectl delete -f deploy/voice/ --ignore-not-found --wait=false
	@kubectl -n $(NAMESPACE) delete secret/provider-keys configmap/agent-voice-config --ignore-not-found
endif

# Provider keys and VOICE_* settings go from .env into a Secret and a ConfigMap
# through stdin, so a key never appears on a command line. The workers read
# them at startup only, so they are restarted when either has changed.
voice:
	@objects="secret/provider-keys configmap/agent-voice-config"; \
	versions() { kubectl -n $(NAMESPACE) get $$objects -o jsonpath='{.items[*].metadata.resourceVersion}' 2>/dev/null; }; \
	before=$$(versions); \
	grep -E '^($(PROVIDER_KEYS))=.+' .env | kubectl -n $(NAMESPACE) create secret generic provider-keys \
		--from-env-file=/dev/stdin --dry-run=client -o yaml | kubectl apply -f - >/dev/null; \
	grep -E '^VOICE_[A-Z_]+=.+' .env | kubectl -n $(NAMESPACE) create configmap agent-voice-config \
		--from-env-file=/dev/stdin --dry-run=client -o yaml | kubectl apply -f - >/dev/null; \
	kubectl apply -f deploy/voice/; \
	if [ -n "$$before" ] && [ "$$before" != "$$(versions)" ]; then \
		echo "voice settings changed; restarting agent-voice"; \
		kubectl -n $(NAMESPACE) rollout restart deploy/agent-voice; fi
	kubectl -n $(NAMESPACE) rollout status deploy/agent-voice --timeout=180s

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
	kubectl -n $(NAMESPACE) patch scaledobject agent-$(AGENT) --type merge -p '{"spec":{"minReplicaCount":$(N)}}'

top: ## CPU and memory per pod
	kubectl -n $(NAMESPACE) top pods

load: ## start COUNT sessions and watch the workers scale up and back down
	API=$(API) AGENT=$(AGENT) NAMESPACE=$(NAMESPACE) scripts/load.sh

smoke: ## end-to-end API check against the running cluster
	API=$(API) scripts/smoke.sh

test: ## unit tests (no cluster or API keys needed)
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
