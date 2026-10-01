CLUSTER     := livekit-orch
NAMESPACE   := livekit
AGENT_IMAGE := livekit-orch/agent-echo:dev
ROOM        ?= spike
IDENTITY    ?= user1
WEB_PORT    ?= 8000

-include .env
export LIVEKIT_URL LIVEKIT_API_KEY LIVEKIT_API_SECRET

# Address LiveKit advertises to host clients: the host's primary LAN address,
# detected at deploy time (never committed). Falls back to loopback when there
# is no network; loopback works in Chrome but not Firefox. Override with
# `make deploy NODE_IP=...` or NODE_IP in .env.
NODE_IP ?= $(shell python3 -c "import socket; s=socket.socket(socket.AF_INET, socket.SOCK_DGRAM); s.connect(('192.0.2.1', 9)); print(s.getsockname()[0])" 2>/dev/null || echo 127.0.0.1)

.PHONY: up down env cluster image secret node-ip deploy token dispatch web logs status

up: env cluster image secret deploy ## create the cluster and deploy everything

env: ## generate .env with random local LiveKit keys if it doesn't exist
	@test -f .env || { \
		printf 'LIVEKIT_API_KEY=local%s\nLIVEKIT_API_SECRET=%s\nLIVEKIT_URL=ws://localhost:7880\n' \
			"$$(openssl rand -hex 4)" "$$(openssl rand -hex 24)" > .env; \
		echo "generated .env"; }

cluster:
	@kind get clusters | grep -qx $(CLUSTER) || kind create cluster --config kind-config.yaml

image:
	docker build -t $(AGENT_IMAGE) agents/echo
	kind load docker-image $(AGENT_IMAGE) --name $(CLUSTER)

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

token: ## print a join token for ROOM / IDENTITY
	@lk token create --join --room $(ROOM) --identity $(IDENTITY) --valid-for 1h

dispatch: ## send the echo agent to ROOM
	lk dispatch create --agent-name echo --room $(ROOM)

web: ## serve the browser test client
	python3 -m http.server $(WEB_PORT) --bind 127.0.0.1 --directory web

status:
	kubectl -n $(NAMESPACE) get pods -o wide
	lk room list

logs:
	kubectl -n $(NAMESPACE) logs deploy/livekit --tail=50
	kubectl -n $(NAMESPACE) logs deploy/agent-echo --tail=50

down: ## delete the cluster
	kind delete cluster --name $(CLUSTER)
