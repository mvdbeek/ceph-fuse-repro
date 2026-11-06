# See README.md.
DURATION ?= 3600
FAULTS ?=
LABEL ?=
WORKLOAD ?= --workers 4 --hot 4

.PHONY: build up down purge status mount umount run capture

build:
	docker build -t ceph-fuse-repro:17.2.9 docker

up:
	scripts/cluster.sh up

down:
	scripts/cluster.sh down

purge:
	scripts/cluster.sh down --purge

status:
	scripts/cluster.sh status

mount:
	scripts/cluster.sh mount

umount:
	scripts/cluster.sh umount

run:
	python3 harness/run.py --duration $(DURATION) --faults "$(FAULTS)" --label "$(LABEL)" -- $(WORKLOAD)

capture:
	harness/capture.sh results/manual-capture-$$(date +%Y%m%d-%H%M%S) manual
