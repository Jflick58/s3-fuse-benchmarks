# S3 filesystem benchmark harness.
#
# Lifecycle:   make data -> make up -> make corpus -> make bench -> make report -> make down
# `make down` destroys the cluster but keeps the corpus bucket, so the next
# run does not have to regenerate hundreds of GB.

SHELL           := /bin/bash
.DEFAULT_GOAL   := help

AWS_PROFILE     ?= agent-toolkit
REGION          ?= us-west-2
CORPUS_PROFILE  ?= dev
RUN_PROFILE     ?= dev
CLIENTS         ?=
RUN_ID          ?= $(shell date -u +%Y%m%dT%H%M%SZ)
TAG             ?= latest
NODE_TYPE       ?= m5dn.2xlarge
FSX             ?= 0

TF_VARS         := -var region=$(REGION) -var aws_profile=$(AWS_PROFILE) \
                   -var node_instance_type=$(NODE_TYPE) \
                   -var enable_fsx_lustre=$(if $(filter 1 true yes,$(FSX)),true,false)

TF_DATA         := terraform -chdir=terraform/data
TF_CLUSTER      := terraform -chdir=terraform/cluster
export AWS_PROFILE

define need_cluster
	@$(TF_CLUSTER) output -raw cluster_name >/dev/null 2>&1 || \
	  { echo "cluster is not up -- run 'make up' first"; exit 1; }
endef

## ---------------------------------------------------------------------------

help: ## Show this help
	@echo "S3 filesystem benchmark"
	@echo
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n",$$1,$$2}'
	@echo
	@echo "Vars: CORPUS_PROFILE={dev|prod} RUN_PROFILE={dev|prod} CLIENTS=mountpoint,s3fs"
	@echo "      NODE_TYPE=m5dn.8xlarge FSX=1"

init: ## terraform init for both states
	$(TF_DATA) init -input=false
	$(TF_CLUSTER) init -input=false

data: ## Create the corpus S3 bucket (survives 'make down')
	$(TF_DATA) init -input=false
	$(TF_DATA) apply -auto-approve -var region=$(REGION) -var aws_profile=$(AWS_PROFILE)
	@echo "bucket: $$($(TF_DATA) output -raw bucket)"

up: data ## Create the EKS cluster and node, then build and push the runner image
	$(TF_CLUSTER) init -input=false
	$(TF_CLUSTER) apply -auto-approve $(TF_VARS)
	@$(MAKE) --no-print-directory kubeconfig
	@$(MAKE) --no-print-directory image
	@echo
	@echo "Cluster is up. Estimated burn: ~\$$0.64/hr (node + control plane)."
	@echo "Next: make corpus CORPUS_PROFILE=$(CORPUS_PROFILE)"

kubeconfig: ## Point kubectl at the benchmark cluster
	aws eks update-kubeconfig --name $$($(TF_CLUSTER) output -raw cluster_name) \
	  --region $(REGION) --profile $(AWS_PROFILE)

image: ## Build and push the benchmark runner image to ECR
	$(call need_cluster)
	@set -euo pipefail; \
	REPO=$$($(TF_CLUSTER) output -raw ecr_repository_url); \
	REG=$${REPO%%/*}; \
	aws ecr get-login-password --region $(REGION) --profile $(AWS_PROFILE) \
	  | docker login --username AWS --password-stdin $$REG; \
	docker buildx build --platform linux/amd64 -f image/Dockerfile \
	  -t $$REPO:$(TAG) --push . ; \
	echo "pushed $$REPO:$(TAG)"

corpus: ## Generate the test corpus in S3 (CORPUS_PROFILE=dev|prod)
	$(call need_cluster)
	@set -euo pipefail; \
	export IMAGE=$$($(TF_CLUSTER) output -raw ecr_repository_url):$(TAG); \
	export BUCKET=$$($(TF_CLUSTER) output -raw bucket); \
	export REGION=$(REGION) CORPUS_PROFILE=$(CORPUS_PROFILE) RUN_ID=$(RUN_ID); \
	envsubst < k8s/corpus-job.yaml.tpl | kubectl apply -f - ; \
	echo "waiting for node to be ready..."; \
	kubectl wait --for=condition=Ready node -l workload=benchmark --timeout=600s; \
	kubectl wait --for=condition=Ready pod -l app=s3fuse-corpus --timeout=600s || true; \
	kubectl logs -f job/corpus-$(RUN_ID)

bench: ## Run the benchmark (RUN_PROFILE=dev|prod, CLIENTS=comma,list)
	$(call need_cluster)
	@set -euo pipefail; \
	export IMAGE=$$($(TF_CLUSTER) output -raw ecr_repository_url):$(TAG); \
	export BUCKET=$$($(TF_CLUSTER) output -raw bucket); \
	export REGION=$(REGION) RUN_PROFILE=$(RUN_PROFILE) CLIENTS="$(CLIENTS)" RUN_ID=$(RUN_ID); \
	envsubst < k8s/bench-job.yaml.tpl | kubectl apply -f - ; \
	kubectl wait --for=condition=Ready pod -l app=s3fuse-bench --timeout=600s || true; \
	kubectl logs -f job/bench-$(RUN_ID); \
	echo; echo "run id: $(RUN_ID)"; \
	$(MAKE) --no-print-directory results RUN_ID=$(RUN_ID)

results: ## Download results for RUN_ID (defaults to the newest run)
	@set -euo pipefail; \
	BUCKET=$$($(TF_DATA) output -raw bucket); \
	ID="$(RUN_ID)"; \
	if ! aws s3 ls "s3://$$BUCKET/results/$$ID/" --profile $(AWS_PROFILE) >/dev/null 2>&1; then \
	  ID=$$(aws s3 ls "s3://$$BUCKET/results/" --profile $(AWS_PROFILE) \
	        | awk '{print $$2}' | tr -d / | sort | tail -1); \
	fi; \
	mkdir -p results; \
	aws s3 cp "s3://$$BUCKET/results/$$ID/results.jsonl" results/results.jsonl \
	  --profile $(AWS_PROFILE); \
	echo "downloaded run $$ID -> results/results.jsonl"

report: ## Render results/results.jsonl into results/report.md
	python3 bench/report.py --results results/results.jsonl --out results/report.md

status: ## Show what is currently running and what it costs
	@$(TF_CLUSTER) output 2>/dev/null || echo "cluster not deployed"
	@echo; kubectl get nodes,pods 2>/dev/null || true

logs: ## Tail the most recent benchmark pod
	kubectl logs -f -l app=s3fuse-bench --tail=200

shell: ## Interactive shell on the benchmark node (for manual poking)
	kubectl run bench-shell --rm -it --restart=Never \
	  --image=$$($(TF_CLUSTER) output -raw ecr_repository_url):$(TAG) \
	  --overrides='{"spec":{"hostNetwork":true,"nodeSelector":{"workload":"benchmark"},"containers":[{"name":"bench-shell","image":"'$$($(TF_CLUSTER) output -raw ecr_repository_url):$(TAG)'","stdin":true,"tty":true,"command":["/bin/bash"],"securityContext":{"privileged":true},"volumeMounts":[{"name":"nvme","mountPath":"/mnt/nvme"},{"name":"lustre","mountPath":"/mnt/lustre"}]}],"volumes":[{"name":"nvme","hostPath":{"path":"/mnt/nvme","type":"DirectoryOrCreate"}},{"name":"lustre","hostPath":{"path":"/mnt/lustre","type":"DirectoryOrCreate"}}]}}'

down: ## Destroy the cluster and node. Keeps the corpus bucket.
	$(TF_CLUSTER) destroy -auto-approve $(TF_VARS)
	@echo "Cluster destroyed. Corpus bucket kept -- 'make nuke' removes it too."

nuke: down ## Destroy everything, including the corpus bucket
	$(TF_DATA) destroy -auto-approve -var region=$(REGION) -var aws_profile=$(AWS_PROFILE)

.PHONY: help init data up kubeconfig image corpus bench results report status logs shell down nuke
