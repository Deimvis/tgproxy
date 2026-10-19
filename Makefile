SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c

test:
	@python3 -m unittest discover -s tests -v

test-nix:
	@nix-instantiate --eval --strict nix/tests.nix

check:
	@nix flake check "path:$(PWD)"

.PHONY: test test-nix check
